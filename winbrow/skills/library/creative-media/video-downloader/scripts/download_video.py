"""Download video or audio with yt-dlp, without assuming a browser extension."""

from __future__ import annotations

import shutil
from pathlib import Path

from winbrow.skill_script_utils import emit, fail, load_spec, require_module, resolve_path

yt_dlp = require_module("yt_dlp", "yt-dlp")


def download(spec: dict) -> None:
    url = str(spec.get("url") or "").strip()
    if not url:
        fail("A video URL is required.")
    output_dir = resolve_path(spec.get("output_dir") or spec.get("folder") or Path.home() / "Downloads")
    output_dir.mkdir(parents=True, exist_ok=True)

    quality = str(spec.get("quality") or "best").lower()
    height = {"best": None, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360,
              "worst": 240}.get(quality)
    container = str(spec.get("format") or "mp4").lower()
    audio_only = bool(spec.get("audio_only"))
    ffmpeg = shutil.which("ffmpeg")

    if audio_only:
        if not ffmpeg:
            fail(
                "Audio-only extraction needs ffmpeg on PATH. Install ffmpeg or "
                "download the video and convert it separately."
            )
        fmt = "bestaudio/best"
    elif height:
        if ffmpeg:
            fmt = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
        else:
            fmt = f"best[height<={height}][ext={container}]/best[height<={height}]"
    else:
        fmt = f"bestvideo+bestaudio/best" if ffmpeg else "best"

    options = {
        "format": fmt,
        "outtmpl": str(output_dir / "%(title).150B [%(id)s].%(ext)s"),
        "noplaylist": bool(spec.get("noplaylist", True)),
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": False,
        "windowsfilenames": True,
        "merge_output_format": container,
    }
    if audio_only:
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": str(spec.get("audio_format") or "mp3"),
            "preferredquality": str(spec.get("audio_quality") or "192"),
        }]
    if spec.get("cookies_from_browser"):
        options["cookiesfrombrowser"] = (str(spec["cookies_from_browser"]),)
    if spec.get("proxy"):
        options["proxy"] = str(spec["proxy"])

    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)
    except Exception as exc:
        fail(f"Download failed: {exc}")

    downloads = info.get("requested_downloads") or []
    paths = []
    for item in downloads:
        filepath = item.get("filepath") or item.get("_filename")
        if filepath:
            paths.append(str(Path(filepath)))
    if not paths:
        # Fall back to the prepared filename when yt-dlp does not expose it.
        filepath = info.get("_filename")
        if filepath:
            paths.append(str(filepath))
    if not paths:
        fail("Download finished but yt-dlp did not report an output path.")
    emit(True, f"Downloaded {len(paths)} file(s).", paths=paths, data={
        "title": info.get("title"), "duration": info.get("duration"),
    })


def main() -> None:
    download(load_spec())


if __name__ == "__main__":
    main()
