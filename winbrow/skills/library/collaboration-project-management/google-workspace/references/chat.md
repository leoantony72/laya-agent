# Google Chat

## Operations

- Send: `{"service":"chat","space":"spaces/AAAA...","text":"..."}`

## Rules

- Confirm the space and the exact message before sending.
- Do not send to a space the user has not named.
- Do not include secrets, customer data, or large pasted documents.
- After sending, report the message ID and space.
- For a threaded reply, the current helper does not support `thread`; say so
  rather than sending a top-level message that looks like a reply.
