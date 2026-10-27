---
name: pdf
skill: playbook_pdf
kind: playbook
category: document-processing
source: https://github.com/anthropics/skills/tree/main/skills/pdf
license: See LICENSE.txt (upstream terms, reference only)
args: []
description: >
  Read, create, merge, split, watermark, fill PDF forms, and export HTML to PDF.
---

# PDF Documents

## When to Use

Use for any `.pdf` task: create a professional report, read/extract text or
tables, merge, split, rotate, watermark, encrypt/decrypt, fill a form, extract
images, or export an HTML deck/page to PDF.

## When NOT to Use

- Do not use for a Word, slide, spreadsheet, or plain-text deliverable.
- Do not use PyMuPDF/`fitz` as the generation engine. WinBrow uses pure-Python
  `fpdf2`, `pypdf`, and `pdfplumber`; PyMuPDF is only a validation fallback.
- Do not use qpdf/pdftk/poppler; they are not installed.

## Prerequisites

`run_skill_script(skill="pdf", script="pdf_tool.py", spec={...})`.

For complex forms read `references/forms.md`; for advanced pypdf recipes read
`references/reference.md`.

## Step Pattern

1. Inspect the input: for an existing PDF run `metadata`; for a new document
   decide the audience, page size, theme, and content outline.
2. Choose the operation. For a new report, build the `content` or `blocks`;
   for edits use `merge`, `split`, `rotate`, `watermark`, `fill_form`, etc.
3. Run the helper with one JSON spec. Resolve images before the call (`path`) or
   let `query` fetch a licensed image.
4. Read the returned validation result and open the rendered page PNGs.
5. Fix any overflow, table, or pagination issue and regenerate.
6. Report the exact path only after the pages have been inspected.

## Create a PDF

The layout engine measures every block before drawing it. It wraps long words
and URLs, splits long paragraphs across pages, repeats table headers, avoids
stranded headings, keeps images proportional, and reserves header/footer space
from the actual page size.

```json
{"operation":"create","output_path":"C:\\Users\\me\\report.pdf",
 "title":"Annual Report 2026","subtitle":"Regional performance",
 "author":"WinBrow","footer":"Confidential",
 "theme":"corporate","page_size":"a4","orientation":"portrait",
 "content":"# Summary\n\nText with a long URL https://example.com/a/very/long/path\n\n## Metrics\n\n| Region | Revenue |\n|---|---|\n| North | $4.2M |\n\n![A caption](photo.png)",
 "images":[{"query":"regional map","caption":"Regional footprint"}],
 "validate":true}
```

Fields:

| Field | Values |
|---|---|
| `title`, `subtitle`, `author`, `header`, `footer` | text |
| `theme` | `corporate`, `academic`, `minimal`, `editorial` |
| `page_size` | `a4`, `letter`, `legal` |
| `orientation` | `portrait`, `landscape` |
| `margins` | `[left,top,right,bottom]` in points, or an object |
| `page_numbers` | true/false (default true) |
| `content` / `content_path` | Markdown-ish text: `#` headings, `-` bullets, `|` tables, fenced code, `>` quotes, `![caption](path)` images |
| `blocks` | structured blocks (advanced): `heading`, `paragraph`, `bullet`, `numbered`, `table`, `image`, `code`, `quote`, `spacer`, `pagebreak` |
| `images` | list of `{path}` or `{query, caption, credit}`; appended at the end |
| `image_query` | one web image appended at the end |
| `image_position` | `end` (default) or `start` |
| `validate` | default true; renders every page to PNG and checks text bounds |
| `strict` | fail if validation reports errors |

### Web images

`{"query":"..."}` uses Wikimedia Commons and returns only public-domain/CC
images with a credit line. If no reusable image is found the operation fails
clearly; it never inserts a placeholder or a broken URL. You can also search
first:

```json
{"operation":"find_image","query":"mountain landscape","count":3,
 "output_dir":"C:\\Users\\me\\images"}
```

Use `path` when you already have an image. Local paths are resolved relative to
the output folder. Always keep the returned `credit` with the image.

### Themes and quality

- `corporate`: blue accent, sans-serif, clean tables.
- `academic`: serif, navy accent, wider line spacing.
- `editorial`: warm serif, terracotta accent.
- `minimal`: black/white, high contrast.

The engine uses measured text, not fixed coordinates. Headings stay with the
following block; paragraphs, code, quotes, and table rows split across pages
instead of being pushed whole to a new page. Images are scaled to fit, centered,
captioned, and credited.

## Other Operations

| `operation` | Spec |
|---|---|
| `merge` | `inputs` list, `output_path` |
| `split` | `path`, `pages`, `output_dir` |
| `rotate` | `path`, `pages`, `angle`, `output_path` |
| `watermark` | `path`, `text`, `font_size`, `output_path` |
| `encrypt` | `path`, `user_password`, `owner_password`, `output_path` |
| `extract_text` | `path`, `pages` |
| `metadata` | `path` |
| `tables` | `path`, `pages` |
| `fields` / `fill_form` | AcroForm list / fill (`fields`, `flatten`) |
| `images` | `path`, `pages`, `output_dir` |
| `from_html` | `path` (HTML deck/page), `output_path`, `page` (`16:9`/`letter`/`a4`/`auto`) |
| `find_image` | `query`, `output_dir`, `count` |
| `validate` | `path`, optional `margins`, `page_size`, `orientation`, `render_dir` |

## Validation Workflow (mandatory)

1. Create the PDF with `validate: true` (default). The result includes
   `validation.ok`, errors, warnings, and rendered page PNGs under
   `<name>_validation/`.
2. Inspect the rendered pages yourself for overflow, clipped text, broken
   tables, overlap, stray whitespace, and bad page breaks.
3. Fix the spec/content and regenerate. Use `validate` on an existing PDF:
   `{"operation":"validate","path":"...pdf","render_dir":"..."}`.
4. Repeat until `ok` is true and the pages look right.
5. Only then report the path. If a limitation remains, state it explicitly.

Automated checks flag text outside the page/content margins, wrong page size,
and blank pages. Visual inspection is still required for border/table aesthetics.

## Acceptance Criteria

- No text outside the content area; long URLs/words wrap.
- No clipped content or overlapping elements.
- Table borders align; header rows repeat across pages.
- Images decode, keep aspect ratio, stay inside their frame, and have captions/
  credits.
- No accidental large right/bottom whitespace; intentional margins remain.
- Correct page size/orientation (A4/Letter/legal, portrait/landscape).
- Paragraphs split across pages rather than leaving a heading stranded.
- The rendered pages were inspected; generation alone is not acceptance.

## Recovery

- Missing package: report the exact `pip install` command and stop.
- `find_image` finds nothing: use a local image or an intentional diagram; never
  insert a placeholder or unrelated stock photo.
- Validation reports overflow: reduce content, fix the block/table, or adjust
  margins; do not deliver with known clipping.
- Encrypted/corrupt input: ask for the password or a fresh copy.
- No form fields: the PDF is flat; do not attempt annotation filling.
