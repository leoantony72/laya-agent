---
name: slack-gif
skill: playbook_slack_gif
kind: playbook
category: creative-media
source: https://github.com/ComposioHQ/awesome-claude-skills
license: see upstream LICENSE.txt
args: []
description: >
  Create an animated GIF sized for Slack messages or emoji. Use when the user
  asks for a GIF, animated emoji, or a short looping reaction.
---

# Slack GIF Creator

## When to Use

Use when GOAL asks for an animated GIF for Slack, a custom emoji, or a short
text/logo animation.

## When NOT to Use

- Do not use for video files or APNG.
- Do not use copyrighted characters or someone's face without consent.
- Do not promise Slack's exact compression; validate the byte size.
- Do not use for a static image; use `canvas-design`.

## Slack Constraints

| Use | Max size | Size | FPS | Colors | Duration |
|---|---:|---:|---:|---:|---:|
| Message GIF | ~2 MB | 480×480 | 15–20 | 128–256 | 2–5 s |
| Emoji GIF | 64 KB | 128×128 | 10–12 | 32–48 | 1–2 s |

Emoji is the hard case: keep it simple, 10–15 frames, flat colors, no gradients.

## Prerequisites

- `run_skill_script` with `skill="slack-gif"` and `script="gif_builder.py"`.
- Pillow is installed.

## Step Pattern

1. Decide message vs emoji and the exact canvas size.
2. Choose one motion primitive: `pulse`, `shake`, `slide`, `zoom`, `spin`, or
   `bounce`. One idea per GIF.
3. Call `run_skill_script`:
   - message: `{"operation":"build","output_path":"C:\\...\\wave.gif","width":480,"height":480,"frames":18,"duration":80,"colors":128,"background":"#1f2937","animation":{"type":"slide","text":"Hello","color":"#ffffff","size":96}}`
   - emoji: same with `"width":128,"height":128,"emoji":true,"frames":12,"colors":40`.
   - image sequence: use `"image_paths":["a.png","b.png"]` instead of `animation`.
4. The script validates the byte limit and retries with fewer colors/frames.
   If it still fails, reduce the canvas or text size.
5. Verify the returned `bytes` value against the table. Preview the GIF when
   the user asked to see it.
6. Report size, dimensions, frames, and duration.

## Output Shape

Returns `success`, `path`, and `data` with `bytes`, `frames`, `width`, and
`height`.

## Recovery

- Above 64 KB for an emoji: reduce to 32 colors, 10 frames, 128×128.
- Text looks jagged: use a bold system font and a higher base size.
- Motion looks chaotic: one primitive only; remove extra elements.
- GIF does not loop: `loop` defaults to 0 (infinite); check the spec.
