---
name: docx
skill: playbook_docx
kind: playbook
category: document-processing
source: https://github.com/anthropics/skills/tree/main/skills/docx
license: proprietary-reference-only
args: []
description: >
  Create, edit, or analyze Word .docx files. Use only when a .docx is the
  input or the requested deliverable.
---

# DOCX Documents

## When to Use

Use when GOAL names a `.docx` or `.dotx` file, or asks for a Word report,
memo, letter, contract, or formatted document. Also use when extracting or
reorganizing content from an existing Word file.

## When NOT to Use

- Do not use for PDF, spreadsheet, slide, plain-text, or Markdown output.
- Do not use when the user only wants a short chat answer.
- Do not use for images, scans, or OCR.
- Do not use `run_command` with hand-written Word XML when this skill's script
  can do the job.

## Prerequisites

- Reading an existing file: use `read_document` (T0).
- Creating or editing: call `run_skill_script` with `skill="docx"` and
  `script="docx_tool.py"`.
- The helper needs `python-docx`. If the script reports it missing, tell the
  user to run `pip install python-docx` from the Integrations panel or a
  terminal. Do not claim the file was created.

## Step Pattern

1. If the user supplied an existing file, call `read_document(path,
   max_chars=40000)` once and use the returned Markdown as source material.
2. Decide the exact output path. Prefer Desktop or Documents unless GOAL gives
   another folder. Never invent a path that was not requested.
3. Build one JSON spec and call `run_skill_script`:
   - create: `{"operation":"create","output_path":"C:\\Users\\me\\Desktop\\report.docx","title":"...","content":"# Heading\\n\\nParagraph","tables":[{"headers":["A","B"],"rows":[["1","2"]]}]}`
   - append: `{"operation":"append","path":"...","content":"..."}`
   - replace: `{"operation":"replace","path":"...","old":"old text","new":"new text"}`
4. Read the result back with `read_document` and confirm the title and key
   sections exist. Only then report success and the exact path.
5. If layout matters, tell the user which styles were applied instead of
   claiming visual fidelity that was not verified.

## Spec Detail

- `content` accepts Markdown-like lines: `#`, `##`, `###`, `-` bullets,
  numbered lines, and blank lines.
- `content_path` reads the body from a UTF-8 file instead of `content`.
- `tables` is optional; each table has `headers` and `rows`.
- `title` and `subtitle` are optional and are centered on the first page.

## Output Shape

The script returns `success`, `output`, `path`, and `count` where relevant.
`read_document` returns the extracted text for verification.

## Recovery

- Missing `python-docx`: report the install command and stop.
- File is open in Word and cannot be written: ask the user to close it, then
  retry once with the same path.
- Content is empty: ask for the source text rather than creating a blank file.
- A write succeeded but read-back fails: report the path and the verification
  error; do not claim the document is correct.
- Never repeat a successful create with identical arguments.
