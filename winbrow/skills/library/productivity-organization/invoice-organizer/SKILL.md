---
name: invoice-organizer
skill: playbook_invoice_organizer
kind: playbook
category: productivity-organization
source: https://github.com/ComposioHQ/awesome-claude-skills
license: Apache-2.0
args: []
description: >
  Extract invoice metadata and file receipts into dated folders. Use for tax
  preparation and bookkeeping cleanup.
---

# Invoice Organizer

## When to Use

Use when GOAL asks to organize invoices, receipts, or expense documents, or to
rename them consistently for an accountant.

## When NOT to Use

- Do not use for scanned images with no text layer; OCR them first with
  `screen-ocr` or ask for a text PDF.
- Do not delete originals; this skill copies.
- Do not treat extracted amounts as authoritative; they are candidates.
- Do not file documents for a jurisdiction you do not know; ask about the
  desired year/category structure.

## Prerequisites

- `run_skill_script` with `skill="invoice-organizer"` and
  `script="organize_invoices.py"`.
- PyMuPDF is installed. PDFs need a text layer for extraction.
- Images (PNG/JPG/TIFF) need OCR first.

## Step Pattern

1. Scan without writing:
   `{"operation":"scan","path":"C:\\Users\\me\\Downloads\\receipts"}`.
2. Review the extracted vendor, date, amount, and invoice number. Flag records
   with `chars: 0` or missing dates.
3. Confirm the target structure: `Invoices/<year>/<month>/<date> <vendor> -
   Invoice - <amount>.<ext>`.
4. Run a dry run:
   `{"operation":"organize","path":"...","target":"...","dry_run":true}`.
5. After approval, run with `"dry_run":false`. The script copies files and
   writes `invoice-index.csv`.
6. Verify the index row count and open a sample destination.
7. Report uncertain extractions separately; never guess a missing amount.

## Output Shape

Returns `records` with file, vendor, date, amount, invoice number, character
count, and destination. A CSV index is written on apply.

## Recovery

- Text layer missing: ask for a text PDF or OCR the image.
- Amount ambiguous: list all candidate totals and ask which is correct.
- Duplicate destination: choose a suffix; do not overwrite.
- Filename invalid on Windows: the script sanitizes it; report the final name.
