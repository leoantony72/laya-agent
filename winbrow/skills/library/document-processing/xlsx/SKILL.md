---
name: xlsx
skill: playbook_xlsx
kind: playbook
category: document-processing
source: https://github.com/anthropics/skills/tree/main/skills/xlsx
license: proprietary-reference-only
args: []
description: >
  Create, edit, analyze, or chart Excel .xlsx and CSV files. Use when a
  spreadsheet is the input or deliverable.
---

# Excel Workbooks

## When to Use

Use when GOAL names an `.xlsx`, `.xlsm`, `.xltx`, `.csv`, or `.tsv` file and
wants it opened, read, cleaned, edited, charted, or created. Also use when the
deliverable must be a spreadsheet.

## When NOT to Use

- Do not use when the deliverable is a Word document, slide deck, PDF, or HTML
  report, even if the source data is tabular.
- Do not use for a quick chat summary that never needs a file.
- Do not use for Google Sheets or a database pipeline.
- Do not use `read_document` alone to plan cell edits; it does not expose cell
  coordinates.

## Prerequisites

- Reading: `read_document` for a quick sheet outline.
- Creating/editing: `run_skill_script` with `skill="xlsx"` and
  `script="xlsx_tool.py"`.
- `openpyxl` and `pandas` are already installed.

## Step Pattern

1. If the workbook exists, read it first with `read_document` or
   `{"operation":"read","path":"...","sheet":"Sheet1"}`. Never edit from an
   assumed layout.
2. Build one spec and call `run_skill_script`:
   - create: `{"operation":"create","output_path":"...","sheets":[{"name":"Sales","headers":["Month","Revenue"],"rows":[["Jan",1200],["Feb",1500]]}]}`
   - append: `{"operation":"append_rows","path":"...","sheet":"Sales","rows":[["Mar",1700]]}`
   - cells: `{"operation":"set_cells","path":"...","sheet":"Sales","cells":[{"cell":"C2","value":"=B2*1.1"}]}`
   - chart: `{"operation":"add_chart","path":"...","sheet":"Sales","chart_type":"bar","data_range":"B2:B12","categories_range":"A2:A12","title":"Revenue"}`
   - profile: `{"operation":"profile","path":"...","sheet":"Sales"}`
   - read: `{"operation":"read","path":"...","sheet":"Sales","max_rows":50}`
3. Verify the result with `read_document` or the `read` operation. Check that
   the expected headers and row count are present.
4. Report the path and a short summary. Do not dump every row into chat.

## Spec Detail

- `rows` are appended in order after any existing data.
- A string beginning with `=` is written as a formula.
- `data_range` and `categories_range` use Excel notation, for example
  `B2:B12`.
- `chart_type` is `bar`, `line`, or `pie`.
- `sheet` defaults to the first sheet.

## Output Shape

Returns `success`, `output`, `path`, and `count` where relevant. `profile`
returns row/column counts, dtypes, missing counts, numeric summaries, and top
values.

## Recovery

- Sheet name is wrong: call `read` or `profile` to list actual sheets, then
  retry with an exact name.
- Formula returns a cached error: report it; do not silently replace the
  formula with a guessed value.
- Workbook is locked: ask the user to close Excel and retry once.
- A large CSV should be summarized, not loaded entirely into chat.
