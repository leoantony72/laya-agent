---
name: pptx
skill: playbook_pptx
kind: playbook
category: document-processing
source: https://github.com/anthropics/skills/tree/main/skills/pptx
license: See LICENSE.txt (upstream terms, reference only)
args: []
description: >
  Create or edit editable .pptx decks with layouts, tables, images, and notes.
---

# PowerPoint Presentations

## When to Use

Use when the user needs a real, editable `.pptx` — a deck they will edit in
PowerPoint, a supplied `.pptx`/`.potx` template, or an Office deliverable.

**Format decision:** for a designed, animated, presenter-ready deck or a PDF
handout, prefer `html-ppt`. Use this skill when the deliverable must be
`.pptx`.

## When NOT to Use

- Do not use for an HTML deck or a PDF-only presentation; use `html-ppt`.
- Do not use for Word/PDF/spreadsheet output.
- Do not use `pptxgenjs`/Node instructions; WinBrow uses `python-pptx` through
  `run_skill_script`.

## Prerequisites

`run_skill_script(skill="pptx", script="pptx_tool.py", spec={...})`.
The engine auto-fits text, measures tables, preserves image aspect ratios,
supports native charts, and validates slide geometry.

## Step Pattern

1. Decide whether the deliverable must be an editable `.pptx` (this skill) or a
   designed HTML/PDF deck (`html-ppt`).
2. Outline the story: one idea per slide, 8–14 slides, a clear arc.
3. Pick a theme and aspect (`16:9` or `4:3`).
4. Build the slide spec and resolve images (`path`) or let `query` fetch a
   licensed image.
5. Create the deck, then inspect every validation preview.
6. Fix overflow, table, image, or overlap issues and regenerate.
7. Report the path only after the slides have been inspected.

## Create a Deck

```json
{"operation":"create","output_path":"C:\\Users\\me\\deck.pptx",
 "title":"Q3 Review","subtitle":"Growth, risks, next steps","author":"WinBrow",
 "theme":"midnight","aspect":"16:9","footer":"Confidential",
 "slides":[
   {"type":"title","title":"Q3 Review","subtitle":"Growth, risks, next steps"},
   {"type":"bullets","title":"What changed",
    "bullets":["Revenue grew 18%","Enterprise drove 70%"],
    "notes":"Pause for questions."},
   {"type":"table","title":"Revenue by region",
    "headers":["Region","Revenue","Growth"],
    "rows":[["North","$4.2M","18%"],["South","$3.1M","12%"]]},
   {"type":"image","title":"Product",
    "images":[{"query":"SaaS dashboard","caption":"Product surface"}]},
   {"type":"chart","title":"Growth","chart_type":"bar",
    "categories":["Q1","Q2","Q3"],
    "series":[{"name":"Revenue","values":[2.1,3.2,4.2]}]},
   {"type":"closing","title":"Thank you","subtitle":"Questions?"}
 ]}
```

### Fields

| Field | Values |
|---|---|
| `theme` | `midnight`, `corporate`, `editorial`, `minimal`, `forest` |
| `aspect` | `16:9` (13.333×7.5 in) or `4:3` (10×7.5 in) |
| `footer`, `author`, `title` | text |
| `colors` | override palette keys (`bg`, `surface`, `text`, `muted`, `accent`, `accent2`) with hex |
| `validate` | default true; structural checks + slide previews |
| `strict` | fail if validation reports errors |

### Slide types

- `title` — title + subtitle + author.
- `section` — full-width accent band + section title.
- `bullets` — auto-fit body text; splits into continuation slides when needed.
- `two_column` — `left`/`right` lists with optional column titles.
- `table` — measured columns/rows; repeats the header when rows continue.
- `image` / `images` — one image or a 2-column gallery; caption + credit.
- `chart` — native editable `bar`/`column`/`line`/`pie`/`doughnut` charts.
- `diagram` — horizontal process boxes with arrows (`steps`).
- `quote` — centered quote + attribution.
- `closing` — final slide.

Speaker notes: `"notes": "..."` on any slide.

## Images

Images can be local (`"path"`) or web-sourced (`"query"`):

```json
{"type":"image","title":"Regional footprint",
 "images":[{"query":"regional map","caption":"Coverage map"}]}
```

`query` uses Wikimedia Commons and returns only reusable public-domain/CC
images with a credit line; the deck includes the credit. If nothing suitable is
found, the operation fails rather than inserting a placeholder. To inspect
candidates first:

```json
{"operation":"find_image","query":"data center","count":4,
 "output_dir":"C:\\Users\\me\\images"}
```

Images are scaled proportionally, centered in their frame, and kept inside the
content area. Charts/visuals are preferred over decorative stock photos.

## Other Operations

| `operation` | Spec |
|---|---|
| `add_slide` | `path`, `title`, `bullets`, `notes` |
| `add_table` | `path`, `slide`, `rows`/`headers`, optional geometry |
| `add_image` | `path`, `slide`, `image_path`, optional geometry |
| `set_notes` | `path`, `slide`, `notes` |
| `delete_slide` | `path`, `slide` (1-based) |
| `read` | `path` — text, tables, notes |
| `find_image` | `query`, `output_dir`, `count` |
| `validate` | `path`, optional `aspect`, `render_dir` |

## Layout Rules

- One idea per slide; bullets split automatically rather than shrinking below
  readability.
- Tables get measured column widths and row heights; header row is styled and
  repeated if rows continue.
- Text boxes use a consistent grid (0.58 in margins) and auto-fit within their
  box. Titles do not overlap the body.
- Every slide has a footer line, footer text, and slide number (except title/
  section/quote/closing).
- Short bullet/two-column content is vertically centered; tables are centered
  in the content area; images are centered and captioned.
- Native charts keep editable data; no chart images.

## Validation (mandatory)

`validate: true` returns `validation.ok`, errors, warnings, and slide previews
under `<name>_slides/`.

- Checks: slide size/aspect, shapes outside the slide, text taller than its box,
  distorted images, overlapping content, empty slides.
- Preview renderer: WinBrow has no PowerPoint/LibreOffice in this environment,
  so it uses a built-in PIL renderer of the actual shape geometry. Charts are
  simplified in the preview; this limitation is reported. Open the `.pptx` in
  PowerPoint for final confirmation when available.
- Inspect every preview for clipped/overlapping text, bad image crops, and
  unintended empty space. Fix the spec and regenerate.
- Use `{"operation":"validate","path":"...pptx","aspect":"16:9"}` to re-check.

Generation alone is not acceptance; the rendered slides must be inspected.

## Acceptance Criteria

- Correct 16:9/4:3 dimensions.
- No clipped or overflowing text; no shapes outside the slide.
- Tables aligned with repeated headers; no malformed borders.
- Images keep aspect ratio, stay inside frames, have captions/credits.
- Native charts render with readable labels and a consistent palette.
- Consistent margins, typography, colors, and slide numbers.
- No accidental overlap or empty slides; intentional whitespace is balanced.
- Notes present where requested.

## Recovery

- Validation reports text overflow: shorten the text, split the slide, or reduce
  the table rows; do not deliver with clipping.
- Image query finds nothing: use a local image or a diagram; never a broken or
  unrelated image.
- A slide index is invalid: `read` the deck and use a valid 1-based number.
- SVG/EMF image fails: convert to PNG/JPG first.
- No PowerPoint/LibreOffice for rendering: previews are approximate; disclose
  that limitation and open in PowerPoint when available.
