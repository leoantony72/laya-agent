---
name: google-workspace
skill: playbook_google_workspace
kind: playbook
category: collaboration-project-management
source: https://github.com/sanjay3290/ai-skills/tree/main/skills
license: Apache-2.0
args: []
description: >
  Read and write Gmail, Calendar, Drive, Docs, Sheets, Slides, and Chat. Use
  when the user works with their Google Workspace account.
---

# Google Workspace

## When to Use

Use when GOAL asks to search or send Gmail, list or create calendar events,
find or upload Drive files, read/write Sheets, read/append Docs, inspect Slides,
or send a Chat message.

## When NOT to Use

- Do not use without a connected Google account.
- Do not send email, chat, or calendar invites without explicit confirmation.
- Do not upload confidential files without consent.
- Do not use for a personal Gmail account if the user said workspace-only.
- Do not guess a file ID, spreadsheet ID, or calendar ID.

## Prerequisites

- OAuth client JSON and a saved token, configured in the Integrations panel.
- `google-api-python-client`, `google-auth`, and `google-auth-oauthlib`.
- `run_skill_script` with `skill="google-workspace"` and
  `script="google_api.py"`.

## Step Pattern

1. If available, call `integration_status("google")`; if unconfigured, ask the
   user to open Integrations and click Connect.
2. Read the relevant reference for the service:
   `references/gmail.md`, `calendar.md`, `drive.md`, `docs.md`, `sheets.md`,
   `slides.md`, or `chat.md`.
3. Discover before writing: list/search first, then use the exact ID.
4. Call `run_skill_script` with `service` and the operation named in the
   reference. Example:
   `{"service":"gmail","gmail_operation":"list","query":"from:alice newer_than:7d","limit":10}`.
5. For any write (send, create event, upload, append, chat), show the exact
   content and recipient first and wait for confirmation.
6. Verify with a read-back: list the sent message, fetch the event, read the
   sheet range, or fetch the doc.
7. Report the service, operation, IDs, and any scope limitation.

## Safety Rules

- Never send a message or invite without a clear confirmation.
- Never store OAuth tokens in a skill folder, log, or chat.
- Respect least privilege; request only the scopes needed.
- Redact email addresses and file names when summarizing to a third party.
- If a write partially fails, report exactly what succeeded.

## Reference Files

- `references/gmail.md`
- `references/calendar.md`
- `references/drive.md`
- `references/docs.md`
- `references/sheets.md`
- `references/slides.md`
- `references/chat.md`

## Output Shape

A service-specific result with IDs, links, and a read-back confirmation for
writes.

## Recovery

- 401: token expired; ask the user to reconnect.
- 403: missing scope; state the exact scope and ask to re-authorize.
- 404: wrong ID; search again instead of guessing.
- Quota exceeded: report it and stop.
