# Google Docs

## Operations

- Get: `{"service":"docs","docs_operation":"get","document_id":"..."}`
- Create: `{"service":"docs","docs_operation":"create","title":"..."}`
- Append: `{"service":"docs","docs_operation":"append","document_id":"...","text":"..."}`

## Rules

- Read the document before appending; do not assume its structure.
- Confirm the title and destination folder before creating.
- For a rewrite, use a new document rather than destructive edits.
- Report the document ID and URL after creation.
- Do not paste secrets into a shared document.
