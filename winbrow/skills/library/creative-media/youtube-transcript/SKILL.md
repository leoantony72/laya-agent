---
name: youtube-transcript
skill: playbook_youtube_transcript
kind: playbook
category: creative-media
source: https://github.com/michalparkola/tapestry-skills-for-claude-code/tree/main/youtube-transcript
license: see upstream repository
args: []
description: >
  Fetch and clean a YouTube transcript, then summarize it. Use when the user
  gives a video URL and wants captions or a summary.
---

# YouTube Transcript

## When to Use

Use when GOAL provides a YouTube URL and asks for the transcript, captions,
subtitles, or a summary of what is said.

## When NOT to Use

- Do not use for a video with no captions and no speech-to-text fallback.
- Do not transcribe private or age-gated content without access.
- Do not republish a full transcript of copyrighted material without
  permission; summarize when possible.
- Do not claim timestamps that the cleaned transcript does not contain.

## Prerequisites

- `yt-dlp` installed (`pip install yt-dlp`).
- `run_skill_script` with `skill="youtube-transcript"` and
  `script="fetch_transcript.py"`.

## Step Pattern

1. Call `run_skill_script` with
   `{"operation":"fetch","url":"https://...","language":"en","output_dir":"C:\\Users\\me\\Downloads"}`.
2. Confirm the returned path and character count. Read the saved file with
   `read_document` if it is long.
3. Summarize in sections or bullets. Quote short phrases only when needed.
4. If the user asked for a full transcript file, return the path. Otherwise
   give a summary plus 3–5 key quotes.
5. Mention that auto-generated captions may contain errors.

## Spec Detail

- `language` defaults to `en`; the script also tries English fallback.
- `output_path` overrides the default `<title>.txt`.
- `cleanup` defaults to true and removes the temporary VTT.
- `output_dir` defaults to Downloads.

## Output Shape

Returns `success`, `path`, and `data` with title, character count, and language.

## Recovery

- No captions: report it and offer a summary from another source.
- Wrong language: retry once with the correct language code.
- Private video: ask the user for access; do not bypass.
- Transcript is huge: summarize chunks rather than pasting everything.
