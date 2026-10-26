---
name: canvas-design
skill: playbook_canvas_design
kind: playbook
category: creative-media
source: https://github.com/ComposioHQ/awesome-claude-skills
license: see upstream LICENSE.txt
args: []
description: >
  Create a poster, cover, or static art piece as PNG or PDF. Use when the user
  wants an original visual design with a clear aesthetic direction.
---

# Canvas Design

## When to Use

Use when GOAL asks for a poster, cover, social image, printable art, or a
static visual composition as PNG/PDF.

## When NOT to Use

- Do not copy an existing artist's work or a trademarked character.
- Do not use for photo editing; use `image-enhancer`.
- Do not use for animated GIFs; use `slack-gif`.
- Do not promise print-perfect CMYK output; this renders RGB.

## Prerequisites

- `run_skill_script` with `skill="canvas-design"` and
  `script="render_canvas.py"`.
- Pillow is installed. The script uses Windows system fonts; no font pack is
  bundled.

## Step Pattern

1. **Write a short design philosophy** (3–6 lines): mood, form, color logic,
   typography, and what the viewer should feel. Do not start with a template.
2. Choose a canvas: poster 1200×1600, square 1080×1080, story 1080×1920, or a
   custom size.
3. Compose in layers: background, large shape, image/graphic, headline, accent
   text, fine detail. Keep text to about 10% of the area.
4. Call `run_skill_script` with a page spec:
   `{"operation":"render","output_path":"C:\\...\\poster.png","width":1200,"height":1600,"background":"#0f172a","elements":[{"type":"rect","x":80,"y":80,"w":1040,"h":1440,"outline":"#f8fafc","width":3},{"type":"text","text":"TITLE","x":120,"y":240,"size":120,"font":"display","color":"#f8fafc"},{"type":"text","text":"Subtitle line","x":124,"y":400,"size":32,"font":"sans","color":"#94a3b8"}]}`
5. For multiple pages, use `pages` instead of `elements`; PDF output supports
   all pages, PNG output writes one file per page.
6. Verify the file exists and reports the expected dimensions. Preview it when
   the user asked to see it.
7. Iterate by changing one layer at a time; do not rebuild from scratch.

## Element Types

- `rect`, `ellipse`: `x`, `y`, `w`, `h`, `fill`, `outline`, `width`.
- `line`: `x1`, `y1`, `x2`, `y2`, `fill`, `width`.
- `text`: `text`, `x`, `y`, `size`, `font` (`sans`/`serif`/`mono`/`display`),
  `color`, `max_width`, `line_height`.
- `image`: `path`, `x`, `y`, `w`, `h`.

## Output Shape

Returns `success`, `path` or `paths`, and `count` (pages).

## Recovery

- Text overflows: reduce size or set `max_width` to wrap.
- Font missing: the script falls back to a system default; say so.
- Output is blank: check that elements have coordinates and a visible color.
- User wants a different size: rerender; do not stretch the PNG afterward.
