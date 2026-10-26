# Google Sheets

## Operations

- Read: `{"service":"sheets","sheets_operation":"read","spreadsheet_id":"...","range":"Sheet1!A1:D20"}`
- Write: `{"service":"sheets","sheets_operation":"write","spreadsheet_id":"...","range":"Sheet1!A1","values":[["A","B"],[1,2]]}`
- Append: `{"service":"sheets","sheets_operation":"append","spreadsheet_id":"...","range":"Sheet1","values":[["new","row"]]}`

## Rules

- Read the header row first; do not write into an assumed layout.
- Confirm the range and the exact rows before a write.
- Prefer append over overwrite.
- Do not delete rows or sheets without explicit approval.
- After a write, read the range back and report the row count.
