# Meeting Brief

## Goal

Prepare a one-page brief before a legal, compliance, vendor, or deal meeting.

## Structure

1. **Meeting facts** — date, time, attendees, format, decision needed.
2. **Objectives** — the three outcomes that would make the meeting useful.
3. **Context** — relevant documents, prior decisions, open issues.
4. **Talking points** — one line each, tied to a document or fact.
5. **Questions to ask** — prioritized, with the reason each matters.
6. **Risks and red lines** — what must not be conceded.
7. **Follow-ups** — owner and due date for each.

## Method

- Read the supplied agenda, documents, and prior notes with `read_document`.
- Separate known facts from assumptions.
- Highlight any document that is missing or unreadable.
- Keep the brief under one page unless the user asks for more.

## Output Format

Markdown with the seven headings above. Add a final line:
`Prepared from: <filenames>; not legal advice.`

## Rules

- Do not invent attendee roles or positions.
- If the user has not supplied the documents, list exactly what is needed.
- For a contentious matter, include a short "questions not to ask" section
  only when the user asks for strategy.
