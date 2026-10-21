---
name: read-document
skill: read_document
args:
  - name: path
    type: string
    required: true
    description: File name or absolute path of the document to read.
  - name: max_chars
    type: int
    required: false
    default: 30000
    description: Maximum characters to return, between 1000 and 40000.
  - name: offset
    type: int
    required: false
    default: 0
    description: Character offset for reading the next chunk of a long document.
description: >
  Read a local document and return its text as Markdown. Supports PDF, PPTX,
  DOCX, XLSX/XLS, CSV, Markdown, HTML, JSON, XML, EPUB and plain text. Use when
  the user asks what a file says or to summarize, search or extract from it.
  Do not use for images, browser pages or files that do not exist yet.
---

## When to Use

Use whenever GOAL refers to the contents of an existing local file: "read the
PDF", "what does this presentation say", "summarize the spreadsheet", "find the
total in the CSV", "show me the markdown notes". It converts the document to
Markdown with headings, lists and tables intact, so later steps can reason over
the text without opening an application.

## When NOT to Use

- Do not use it for images, screenshots or scanned pages; use screen-ocr after
  opening the image, or ask the user for a text version.
- Do not use it for web pages; use browser-read-page.
- Do not use it to create, edit or convert a file; use create-text-file,
  run-command or an app adapter.
- Do not guess a path. If find-files returned nothing, ask the user instead of
  trying a drive-wide scan.
- Do not use it for very large binaries; the tool rejects files over 100 MB.

## Step Pattern

1. If the exact path is unknown, find-files with the user's filename first.
2. Call read_document once with the exact `path` and `max_chars=40000` for a
   summary or full-text task. The returned text stays in HISTORY for the next
   step, so do not re-read the same file or overlapping ranges.
3. Read the returned Markdown. Answer the question or extract only the values
   GOAL asks for; do not dump the whole document back to the user.
4. Only if the output ends with `[TRUNCATED: ...]`, call read_document again
   with the returned `next_offset` until the needed content is covered.
5. For multi-file requests, read each file separately and cite each filename.
6. To rewrite or expand a summary, write the complete new text in ONE
   create-text-file or mcp_filesystem_write_file call. Do not use
   mcp_filesystem_edit_file for a whole-document rewrite.

## Args Detail

- `path` accepts an absolute path, a `~` path, or a filename found in the
  common user folders. Quote paths that contain spaces.
- `max_chars` defaults to 30000 and is clamped to 1000-40000. Use 40000 for a
  summary or full-text task; a smaller value only for a quick preview.
- `offset` defaults to 0. Use the `next_offset` value from a truncated result,
  never an arbitrary number.
- Supported formats: PDF, PPTX, DOCX, XLSX, XLS, CSV, TSV, Markdown, TXT, HTML,
  JSON, XML, EPUB, Jupyter notebooks, and other text-based files.

## Output Shape

The tool returns `success`, `output`, `path`, `format`, `chars`, `offset` and
`next_offset`. `output` starts with a FILE/FORMAT/CHARS/RANGE header, followed
by Markdown text. `next_offset` is null when the end of the document was read.

## Recovery

- File not found: re-run find-files with a fuzzy variant, then clarify with the
  folders searched. Never retry the same missing path.
- Unsupported or unreadable format: report the format and the error, and ask
  whether to open it in an app instead.
- No extractable text: the file may be scanned or image-only. Say so; do not
  invent content and do not silently OCR it.
- Dependency error: report that document support is missing and suggest
  `pip install "markitdown[pdf,docx,pptx,xlsx,xls]"`.
- If the document is huge, continue with `offset` rather than re-reading from 0.
