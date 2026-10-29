---
name: html-ppt
skill: playbook_html_ppt
kind: playbook
category: document-processing
source: https://github.com/lewislulu/html-ppt-skill
license: MIT (see LICENSE)
args: []
description: >
  Build styled HTML slide decks with themes, layouts, animations, notes, and PDF export.
---

# HTML PPT Studio

## When to Use

Use for a presentation, PPT, deck, keynote, slideshow, tech talk, pitch, product
launch, course module, weekly report, or a visual multi-slide document. This is
the default for beautiful, animated, keyboard-navigable decks and for decks
that will be exported to PDF.

Use `pptx` instead only when a real editable `.pptx` file is required (the user
must edit it in PowerPoint or supplied a `.pptx` template).

## When NOT to Use

- Do not use for an editable `.pptx`/`.potx` deliverable; use `pptx`.
- Do not use for a single static poster or image; use `canvas-design`.
- Do not use for PDF forms, merging, or text extraction; use `pdf`.
- Do not use when the user only needs a short chat answer.

## What You Get

- 36 themes (`assets/themes/*.css`) — business, editorial, technical, dark,
  pastel, cyber, academic, magazine.
- 36 layout pages (`templates/single-page/*.html`) — cover, two-column, KPI
  grid, charts, timeline, roadmap, comparison, code, terminal, images, quotes.
- 15 complete deck templates (`templates/full-decks/<name>/`) — pitch-deck,
  product-launch, tech-sharing, weekly-report, course-module, presenter-mode,
  xhs styles, and extracted real-world decks.
- 27 CSS entry animations plus 20 canvas FX (`assets/animations/`).
- Keyboard runtime: ←/→ navigate, `T` themes, `A` animation, `F` fullscreen,
  `S` presenter mode, `N` notes drawer, `O` overview, `#/N` deep links.
- Presenter mode with current/next previews, speaker script, and timer.

All local. Only webfonts fall back to system fonts when offline.

## Before You Author — Ask Three Things

Do not write slides until these are clear (ask, or propose a tasteful default
and confirm):

1. **Content/audience** — topic, slide count, who is watching.
2. **Theme** — suggest 2–3 based on tone:
   - Business/investor: `pitch-deck-vc`, `corporate-clean`, `swiss-grid`
   - Tech/engineering: `tokyo-night`, `dracula`, `catppuccin-mocha`,
     `terminal-green`, `blueprint`
   - Editorial/report: `academic-paper`, `editorial-serif`, `minimal-white`
   - Bold/launch: `cyberpunk-neon`, `vaporwave`, `y2k-chrome`, `neo-brutalism`
   - Soft/social: `xiaohongshu-white`, `soft-pastel`, `rainbow-gradient`
3. **Starting point** — a full-deck template or a blank deck.

## Step Pattern

1. Scaffold from the closest template:

   ```json
   {"name":"q3-review","output_parent":"C:\\Users\\me\\Desktop",
    "template":"pitch-deck"}
   ```

   `run_skill_script(skill="html-ppt", script="new_deck.py", spec=<JSON>)`.
   It copies the template, rewrites every asset path for the output depth, and
   verifies all local references. `template` accepts `deck`, a full-deck name,
   a single-page layout name, or an absolute `.html`/folder path.

2. Open `index.html` with `open_file(index_html)` and confirm it loads.
   Press `T` to cycle themes, `O` for the overview, arrows to navigate.

3. Replace the demo content with the user's material, one
   `<section class="slide">` per logical page. **Edit the scaffolded
   `index.html` in place** (or use `mcp_filesystem_edit_file` for exact
   replacements). Do NOT replace the whole file with hand-written HTML:
   keep the `<head>` asset links (`assets/fonts.css`, `assets/base.css`,
   the theme/`style.css`, `assets/animations/animations.css`), the
   `<body class="tpl-*">` theme class, the `<div class="deck">` wrapper, and
   `assets/runtime.js`. Start from a layout in `templates/single-page/`;
   do not invent markup from scratch.

4. Put presenter-only text inside `<div class="notes">…</div>`, never as
   visible slide text. Press `S` to check the presenter view and script.

