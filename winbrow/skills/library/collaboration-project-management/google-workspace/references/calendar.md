# Google Calendar

## Operations

- List: `{"service":"calendar","calendar_operation":"list","calendar_id":"primary","limit":10,"time_min":"2026-01-01T00:00:00Z"}`
- Create: `{"service":"calendar","calendar_operation":"create","event":{"summary":"...","start":{"dateTime":"2026-01-01T10:00:00-05:00"},"end":{"dateTime":"2026-01-01T10:30:00-05:00"},"attendees":[{"email":"a@b.com"}]}}`

## Rules

- Confirm timezone, duration, and attendees before creating.
- Do not invite external attendees without explicit approval.
- Check for conflicts with a list call first.
- Never create a recurring event with an unbounded end date unless asked.
- After creating, fetch the event and report its link/ID.
