---
name: browser-accessibility
skill: browser_get_accessibility_tree
args:
  - name: target
    type: string
    required: false
    default: ""
    description: Optional label/role filter, case-insensitive (upload, submit, search).
  - name: max_chars
    type: int
    required: false
    default: 6000
    description: Maximum tree characters to return, between 1000 and 12000.
description: >
  List interactive elements with roles, labels and selectors. Use when
  browser_read_full already ran but you need to update the element list without
  re-reading page text. Pass target= to return only elements matching a label
  (upload, submit, search etc). Never call this immediately after
  browser_read_full on the same unchanged page — it returns the same data.
---

## When to Use

Use after browser_read_full when the element list is stale (a menu opened, a
modal appeared, or the page changed) and you need fresh selectors without
re-reading page text. Pass `target` to narrow a dense page to the control you
need in one call.

## When NOT to Use

- Do not call it immediately after browser_read_full on the same unchanged page.
- Do not use it to read article text or links; use browser_read_page.
- Do not use it for a native Windows dialog or application; use screen-ocr.
- Do not call it before the page has finished loading; wait first if needed.
- Do not use it to work around a missing element more than once. If `target`
  returns nothing, read the page and look for an iframe or login wall.

## Step Pattern

1. browser_navigate or browser_read_full establishes the current page.
2. browser-accessibility (optionally with target) returns roles, labels and
   selectors.
3. Pick the exact element whose role and label match the task.
4. browser-interact passes that selector directly, skipping its internal walk.
5. browser-wait then browser-read-page verify the result.
6. For uploads, use the selector of the control that opens the file input, not
   the hidden file input itself, then call browser-upload-file.

## Args Detail

- `target` is optional. When given, only elements whose role or label contains
  the string (case-insensitive) are returned. Example: `target: "upload"`.
- `max_chars` defaults to 6000 and is clamped to 1000-12000. The cap is
  character-based, so a page with many elements is not cut off by an element
  count. A result with `_truncated: true` was clipped; retry with a more
  specific `target`.
- The returned selector uses a stable `data-winbrow-id` attribute. Pass it
  verbatim to browser-interact.
- Hidden file inputs are included on purpose; they confirm the page has an
  upload target even when the visible button is a wrapper.

## Output Shape

Returns `success`, `output` (a list of elements with role, label, tag, type,
href, disabled state and selector), `url`, `title`, `element_count`,
`total_elements`, and optional `target` / `_truncated` / `_warning`. Labels are
clipped, so two controls can share a prefix; use role and nearby text to
disambiguate.

## Recovery

If `_truncated` is set, retry once with a more specific `target`. If the target
is still missing, call browser-wait and retry once, then browser-read-page to
check for an iframe or login wall. If the element exists but the interaction
fails, prefer the selector over the natural-language instruction. Never fall
back to screen-ocr for browser content. Follow the self-correct rule and emit
clarify after two failed lookups.
