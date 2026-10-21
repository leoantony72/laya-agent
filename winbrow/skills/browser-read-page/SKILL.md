---
name: browser-read-page
skill: browser_read_page
args:
  - name: url
    type: string
    required: false
    default: ""
    description: Optional URL to report when the active tab URL is unknown.
  - name: max_chars
    type: int
    required: false
    default: 5000
    description: Maximum page-text characters to return, between 1000 and 5000.
description: >
  Read the current browser page without navigating and return its title, URL,
  visible text and links. Use to inspect state before interacting and to verify
  the result of an action. Do not use for native desktop apps, file contents,
  or pages that have not loaded yet.
---

## When to Use

Use before browser-interact to find the exact link, label or text to act on,
and after any action when HISTORY needs proof of the resulting page. It is also
the correct first choice when the current tab URL is uncertain.

## When NOT to Use

- Do not use it to open a new page; use browser-navigate.
- Do not use it for content in a native Windows app; use screen-ocr.
- Do not use it to read a file on disk; use open-file or the filesystem tools.
- Do not use it immediately after browser-navigate unless the returned state is
  stale or truncated.
- Do not use it to discover CSS selectors for controls; use
  browser-accessibility for that.

## Step Pattern

1. browser-navigate or browser-wait establishes the page.
2. browser-read-page extracts the visible text and links.
3. Use the exact text or URL discovered here in the next step's args.
4. browser-accessibility then browser-interact when an element must be clicked.
5. browser-read-page again after the action to confirm the effect.

## Args Detail

- `url` is optional and only used to report/fetch when the current tab URL is
  unknown. Prefer omitting it so the active page is read.
- `max_chars` defaults to 5000 and is clamped to 1000-5000. Increase it only
  when the needed content sits below the first screen of text.
- Empty page text is normal on canvas, video, PDF viewer and login-wall pages;
  that is not proof the action failed.

## Output Shape

Returns `success`, `output` (title, URL, PAGE TEXT and LINKS), `url`, `title`,
`text`, `links`, and an optional `_warning`. The links list is capped, so for
heavily linked pages use browser-accessibility to get every control.

## Recovery

If the text is empty or stale, call browser-wait for 1-3 seconds and read
again. If the needed control is still missing, call browser-accessibility. Only
after both structured reads fail should you consider screen-ocr, and never for
ordinary web content. If the page clearly failed to load, re-check the URL via
HISTORY before navigating again.
