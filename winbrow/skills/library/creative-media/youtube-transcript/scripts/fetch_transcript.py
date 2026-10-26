"""Fetch and clean YouTube captions with yt-dlp."""

from __future__ import annotations

import re
from pathlib import Path

from winbrow.skill_script_utils import emit, fail, load_spec, require_module, resolve_path

yt_dlp = require_module("yt_dlp", "yt-dlp")

_TIMESTAMP = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3}\s*-->")
_TAGS = re.compile(r"<[^>]+>")
_CUE = re.compile(r"^\d+$")


def _clean_vtt(text: str) -> str:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("WEBVTT") or line.startswith("NOTE"):
            continue
        if _TIMESTAMP.match(line) or _CUE.match(line):
            continue
        line = _TAGS.sub("", line)
        line = line.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        if not line:
            continue
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    # YouTube auto-captions repeat rolling lines; a second dedupe pass removes
    # adjacent phrases that are strict prefixes of the next line.
    cleaned: list[str] = []
    for line in lines:
        if cleaned and line.startswith(cleaned[-1]):
            cleaned[-1] = line
        else:
            cleaned.append(line)
    return " ".join(cleaned)


def fetch(spec: dict) -> None:
    url = str(spec.get("url") or "").strip()
    if not url:
        fail("A YouTube URL is required.")
    language = str(spec.get("language") or "en")
    work_dir = resolve_path(spec.get("output_dir") or Path.home() / "Downloads")
    work_dir.mkdir(parents=True, exist_ok=True)
    options = {
        "skip_download": True,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [language, "en"],
        "subtitlesformat": "vtt",
        "outtmpl": str(work_dir / "%(title).120B [%(id)s].%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "windowsfilenames": True,
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)
    except Exception as exc:
        fail(f"Could not fetch captions: {exc}")

    title = str(info.get("title") or "youtube_transcript")
    candidates = sorted(work_dir.glob(f"*{info.get('id', '')}*.vtt"))
    if not candidates:
        fail("No captions were found for this video. It may have none, or the language is unavailable.")
    subtitle_path = candidates[0]
    content = _clean_vtt(subtitle_path.read_text(encoding="utf-8", errors="replace"))
    if not content:
        fail("Captions were downloaded but contained no text.")

    output = resolve_path(
        spec.get("output_path")
        or (work_dir / f"{title[:120]}.txt")
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    if spec.get("cleanup", True):
        try:
            subtitle_path.unlink()
        except OSError:
            pass
    emit(True, f"Saved transcript: {output}", path=output, data={
        "title": title, "chars": len(content), "language": language,
    })


def main() -> None:
    fetch(load_spec())


if __name__ == "__main__":
    main()
