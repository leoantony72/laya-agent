---
name: video-downloader
skill: playbook_video_downloader
kind: playbook
category: creative-media
source: https://github.com/ComposioHQ/awesome-claude-skills
license: Apache-2.0
args: []
description: >
  Download a video or audio track from YouTube and similar sites. Use for
  offline viewing or archival when the user has the rights.
---

# Video Downloader

## When to Use

Use when GOAL gives a video URL and asks to download, save, archive, or extract
the audio.

## When NOT to Use

- Do not download copyrighted material the user does not have rights to save.
- Do not bypass DRM, paywalls, or private-video access controls.
- Do not download a whole playlist unless the user explicitly asks.
- Do not claim an audio file was created if ffmpeg is missing.

## Prerequisites

- `yt-dlp` installed (`pip install yt-dlp`).
- Optional: ffmpeg on PATH for merging separate video/audio streams and MP3
  extraction.
- `run_skill_script` with `skill="video-downloader"` and
  `script="download_video.py"`.

## Step Pattern

1. Confirm the URL and intended use. If the user mentions a playlist, ask
   whether they want all items or one.
2. Choose quality (`best`, `1080p`, `720p`, `480p`, `360p`) and format (`mp4`,
   `webm`, `mkv`) or `audio_only`.
3. Call `run_skill_script`:
   `{"operation":"download","url":"https://...","output_dir":"C:\\Users\\me\\Downloads","quality":"720p","format":"mp4"}`
   or `{"audio_only":true,"audio_format":"mp3"}`.
4. Verify the returned `paths` and file sizes. Do not claim success without
   them.
5. Report title, path, format, and size.

## Spec Detail

- `output_dir` defaults to Downloads.
- `noplaylist` defaults to true.
- `audio_only` requires ffmpeg.
- `cookies_from_browser` is optional and uses the user's own session.
- `proxy` is optional; never put credentials in the spec.

## Output Shape

Returns `success`, `paths`, and `data` with title and duration.

## Recovery

- `yt-dlp` missing: report `pip install yt-dlp` and stop.
- ffmpeg missing: download a single-file MP4 or ask the user to install it.
- 403/age gate: ask the user to sign in in the controlled browser and pass
  `cookies_from_browser`, if appropriate.
- Download stalls: cancel and retry once with a lower quality.
