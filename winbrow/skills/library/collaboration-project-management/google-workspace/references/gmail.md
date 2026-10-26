# Gmail

## Operations

- List/search: `{"service":"gmail","gmail_operation":"list","query":"from:alice newer_than:7d","limit":10}`
- Send: `{"service":"gmail","gmail_operation":"send","to":"a@b.com","subject":"...","body":"..."}`

## Rules

- Search first; never guess a message ID.
- Show recipient, subject, and body before sending.
- Do not send to a list or a large recipient set without explicit approval.
- Do not include secrets or customer data in a subject line.
- After sending, verify with a list/search query.

## Common queries

- `from:name@example.com`
- `subject:"invoice"`
- `has:attachment newer_than:30d`
- `is:unread label:inbox`
