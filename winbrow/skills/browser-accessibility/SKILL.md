---
name: browser-accessibility
skill: browser_get_accessibility_tree
args:
  - name: max_elements
    type: int
    required: false
    default: 80
    description: Maximum interactive elements to return, between 20 and 160.
description: >
  List the current page's interactive elements with roles, accessible labels
  and CSS selectors. Use before browser-interact on an unfamiliar or dynamic
  page. Do not use it for bulk text extraction or for native desktop apps.
---

## When to Use

Use whenever the target control is not obvious from browser-read-page: modals,
dropdowns, icon-only buttons, file inputs, SPA components and pages with
duplicate labels. It returns the selector that makes the following interaction
reliable.

## When NOT to Use

- Do not use it to read article text or links; use browser-read-page.
- Do not use it for a native Windows dialog or application; use screen-ocr.
- Do not use it after every click when the page state is already known.
- Do not call it before the page has finished loading; wait first if needed.

## Step Pattern

1. browser-navigate or browser-wait establishes the current page.
2. browser-accessibility returns roles, labels and selectors.
3. Pick the exact element whose role and label match the task.
4. browser-interact passes that selector (and value when typing).
5. browser-wait then browser-read-page verify the result.
6. For uploads, use the selector of the control that opens the file input, not
   the hidden file input itself, then call browser-upload-file.

## Args Detail

- `max_elements` defaults to 80 and is clamped to 20-160. Increase it for dense
  admin pages; decrease it when the output is noisy.
- The returned selector uses a stable `data-winbrow-id` attribute. Pass it
  verbatim to browser-interact.
- Hidden file inputs are included on purpose; they confirm the page has an
  upload target even when the visible button is a wrapper.

## Output Shape

Returns `success`, `output` (a list of elements with role, label, tag, type,
href, disabled state and selector), `url`, `title`, and optional `_warning`.
Labels are clipped, so two controls can share a prefix; use role and nearby text
to disambiguate.

## Recovery

If the target is missing, call browser-wait and retry once, then
browser-read-page to check for an iframe or login wall. If the element exists
but the interaction fails, prefer the selector over the natural-language
instruction. Never fall back to screen-ocr for browser content. Follow the
self-correct rule and emit clarify after two failed lookups.
