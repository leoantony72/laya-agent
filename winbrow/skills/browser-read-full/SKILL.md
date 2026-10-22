---
name: browser-read-full
skill: browser_read_full
args:
  - name: target
    type: string
    required: false
    default: ""
    description: Optional label/role filter for interactive elements (upload, submit, search).
  - name: max_text_chars
    type: int
    required: false
    default: 3000
    description: Maximum page-text characters, between 1000 and 5000.
  - name: max_tree_chars
    type: int
    required: false
    default: 3000
    description: Maximum interactive-element characters, between 1000 and 6000.
description: >
  Read the current page text AND interactive elements in one call. Use as the
  first step on any unfamiliar page before interacting. Returns title, URL,
  visible text, links, and a filtered accessibility tree. Replaces calling
  browser_read_page and browser_get_accessibility_tree separately. Pass
  target= to filter elements by label.
---

## When to Use

Use as the DEFAULT first read on any page you have not inspected yet, and
again after a navigation or a major page change. One call returns everything
needed to choose the next action: page text, links, and interactive elements
with selectors. It replaces the two-step read (browser-read-page then
browser-accessibility) that otherwise costs an extra brain turn.

## When NOT to Use

- Do not call it immediately after browser-navigate if the navigation already
  returned a complete page state; the state is cached and the same data comes
  back. Read HISTORY first.
- Do not use it when only the element list changed (a menu opened); use
  browser-accessibility with `target` instead.
- Do not use it to read a file on disk or a native desktop app; use open-file,
  read-document or screen-ocr.
- Do not use it on a page that has not loaded yet; call browser-wait first.

## Step Pattern

1. browser-navigate opens the page in the agent tab.
2. browser-read-full returns text, links and interactive elements together.
3. Pick the exact selector for the control you need from the returned elements.
4. browser-interact passes that selector directly (no internal tree walk).
5. browser-wait then browser-read-page verify the result.
6. For uploads: browser-interact clicks the upload trigger, then
   browser-upload-file sets the absolute path.

## Args Detail

- `target` is optional. When given, only interactive elements whose role or
  label contains the string (case-insensitive) are returned. Example:
  `target: "upload"` returns just the upload-related controls.
- `max_text_chars` defaults to 3000 and is clamped to 1000-5000.
- `max_tree_chars` defaults to 3000 and is clamped to 1000-6000.
- The element list is capped by characters, not by an element count, so a dense
  page is not cut off before the control you need. `_truncated_tree: true`
  means the list was clipped; retry with a specific `target`.
- The returned selector uses a stable `data-winbrow-id` attribute. Pass it
  verbatim to browser-interact.

## Output Shape

Returns `success`, `output` (title, URL, PAGE TEXT, LINKS and INTERACTIVE
ELEMENTS), `url`, `title`, `text`, `links`, `elements`, `element_count`,
`method`, and optional `_truncated_text` / `_truncated_tree` / `_truncated` /
`_warning`.

## Recovery

If `_truncated_tree` is set, retry once with a specific `target` or use
browser-accessibility with that target. If the page text is empty or stale,
call browser-wait and read again. If the needed control is not present, check
for an iframe, login wall or a menu that must be opened first. Never fall back
to screen-ocr for ordinary browser content. Follow the self-correct rule and
emit clarify after two failed lookups.
