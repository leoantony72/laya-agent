---
name: pptx
skill: playbook_pptx
kind: playbook
category: document-processing
source: https://github.com/anthropics/skills/tree/main/skills/pptx
license: proprietary-reference-only
args: []
description: >
  Create, read, or extend PowerPoint .pptx decks. Use when slides, a pitch
  deck, or a presentation is the input or deliverable.
---

# PowerPoint Decks

## When to Use

Use when GOAL mentions a `.pptx`/`.potx` file, a deck, slides, a presentation,
or a pitch. Also use when extracting text and speaker notes from an existing
deck.

## When NOT to Use

- Do not use for Word documents, spreadsheets, PDFs, or HTML pages.
- Do not use for a one-line chat answer that does not need slides.
- Do not claim a visual theme or layout was applied unless the script did it.
- Do not try to render thumbnails on this machine; inspect text instead.

## Prerequisites

- Reading: `read_document` for a quick Markdown outline, or the script's
  `read` operation for shape-level text.
- Creating/extending: `run_skill_script` with `skill="pptx"` and
  `script="pptx_tool.py"`.
- The helper uses `python-pptx`, which is already installed.

## Step Pattern

1. If an existing deck is the source, read it first with `read_document` or
   `{"operation":"read","path":"..."}` and keep the outline in HISTORY.
2. Choose a clear deck outline: one idea per slide, at most five bullets per
   slide, no walls of text.
3. Call `run_skill_script`:
   - create: `{"operation":"create","output_path":"...","title":"Deck title","subtitle":"Optional","slides":[{"title":"Problem","bullets":["...","..."]},{"title":"Plan","bullets":["..."],"notes":"Speaker note"}]}`
   - add: `{"operation":"add_slide","path":"...","title":"Next","bullets":["..."]}`
   - read: `{"operation":"read","path":"..."}`
4. Verify with `{"operation":"read","path":"OUTPUT"}` and confirm the expected
   slide titles exist. Report the path and slide count.
5. If the user asked for a specific brand or theme, say which colors/fonts
   were not applied; do not invent a theme.

## Spec Detail

- `slides` is a list of objects with `title`, `bullets`, and optional `notes`.
- `bullets` may be a list or a newline-separated string.
- `output_path` gets a `.pptx` extension automatically.
- The first slide is a title slide only when `title` is provided.

## Output Shape

Returns `success`, `output`, `path`, and `count` (slides created). The `read`
operation returns slide-by-slide text.

## Recovery

- File is open in PowerPoint: ask the user to close it and retry once.
- Empty slide list: create a title plus one overview slide, or ask for the
  outline if GOAL does not supply one.
- Text overflows: reduce bullets and recreate the slide; never claim it fits
  without checking.
- Never repeat a successful deck creation with identical arguments.
