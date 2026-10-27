---
name: docx
skill: playbook_docx
kind: playbook
category: document-processing
source: https://github.com/anthropics/skills/tree/main/skills/docx
license: See LICENSE.txt (upstream terms, reference only)
args: []
description: >
  Create or edit Word .docx files with tables, TOC, comments, and read-back checks.
---

# DOCX Documents

## When to Use

Use when GOAL names a `.docx`/`.dotx` file, or asks for a Word report, memo,
letter, contract, proposal, or formatted document. Also use when reading,
restructuring, commenting on, or replacing content in an existing Word file.

## When NOT to Use

- Do not use for PDF, spreadsheet, slide, plain-text, or Markdown output.
- Do not use for a short chat answer that does not need a file.
- Do not use `run_command` with hand-written Word XML when the bundled tool can
  do the job.
- Do not use docx-js/Node instructions from other skill collections. WinBrow
  uses `python-docx` through `run_skill_script`, which is deterministic here.

## Prerequisites

- Read an existing file with `read_document(path, max_chars=40000)` (T0).
- Run the bundled helper with:
  `run_skill_script(skill="docx", script="docx_tool.py", spec={...})`.
- The helper needs `python-docx`. If it reports the package missing, give the
  exact `pip install python-docx` command and stop. Never claim a file exists
  when the script failed.
- For visual checks, `open_file(path)` in Word. Do not claim a layout looks
  correct unless you read the document back or the user confirms.

## Step Pattern

1. If the user supplied a source file, read it once and use that Markdown as
   source material. Do not re-read it after a successful read.
2. Choose the exact output path (Desktop/Documents unless GOAL names one).
   Never invent a path.
3. Plan the structure before writing: title, headings, TOC, page numbers,
   tables, images, and a header/footer when appropriate.
4. Create or edit with one JSON spec per call (see Spec Detail).
5. Verify with `read_document` and `operation="inspect"`: confirm the title,
   key headings, table count, and page count are present. Only then report
   success and the exact path.
6. If the user asks for visual polish, open the file in Word and say what was
   applied. Do not invent pixel-level claims.

## Spec Detail

Common fields: `path` or `output_path`, `title`, `subtitle`, `content`
(Markdown-like lines), `content_path`, and `tables`
(`[{"headers":[...],"rows":[[...]]}]`).

| `operation` | What it does |
|---|---|
| `create` | New document. Optional `toc: true`, `page_numbers: true`, `header`, `footer`, `page_size` (`letter`/`a4`), `orientation` (`portrait`/`landscape`) |
| `append` | Add content/tables; `page_break: true` starts a new page |
| `replace` | Replace `old` with `new` in paragraphs and table cells |
| `comment` | Add a Word comment: `anchor` (text to attach to), `comment`, optional `author`/`initials` |
| `toc` | Insert a Table of Contents field at the top (Word builds it on open/update) |
| `page_numbers` | Add centered page numbers to every section footer |
| `header_footer` | Set `header` and/or `footer` text on every section |
| `image` | Insert `image_path` with optional `width_inches` and `caption` |
| `inspect` | Return headings, paragraph/table/comment counts, and section info |
| `merge_runs` | Coalesce adjacent identically-formatted runs before `replace` (Word splits visible text across many runs) |

Examples:

```json
{"operation":"create","output_path":"C:\\Users\\me\\Desktop\\report.docx",
 "title":"Quarterly Report","toc":true,"page_numbers":true,
 "content":"# Summary\n\nRevenue grew 18%.\n\n## Risks\n\n- Supply chain",
 "tables":[{"headers":["Metric","Value"],"rows":[["Revenue","$4.2M"]]}]}
```

```json
{"operation":"comment","path":"C:\\Users\\me\\contract.docx",
 "anchor":"Fees & expenses cap","comment":"This cap is too low.","author":"WinBrow"}
```

```json
{"operation":"replace","path":"C:\\Users\\me\\memo.docx",
 "old":"old vendor name","new":"New Vendor Ltd"}
```

## Document Quality

- Use one clear hierarchy: title, section headings, body. Do not fake headings
  with bold body text.
- Professional reports: TOC when longer than ~3 pages, page numbers, consistent
  1-inch margins, and a header/footer with the document name or date.
- Tables: header row, short cell text, consistent numeric alignment. Prefer a
  table over comma-separated prose.
- Images: set an explicit width; add a caption paragraph when the image carries
  meaning.
- Keep body text readable: 10.5–12 pt equivalent, generous spacing, no walls of
  text. Split long sections with subheadings.
- Never insert literal bullet characters; the helper uses Word list styles.

## Editing Existing Documents

- Read first; replace only the text the user asked to change.
- Word stores a visible phrase across multiple runs. If `replace` reports 0
  matches, run `merge_runs` first, then retry once.
- `comment` anchors to the paragraph containing `anchor`; if no anchor is
  found, the helper reports it instead of guessing.
- Tracked changes are not supported by `python-docx`. If the user explicitly
  needs redlining, explain that this build creates clean edits/comments and
  offer a clean copy instead; do not hand-edit OOXML blindly.
- Never repeat a successful create with identical arguments.

## Recovery

- Missing `python-docx`: report the install command and stop.
- File open in Word and cannot be written: ask the user to close it, retry once.
- Empty content: ask for the source text instead of creating a blank document.
- Write succeeded but read-back fails: report the path and the verification
  error; do not claim the document is correct.
- A table or image is missing after creation: inspect the spec and rerun the
  single operation after fixing the field, not the whole document.
