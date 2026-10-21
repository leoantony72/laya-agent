---
name: screen-ocr
skill: screen_read_text
args:
  - name: region
    type: string
    required: false
    default: full
    description: Capture region — full, active_window, or browser.
description: >
  Capture the screen or a window and extract visible text with OCR. Use only
  for native desktop apps that have no DOM or API, such as PDF viewers, image
  editors, legacy software and non-browser dialogs. Do not use it for browser
  content or file contents.
---

## When to Use

Use when the content is visible only in a native Windows application and no
structured tool can read it: a PDF viewer, an image editor, a game UI, old ERP
software, a desktop notification or an error dialog from a non-browser app.
Choose the region deliberately: `active_window` for the focused app, `full` for
the whole screen when ownership is unclear.

## When NOT to Use

- Do not use it for content in a browser tab or web page; use
  browser-read-page or browser-accessibility.
- Do not use it for file contents; use open-file or the filesystem read tools.
- Do not use it for system dialogs triggered by the browser; use browser tools.
- Do not use it when a structured accessibility tree is available for the same
  app.
- Do not call it more than twice in a row for the same empty result.

## Step Pattern

1. Decide where the content lives, not what you want to read.
2. Browser content goes to browser-read-page; stop.
3. File content goes to open-file; stop.
4. Native desktop content goes to screen-read-text with
   `region="active_window"`.
5. Full-screen, owner-unknown content uses `region="full"` as a last resort.
6. If the text is empty or garbled, wait briefly and retry once with
   `active_window`; then clarify.

## Args Detail

- `region` accepts `full`, `active_window` or `browser`.
- `active_window` is the default choice for a known app and avoids unrelated
  desktop noise.
- `browser` captures the browser window but still returns unstructured text;
  it is a fallback, not a replacement for browser tools.
- OCR takes about 2-5 seconds and loses layout, so numbers and small labels may
  be misread. Cross-check critical values.

## Output Shape

Returns `success`, `output`, `text` (the raw OCR result) and the captured region.
An empty `text` with success means the screen contained no recognizable text,
not that the screen is blank.

## Recovery

If OCR returns empty or garbled text, wait 2 seconds and retry once with
`active_window`. If it is still unusable, emit clarify describing what is
visible and ask the user to confirm. Never use OCR as the primary reader for a
browser task. Follow the self-correct rule and avoid repeating the same capture.
