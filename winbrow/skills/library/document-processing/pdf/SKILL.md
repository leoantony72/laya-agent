---
name: pdf
skill: playbook_pdf
kind: playbook
category: document-processing
source: https://github.com/anthropics/skills/tree/main/skills/pdf
license: proprietary-reference-only
args: []
description: >
  Read, create, merge, split, rotate, watermark, or encrypt PDF files. Use
  whenever a .pdf is the input or the deliverable.
---

# PDF Documents

## When to Use

Use for any PDF task: extracting text, merging or splitting files, rotating or
watermarking pages, creating a PDF from text, or adding password protection.

## When NOT to Use

- Do not use for Word, Excel, PowerPoint, EPUB, or image output.
- Do not use OCR for a text PDF; `read_document` already extracts text.
- Do not use an external website or a browser print dialog for a local PDF
  operation this skill can perform.
- Do not claim a page count or text extraction without reading the result.

## Prerequisites

- Text reading: `read_document` (T0, uses MarkItDown/pdfminer).
- PDF operations: `run_skill_script` with `skill="pdf"` and
  `script="pdf_tool.py"`.
- The helper uses the pure-Python `pypdf` + `fpdf2` stack installed by
  `requirements.txt`. It does not need PyMuPDF.

## Step Pattern

1. Locate the file with `find_files` if the path is unknown. Never scan a
   whole drive.
2. For reading, call `read_document(path, max_chars=40000)` once. Continue
   with `offset`/`next_offset` only when the result is truncated.
3. For an operation, choose one spec and call `run_skill_script`:
   - create: `{"operation":"create","output_path":"...","title":"...","content":"..."}`
   - merge: `{"operation":"merge","inputs":["a.pdf","b.pdf"],"output_path":"merged.pdf"}`
   - split: `{"operation":"split","path":"in.pdf","pages":"1-3,5","output_dir":"..."}`
   - rotate: `{"operation":"rotate","path":"in.pdf","pages":"1-2","angle":90,"output_path":"out.pdf"}`
   - watermark: `{"operation":"watermark","path":"in.pdf","text":"CONFIDENTIAL","output_path":"out.pdf"}`
   - encrypt: `{"operation":"encrypt","path":"in.pdf","user_password":"...","output_path":"out.pdf"}`
   - extract_text: `{"operation":"extract_text","path":"in.pdf","pages":"1-2"}`
4. Verify: read the output with `read_document` or report the exact page
   count/path returned by the script. Never state that a merge or split worked
   without a successful result.
5. For encryption, never echo the password back in the final answer.

## Page Syntax

`pages` is 1-based and accepts ranges and commas, for example `1-3,7,9-11`.
An empty value or `all` means every page.

## Output Shape

The script returns `success`, `output`, `path`, and sometimes `paths` or
`count`. `read_document` returns a FILE/FORMAT/CHARS/RANGE header plus text.

## Recovery

- Import error: report the exact error and stop. Do not retry `pip install`
  in a loop; PyMuPDF is not required by this skill.
- Encrypted input: ask for the password; do not attempt to crack it.
- Corrupt PDF: report the exact error and ask for another copy.
- Output path is open in a viewer: ask the user to close it and retry once.
- Split produced zero files: check the page range against the reported page
  count.
- Never overwrite the input unless the user explicitly asked for in-place
  editing.
