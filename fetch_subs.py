#!/usr/bin/env python3
"""Скачать субтитры и метаданные YouTube-видео для пайплайна саммаризации.

Выбор дорожки субтитров (--lang auto, по умолчанию):
  1) определяется язык ОРИГИНАЛА видео — по метаданным (поле language), иначе
     по дорожке '-orig'/без tlang; субтитры берутся на языке оригинала:
     ручные, иначе оригинальная автораскадровка. У видео с дубляжами есть
     'оригинальные' автораскадровки каждого дубляжа — берётся именно язык
     оригинала, а не перевод;
  2) если оригинал не ru/en — ручные ru/en, затем автоперевод (ru, затем en).

Скачиванием файла субтитров занимается сам yt-dlp (своим клиентом с полным
контекстом запроса) — прямые запросы к эндпоинту timedtext YouTube отбивает
ошибкой 429.

Результат кладётся в work/<Автор>_<Название>_[<video_id>]/ (без пробелов):
meta.json, transcript.txt, raw.vtt. На stdout — одна строка JSON.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yt_dlp

ROOT = Path(__file__).resolve().parent
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


def is_original_auto(track: dict) -> bool:
    # У автопереведённых дорожек в URL есть параметр tlang; у оригинала его нет.
    q = parse_qs(urlparse(track.get("url", "")).query)
    return "tlang" not in q


def find_track(tracks_map: dict, lang: str, original_only: bool = False) -> str | None:
    """Ищет ключ дорожки языка lang; при original_only — только оригинальную."""
    for key, tracks in tracks_map.items():
        if norm_lang(key) != lang or not tracks:
            continue
        if original_only and not (key.endswith("-orig") or is_original_auto(tracks[0])):
            continue
        return key
    return None


def choose_track(subs: dict, autos: dict, forced: str, video_lang: str = ""):
    """Возвращает (точный_ключ_дорожки, язык, источник) или None.

    Точный ключ нужен yt-dlp для скачивания; язык: ru|en;
    источник: manual | auto | auto-translated.
    """
    if forced in LANGS:
        key = find_track(subs, forced)
        if key:
            return key, forced, "manual"
        key = find_track(autos, forced)
        if key:
            src = "auto" if key.endswith("-orig") or is_original_auto(autos[key][0]) else "auto-translated"
            return key, forced, src
        return None

    # Язык оригинала: метаданные видео -> ключ '-orig' -> дорожка без tlang.
    orig_lang = ""
    if norm_lang(video_lang) in LANGS:
        orig_lang = norm_lang(video_lang)
    if not orig_lang:
        for key, tracks in autos.items():
            if key.endswith("-orig") and tracks and norm_lang(key) in LANGS:
                orig_lang = norm_lang(key)
                break
    if not orig_lang:
        for key, tracks in autos.items():
            if tracks and is_original_auto(tracks[0]) and norm_lang(key) in LANGS:
                orig_lang = norm_lang(key)
                break

    if orig_lang:
        key = find_track(subs, orig_lang)
        if key:
            return key, orig_lang, "manual"
        key = find_track(autos, orig_lang, original_only=True)
        if key:
            return key, orig_lang, "auto"

    # Оригинал не ru/en (или неизвестен): ручные ru/en, затем автоперевод
    for lang in LANGS:
        key = find_track(subs, lang)
        if key:
            return key, lang, "manual"
    for lang in LANGS:
        key = find_track(autos, lang)
        if key:
            return key, lang, "auto-translated"
    return None


def ydl_opts(**extra) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 5,
    }
    opts.update(extra)
    return opts


def fetch_info(url: str) -> dict:
    with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
        info = ydl.extract_info(url, download=False)
    if info and "entries" in info:  # ytsearchN:... или плейлист — берём первое видео
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise RuntimeError("поиск/плейлист не вернул ни одного видео")
        info = entries[0]
    return info


def download_subtitles(url: str, lang_key: str, manual: bool, out_base: Path) -> Path:
    """Скачивает дорожку субтитров yt-dlp'ом; возвращает путь к файлу .vtt/.srt."""
    opts = ydl_opts(
        writesubtitles=manual,
        writeautomaticsub=not manual,
        subtitleslangs=[lang_key],
        subtitlesformat="vtt/best",
        outtmpl=str(out_base),
    )
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    found = [
        p
        for p in out_base.parent.iterdir()
        if p.name.startswith(out_base.name + ".") and p.suffix in (".vtt", ".srt")
    ]
    if not found:
        raise RuntimeError("yt-dlp не смог скачать файл субтитров")
    return found[0]


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
    ap.add_argument("--out", default=None, help="рабочая папка (по умолчанию work/<Автор>_<Название>_[<id>])")
    ap.add_argument("--lang", default="auto", choices=["auto", "ru", "en"], help="язык субтитров")
    ap.add_argument("--timestamps", action="store_true", help="добавлять [ММ:СС] в начало абзацев")
    args = ap.parse_args()

    try:
        info = fetch_info(args.url)
    except Exception as e:  # noqa: BLE001
        print(f"ОШИБКА: не удалось получить данные видео: {e}", file=sys.stderr)
        sys.exit(1)

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

    subs = info.get("subtitles") or {}
    autos = info.get("automatic_captions") or {}
    chosen = choose_track(subs, autos, args.lang, info.get("language") or "")
    if not chosen:
        print(
            "ОШИБКА: у видео нет субтитров на ru/en (ни ручных, ни автоматических) — обработка невозможна.",
            file=sys.stderr,
        )
        sys.exit(1)
    key, lang, source = chosen

    workdir.mkdir(parents=True, exist_ok=True)
    try:
        vtt_path = download_subtitles(args.url, key, source == "manual", workdir / "subs")
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        hint = ""
        if "429" in msg or "too many requests" in msg.lower():
            hint = " Это лимит запросов YouTube по IP: подожди 1–2 минуты и повтори один раз; частые повторы продлевают блокировку."
        print(f"ОШИБКА: не удалось скачать субтитры: {msg}.{hint}", file=sys.stderr)
        sys.exit(1)

    raw = vtt_path.read_text(encoding="utf-8", errors="replace")
    vtt_path.unlink(missing_ok=True)
    events = dedupe_rolling(parse_vtt(raw))
    if not events:
        print("ОШИБКА: файл субтитров пуст или не удалось его разобрать.", file=sys.stderr)
        sys.exit(1)
    transcript = build_paragraphs(events, args.timestamps)

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
    (workdir / "raw.vtt").write_text(raw, encoding="utf-8")
    (workdir / "transcript.txt").write_text(transcript + "\n", encoding="utf-8")
    (workdir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(
        json.dumps(
            {
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
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    main()
