---
name: imagen
skill: playbook_imagen
kind: playbook
category: creative-media
source: https://github.com/sanjay3290/ai-skills/tree/main/skills/imagen
license: Apache-2.0
args: []
description: >
  Generate or edit images with Gemini. Use for mockups, icons, illustrations,
  and visual assets when a Gemini API key is configured.
---

# Gemini Image Generation

## When to Use

Use when GOAL asks to generate an image, icon, illustration, UI mockup, or to
edit an existing image with a text instruction.

## When NOT to Use

- Do not use for photos of real people without consent.
- Do not use to imitate a living artist's style or a trademarked character.
- Do not use for text-heavy assets where exact typography matters; render text
  separately with `canvas-design`.
- Do not use if the user has not configured Gemini.

## Prerequisites

- Gemini API key in Integrations or `GEMINI_API_KEY`.
- `run_skill_script` with `skill="imagen"` and `script="imagen.py"`.
- `httpx` is installed. The script returns an actionable error if the key is
  missing.

## Step Pattern

1. If available, call `integration_status("gemini")`; stop and ask the user to
   configure it if missing.
2. Write a precise prompt: subject, composition, lighting, palette, style,
   aspect ratio, and what to avoid. Do not rely on one vague adjective.
3. Call `run_skill_script`:
   - generate: `{"operation":"generate","prompt":"...","aspect_ratio":"1:1","output_path":"C:\\...\\image.png"}`
   - edit: add `"input_path":"C:\\...\\source.png"` and describe the change.
4. Verify the file exists and its byte size is non-zero. Preview it when the
   user asked to see it.
5. Report the model, path, and any limitation (cropped, low resolution, text
   artifacts).

## Spec Detail

- `model` defaults to `gemini-2.5-flash-image`.
- `aspect_ratio` is optional (`1:1`, `16:9`, `9:16`, `4:3`, `3:4`).
- `output_path` gets an image extension automatically.
- `timeout` defaults to 120 seconds.

## Output Shape

Returns `success`, `output`, `path`, and `data` with MIME type, bytes, and
model.

## Recovery

- HTTP 401/403: invalid key; ask the user to re-enter it.
- Safety block: explain that the prompt was rejected and offer a safer rewrite.
- No image part in the response: report the text reason; do not save a blank
  file.
- Quota exceeded: report the limit and suggest retrying later.
