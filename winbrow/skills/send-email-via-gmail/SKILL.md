---
name: "send-email-via-gmail"
skill: "send_email_via_gmail"
args:
  - name: "recipient_email"
    type: "string"
    required: true
    default: null
    description: "Email address of the recipient."
  - name: "subject"
    type: "string"
    required: true
    default: null
    description: "Subject line of the email."
  - name: "body"
    type: "string"
    required: true
    default: null
    description: "Plain-text body content of the email."
description: >
  Compose and send an email through Gmail's web interface using the compose
  URL shortcut. Use when the user asks to send an email to a recipient with a
  given subject and body. Do not use for reading, searching, or managing
  existing emails, or for non-Gmail providers.
---

## When to Use

Use when the user asks to send an email to a specific recipient with a subject and body, and the browser is available with a logged-in Gmail session.

## When NOT to Use

Do not use for reading, replying to, searching, or organising existing emails; do not use for non-Gmail email providers; do not use when the user only wants a draft saved without sending.

## Step Pattern

1. URL-encode recipient_email, subject, and body, then navigate to https://mail.google.com/mail/?view=cm&fs=1&to={recipient_email}&su={subject}&body={body}.
2. Read the full page to confirm the compose window loaded with the correct recipient, subject, and body.
3. Locate the Send button in the accessibility tree and click it.
4. Wait a few seconds for the send action to complete.
5. Read the accessibility tree again to confirm the compose window closed and the message was sent (e.g. 'Message sent' confirmation or return to inbox).

## Args Detail

- `recipient_email` (string, required): Email address of the recipient.
- `subject` (string, required): Subject line of the email.
- `body` (string, required): Plain-text body content of the email.

## Output Shape

Returns a result dict with `success`, `output`, and any state fields the
chosen tool provides. A successful step means the tool dispatched the action;
it does not by itself prove the wider goal is complete. Verify with the next
read step before declaring the task done.

## Recovery

If the compose window does not load, verify the Gmail session is logged in and retry the navigation. If the Send button cannot be found, re-read the accessibility tree and search for a button labelled 'Send'. If sending fails, check for validation errors (missing recipient, invalid address) and report them to the user rather than retrying blindly.
