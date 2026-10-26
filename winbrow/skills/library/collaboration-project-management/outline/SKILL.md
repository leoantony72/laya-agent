---
name: outline
skill: playbook_outline
kind: playbook
category: collaboration-project-management
source: https://github.com/sanjay3290/ai-skills/tree/main/skills/outline
license: Apache-2.0
args: []
description: >
  Search, read, create, and update Outline wiki documents. Use when the team
  knowledge base is Outline.
---

# Outline Wiki

## When to Use

Use when GOAL asks to find, read, write, or update a document in an Outline
wiki (cloud or self-hosted).

## When NOT to Use

- Do not use without an Outline base URL and API token.
- Do not publish or move a document without explicit approval.
- Do not copy confidential wiki content into a public channel.
- Do not guess a document or collection ID.

## Prerequisites

- Outline URL and API token in Integrations, or `OUTLINE_API_URL` and
  `OUTLINE_API_TOKEN`.
- `run_skill_script` with `skill="outline"` and `script="outline_api.py"`.
- `requests` is installed.

## Step Pattern

1. If available, call `integration_status("outline")`; stop if unconfigured.
2. Search first:
   `{"operation":"search","query":"onboarding","limit":10}`.
3. Read the exact document:
   `{"operation":"info","document_id":"..."}`.
4. For a new document:
   `{"operation":"create","title":"...","text":"# ...","collection_id":"...","publish":false}`.
   Create as a draft first; publishing needs confirmation.
5. For an update, read the current text, then send the full replacement:
   `{"operation":"update","document_id":"...","text":"..."}`.
6. Verify by reading the document back and checking the title and key sections.
7. Report the document URL/ID and whether it is published.

## Safety Rules

- Prefer drafts over published documents.
- Do not overwrite a document without reading it first.
- Keep the team's formatting conventions; do not restructure the whole page.
- Do not include secrets or customer data in a public collection.
- If the API returns a permission error, report the collection and stop.

## Output Shape

Search results, document JSON/text, or a created/updated document with ID and
URL.

## Recovery

- 401/403: re-enter the token or check the workspace permission.
- 404: search again; the ID may be stale.
- Conflict on update: re-read and merge; do not blind-overwrite.
- API unavailable: report the base URL and the error.
