---
name: csv-summarizer
skill: playbook_csv_summarizer
kind: playbook
category: data-analysis
source: https://github.com/coffeefuelbump/csv-data-summarizer-claude-skill
license: see upstream repository
args: []
description: >
  Profile a CSV and optionally chart a numeric column. Use for quick data
  insight, quality checks, and visual summaries.
---

# CSV Summarizer

## When to Use

Use when GOAL asks what is in a CSV, wants summary statistics, data-quality
checks, top values, or a quick chart from a `.csv`/`.tsv` file.

## When NOT to Use

- Do not use for `.xlsx` workbooks; use the `xlsx` skill.
- Do not use for PDF or image tables; read those first.
- Do not use for a full statistical model or a dashboard.
- Do not claim a trend from a chart without checking the underlying rows.

## Prerequisites

- `run_skill_script` with `skill="csv-summarizer"` and
  `script="profile_csv.py"`.
- `pandas`, `matplotlib`, and `Pillow` are already installed.

## Step Pattern

1. Locate the file with `find_files` if needed.
2. Call `run_skill_script` with `{"operation":"profile","path":"..."}`.
3. Read the returned `data` object: rows, columns, dtypes, missing counts,
   numeric summaries, and top values. State the shape and the most important
   quality issue.
4. If the user asked for a chart, call the script again with:
   `{"operation":"chart","path":"...","x":"Month","y":"Revenue","chart_type":"bar","title":"Revenue by month","output_path":"..."}`.
5. Read the chart path back with `open_file` or show it if the user asked to
   see it. Verify the file exists before claiming success.
6. For a large file, summarize and show a small preview; do not dump all rows.

## Spec Detail

- `chart_type`: `bar`, `line`, or `pie`.
- `x` defaults to the first column; `y` defaults to the first numeric column.
- `output_path` defaults to `<csv>_<chart>.png` next to the source.
- `preview` returns the first N rows (`rows`, default 20).
- Encoding fallback: UTF-8 with BOM, UTF-8, cp1252, latin-1.

## Output Shape

`profile` returns a `data` object. `chart` returns `path`. `preview` returns
CSV text.

## Recovery

- Column not found: call `profile` and use an exact column name.
- Non-numeric `y`: choose a numeric column or ask the user.
- Empty file: report zero rows and ask for the source.
- Chart path cannot be written: choose a Desktop path and retry once.
