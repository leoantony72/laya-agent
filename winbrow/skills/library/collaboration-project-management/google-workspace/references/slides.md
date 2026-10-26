# Google Slides

## Operations

- Get: `{"service":"slides","presentation_id":"..."}`

The current helper returns the presentation JSON (slides, elements, text). It
does not create or edit slides yet.

## Rules

- Extract text and speaker notes from the JSON; do not claim a visual review.
- Use the `pptx` skill for a local deck that must be edited.
- Do not edit a shared deck without explicit approval.
- Report the presentation ID and title.
