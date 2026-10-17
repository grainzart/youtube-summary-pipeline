#!/usr/bin/env python3
"""Скачать субтитры и метаданные YouTube-видео для пайплайна саммаризации.

Выбор дорожки субтитров (--lang auto, по умолчанию):
  1) ручные субтитры: ru, затем en;
  2) оригинальная автораскадровка (её язык = язык видео), если это ru или en;
  3) автоперевод автораскадровки: ru, затем en.

Результат кладётся в work/<Автор>_<Название>_[<video_id>]/ (без пробелов): meta.json, transcript.txt, raw.vtt.
На stdout печатается одна строка JSON с путями и параметрами.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.request
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp

ROOT = Path(__file__).resolve().parent
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
LANGS = ("ru", "en")

TAG_RE = re.compile(r"<[^>]+>")
CUE_TIME_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})[.,]\d{3}")
SENT_END_RE = re.compile(r"[.!?…]")
BAD_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_dir_name(name: str, max_len: int = 120) -> str:
    """Имя папки без пробелов: плохие символы и пробелы -> подчёркивания."""
    name = BAD_CHARS_RE.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    return re.sub(" ", "_", name[:max_len].rstrip(". "))


def norm_lang(code: str) -> str:
    return code.split("-")[0].split("_")[0].lower()


def pick_format(tracks: list[dict]) -> dict | None:
    for ext in ("vtt", "srt"):
        for t in tracks:
            if t.get("ext") == ext:
                return t
    return tracks[0] if tracks else None


def is_original_auto(track: dict) -> bool:
    # У автопереведённых дорожек в URL есть параметр tlang; у оригинала его нет.
    q = parse_qs(urlparse(track.get("url", "")).query)
    return "tlang" not in q


def by_norm_lang(tracks_map: dict[str, list]) -> dict[str, list]:
    out: dict[str, list] = {}
    for lang, tracks in tracks_map.items():
        n = norm_lang(lang)
        if n not in out and tracks:
            out[n] = tracks
    return out


def choose_track(subs: dict, autos: dict, forced: str):
    """Возвращает (lang, source, track) или None. source: manual | auto | auto-translated."""
    manual = by_norm_lang(subs)
    auto = by_norm_lang(autos)
    if forced in LANGS:
        if manual.get(forced):
            return forced, "manual", pick_format(manual[forced])
        if auto.get(forced):
            src = "auto" if is_original_auto(auto[forced][0]) else "auto-translated"
            return forced, src, pick_format(auto[forced])
        return None
    for lang in LANGS:  # ручные субтитры точнее автораскадровки
        tr = pick_format(manual.get(lang) or [])
        if tr:
            return lang, "manual", tr
    for lang, tracks in auto.items():  # оригинальная автораскадровка = язык видео
        tr = pick_format(tracks)
        if tr and lang in LANGS and is_original_auto(tracks[0]):
            return lang, "auto", tr
    for lang in LANGS:  # автоперевод; ru приоритетнее — конспект всё равно на русском
        tr = pick_format(auto.get(lang) or [])
        if tr:
            return lang, "auto-translated", tr
    return None


def fetch_info(url: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "no_progress": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 30,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if info and "entries" in info:  # ytsearchN:... или плейлист — берём первое видео
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise RuntimeError("поиск/плейлист не вернул ни одного видео")
        info = entries[0]
    return info


def download_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def unescape(text: str) -> str:
    # YouTube любит двойное экранирование: &amp;#39; -> &#39; -> '
    for _ in range(3):
        new = html.unescape(text)
        if new == text:
            return new
        text = new
    return text


def parse_vtt(text: str) -> list[tuple[float, str]]:
    """WebVTT/SRT -> список (секунда_начала_куэ, очищенная_строка) в порядке следования."""
    events: list[tuple[float, str]] = []
    cur_time = 0.0
    for raw in text.splitlines():
        line = raw.strip("\ufeff").strip()
        if not line:
            continue
        if line == "WEBVTT" or line.startswith(("Kind:", "Language:", "NOTE", "Style:", "Region:")):
            continue
        if "-->" in line:
            m = CUE_TIME_RE.search(line)
            if m:
                h, mi, s = map(int, m.groups())
                cur_time = h * 3600 + mi * 60 + s
            continue
        if line.isdigit():  # номера кую в srt
            continue
        clean = unescape(TAG_RE.sub("", line)).replace("\u00a0", " ").strip()
        if clean:
            events.append((cur_time, clean))
    return events


def dedupe_rolling(events: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """Убирает повторы автораскадровки: точные дубли подряд и скользящие строки."""
    no_dupes: list[tuple[float, str]] = []
    for t, ln in events:
        if no_dupes and no_dupes[-1][1] == ln:
            continue
        no_dupes.append((t, ln))
    out: list[tuple[float, str]] = []
    for i, (t, ln) in enumerate(no_dupes):
        if i + 1 < len(no_dupes) and no_dupes[i + 1][1].startswith(ln):
            continue  # текущая строка — начало следующей, оставляем следующую
        out.append((t, ln))
    return out


def build_paragraphs(
    events: list[tuple[float, str]],
    with_timestamps: bool,
    target_sentences: int = 6,
    max_lines: int = 9,
    max_gap: float = 45.0,
) -> str:
    """Склеивает строки в абзацы: по предложениям, по числу строк или по паузе в речи."""
    paras: list[str] = []
    lines: list[str] = []
    start_t = events[0][0] if events else 0.0
    sent = 0

    def flush() -> None:
        nonlocal lines, sent
        if lines:
            body = " ".join(lines)
            if with_timestamps:
                m, s = divmod(int(start_t), 60)
                body = f"[{m:02d}:{s:02d}] {body}"
            paras.append(body)
        lines.clear()
        sent = 0

    prev_t = None
    for t, ln in events:
        if prev_t is not None and lines and t - prev_t >= max_gap:
            flush()
        if not lines:
            start_t = t
        lines.append(ln)
        sent += len(SENT_END_RE.findall(ln))
        ends_sentence = ln.endswith((".", "!", "?", "…", '"', "»"))
        if (sent >= target_sentences and ends_sentence) or len(lines) >= max_lines:
            flush()
        prev_t = t
    flush()
    return "\n\n".join(paras)


def main() -> None:
    ap = argparse.ArgumentParser(description="Скачать субтитры YouTube-видео для саммаризации")
    ap.add_argument("url", help="ссылка на видео (или ytsearchN:запрос)")
    ap.add_argument("--out", default=None, help="рабочая папка (по умолчанию work/<video_id>)")
    ap.add_argument("--lang", default="auto", choices=["auto", "ru", "en"], help="язык субтитров")
    ap.add_argument("--timestamps", action="store_true", help="добавлять [ММ:СС] в начало абзацев")
    args = ap.parse_args()

    try:
        info = fetch_info(args.url)
    except Exception as e:  # noqa: BLE001
        print(f"ОШИБКА: не удалось получить данные видео: {e}", file=sys.stderr)
        sys.exit(1)

    subs = info.get("subtitles") or {}
    autos = info.get("automatic_captions") or {}
    chosen = choose_track(subs, autos, args.lang)
    if not chosen:
        print(
            "ОШИБКА: у видео нет субтитров на ru/en (ни ручных, ни автоматических) — обработка невозможна.",
            file=sys.stderr,
        )
        sys.exit(1)
    lang, source, track = chosen

    try:
        raw = download_text(track["url"])
    except Exception as e:  # noqa: BLE001
        print(f"ОШИБКА: не удалось скачать файл субтитров: {e}", file=sys.stderr)
        sys.exit(1)

    events = dedupe_rolling(parse_vtt(raw))
    if not events:
        print("ОШИБКА: файл субтитров пуст или не удалось его разобрать.", file=sys.stderr)
        sys.exit(1)
    transcript = build_paragraphs(events, args.timestamps)

    vid = info.get("id") or "video"
    if args.out:
        workdir = Path(args.out)
    else:
        # Автор в начале имени папки — чтобы work/ сортировалась по каналам.
        # Папки work/ без пробелов (подчёркивания) — удобно в терминале;
        # готовые файлы в summaries/ собираются с пробелами (для Obsidian).
        author = (info.get("uploader") or info.get("channel") or "").strip()
        title = (info.get("title") or "").strip()
        base = "_".join(x for x in (safe_dir_name(author), safe_dir_name(title)) if x) or vid
        workdir = ROOT / "work" / f"{base}_[{vid}]"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "raw.vtt").write_text(raw, encoding="utf-8")
    (workdir / "transcript.txt").write_text(transcript + "\n", encoding="utf-8")

    meta = {
        "video_id": vid,
        "url": info.get("webpage_url") or args.url,
        "title": info.get("title") or "",
        "author": info.get("uploader") or info.get("channel") or "",
        "author_url": info.get("channel_url") or info.get("uploader_url") or "",
        "thumbnail": info.get("thumbnail") or f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
        "duration_sec": info.get("duration"),
        "upload_date": info.get("upload_date") or "",
        "sub_language": lang,
        "sub_source": source,
    }
    (workdir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    result = {
        "ok": True,
        "work": str(workdir),
        "transcript": str(workdir / "transcript.txt"),
        "meta": str(workdir / "meta.json"),
        "lang": lang,
        "source": source,
        "chars": len(transcript),
        "title": meta["title"],
        "author": meta["author"],
        "duration_sec": meta["duration_sec"],
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    main()
