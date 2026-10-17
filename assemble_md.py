#!/usr/bin/env python3
"""Собрать готовый md-конспект из рабочих файлов конвейера.

Читает workdir/meta.json, summary.md, transcript_ru.md и склеивает их
в один файл по шаблону TEMPLATE.md:
vid-блок -> ## Саммари -> ## Мои заметки -> ## Стенограмма
(разделы разделяются линией ---; после подзаголовков пустых строк не остаётся;
«Мои заметки» — пустой раздел-заготовка для заметок пользователя).

На stdout печатает одну строку JSON с путём к результату.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BAD_FILENAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
HEADING_RE = re.compile(r"#{1,6}\s")


def sanitize_filename(name: str, max_len: int = 140) -> str:
    name = BAD_FILENAME_RE.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    return name[:max_len].rstrip(". ") or "video"


def load(path: Path) -> str | None:
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        return text or None
    return None


def normalize(md: str) -> str:
    """Единый стиль: без пустой строки сразу после подзаголовка, пустая строка
    перед подзаголовком, не больше одной пустой строки подряд."""
    md = md.replace("\r\n", "\n").replace("\u00a0", " ")
    md = re.sub(r"==([^=]+)==", r"\1", md)  # хайлайты зарезервированы для ручного выделения человеком
    out: list[str] = []
    in_fence = False
    for line in (l.rstrip() for l in md.split("\n")):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if not line.strip():
            prev = out[-1] if out else ""
            if out and prev.strip() and not HEADING_RE.match(prev):
                out.append("")
            continue
        if HEADING_RE.match(line) and out and out[-1].strip() and not in_fence:
            out.append("")  # перед подзаголовком нужна пустая строка
        out.append(line)
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def vid_block(meta: dict) -> str:
    thumb = meta.get("thumbnail") or f"https://i.ytimg.com/vi/{meta.get('video_id', '')}/mqdefault.jpg"
    rows = [
        "```vid",
        meta.get("url", ""),
        f"Title: {meta.get('title', '')}",
        f"Author: {meta.get('author', '')}",
        f"Thumbnail: {thumb}",
        f"AuthorUrl: {meta.get('author_url', '')}",
        "```",
    ]
    return "\n".join(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Собрать финальный md-конспект")
    ap.add_argument("--work", required=True, help="рабочая папка видео (work/<video_id>)")
    ap.add_argument("--out", default=None, help="путь к финальному .md (по умолчанию summaries/<Автор>. <Название>.md)")
    args = ap.parse_args()

    workdir = Path(args.work)
    meta_path = workdir / "meta.json"
    if not meta_path.exists():
        print(f"ОШИБКА: не найден {meta_path}", file=sys.stderr)
        sys.exit(1)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    summary = load(workdir / "summary.md")
    if not summary:
        print(f"ОШИБКА: не найден или пуст {workdir / 'summary.md'}", file=sys.stderr)
        sys.exit(1)
    transcript = load(workdir / "transcript_ru.md")

    parts = [vid_block(meta), "## Саммари\n" + normalize(summary)]
    parts.append("---\n\n## Мои заметки")
    if transcript:
        parts.append("---\n\n## Стенограмма\n" + normalize(transcript))
    doc = "\n\n".join(parts) + "\n"

    if args.out:
        out_path = Path(args.out)
    else:
        author = meta.get("author") or ""
        title = meta.get("title") or ""
        name = f"{author}. {title}".strip(". ")
        out_path = ROOT / "summaries" / (sanitize_filename(name) + ".md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")

    print(
        json.dumps(
            {
                "ok": True,
                "out": str(out_path),
                "chars": len(doc),
                "sections": ["Саммари", "Мои заметки"] + (["Стенограмма"] if transcript else []),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    main()
