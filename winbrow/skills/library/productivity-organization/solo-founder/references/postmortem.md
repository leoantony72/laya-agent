# Postmortem

## Goal

Turn an incident or missed launch into a small set of durable improvements.

## Structure

1. **Summary** — what happened, when, and the user impact.
2. **Timeline** — detection, response, mitigation, resolution (with times).
3. **Root cause** — the first wrong state, not the last symptom.
4. **What went well** — keep the useful parts.
5. **What made it worse** — missing alert, unclear owner, slow rollback.
6. **Action items** — owner, due date, and a test that proves it is done.
7. **Lessons** — one or two, not a list of platitudes.

## Rules

- Do not blame individuals: describe decisions and systems, not people.
- Separate trigger from root cause.
- Every action item must be small and verifiable.
- Do not invent a root cause you did not confirm.
- Publish a short version for customers if they were affected.

## Output

A one-page Markdown postmortem plus a table of action items.