5. Verify: run `validate_deck.py` (or the export step, which repairs common
   asset loss), then open the deck, navigate all slides, and check for
   overflow/clipped text, missing images, and console errors.
   ```json
   {"input":"C:\\Users\\me\\Desktop\\q3-review\\index.html","repair":true}
   ```

6. Export to PDF when the user wants a shareable/printable file:

   ```json
   {"input":"C:\\Users\\me\\Desktop\\q3-review\\index.html",
    "output":"C:\\Users\\me\\Desktop\\q3-review\\q3-review.pdf",
    "format":"pdf","page":"16:9","repair":true,
    "layout_check":true,"validate":true}
   ```

   `run_skill_script(skill="html-ppt", script="render_deck.py", spec=<JSON>)`
   uses installed Chrome/Edge. Deck PDFs are **16:9 (13.333 × 7.5 in)** by
   default — the same geometry as a PowerPoint widescreen slide. Never export
   a deck as A4/Letter unless the user explicitly wants a paper version.
   For PNG thumbnails, `{"format":"png","slides":"all"}`.

## Authoring Rules

- **Start from a template.** Copy a layout, then replace content.
- **Use tokens, not literal colors.** `var(--text-1)`, `var(--accent)`,
  `var(--accent-ink)`. Never hard-code `#111` in slides.
- **One `.slide` per page.** Only one is visible; `runtime.js` handles the rest.
- **Include the runtime** (`assets/runtime.js`) and the theme link so keyboard
  navigation, `T`, `S`, `O`, and print/PDF page painting work.
- **Images:** start from an `image-*` layout and wrap in `.img-frame`
  (`.img-frame.contain` for screenshots/diagrams). Never drop a bare `<img>`
  into a slide.
- **No presenter text on slides.** Use `.notes`.
- **Add a logo once** with `data-logo` on `<body>`; skip slides with
  `data-no-logo`.
- **Never hand-edit `../` asset depth.** Scaffold with `new_deck.py`; it
  computes and verifies paths.
- **Keep content legible:** one idea per slide, title 32–40 pt equivalent, body
  16–20 pt, no walls of text; move detail to notes.
- **PDF geometry:** decks export at 16:9 (13.333 × 7.5 in). Do not switch to
  A4/Letter for a deck unless the user asks for a printable paper version.
- **Print colors:** dark themes must keep their background in PDF. Use theme
  tokens and never lighten text on an unset/white background.
- **One slide, one page:** verify the PDF page count equals the slide count.

## References (load only when needed)

- `references/themes.md` — 36 themes and when to use them.
- `references/layouts.md` — 36 layout types.
- `references/animations.md` — CSS + canvas FX classes.
- `references/full-decks.md` — 15 complete deck templates.
- `references/presenter-mode.md` — speaker script rules and presenter view.
- `references/authoring-guide.md` — full workflow and export notes.

## Output Shape

`new_deck.py` returns the `index.html` path, the asset prefix, and the number of
verified references. `render_deck.py` returns the PDF path (or `paths` for PNG
slides). Both print one JSON object for `run_skill_script`.

## Recovery

- Scaffold or export reports unresolved references: the export step repairs
  missing `base.css`/theme/`runtime.js` and wraps stray slides in `.deck`. If
  errors remain, re-scaffold from the template; do not hand-edit `../` depth.
- Browser does not open: report the absolute `index.html` path; the user can
  open it manually.
- PDF export says Chrome/Edge not found: set `WINBROW_BROWSER` to the browser
  executable path.
- A slide is blank: check that it has `class="slide"`, the `.deck` wrapper, and
  the runtime script.
- Presenter view (`S`) does nothing: the deck must load `assets/runtime.js`.
- Text overflows in print: reduce content or move detail to `.notes`; the
  fallback CSS and print rules preserve slide geometry, but cannot shrink an
  overfull slide. Verify by opening the exported PDF, not just the browser.
- PDF has the wrong page count or size: run `render_deck.py` with
  `"page":"16:9","validate":true`; it fails instead of reporting a bad PDF.
- Light text on a white PDF: the deck lost `base.css`/the theme. Run the export
  (it repairs missing links) or re-scaffold; never ship the unstyled PDF.
