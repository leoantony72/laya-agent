---
name: browser-interact
skill: browser_interact
args:
  - name: instruction
    type: string
    required: false
    default: ""
    description: One natural-language action, such as "click the Python filter".
  - name: selector
    type: string
    required: false
    default: ""
    description: CSS selector from browser-read-full or browser-accessibility.
  - name: value
    type: string
    required: false
    default: ""
    description: Text to type into the selected field.
description: >
  Perform exactly one click, type or submit. Pass selector= directly if you
  already have it from a prior accessibility tree call — this skips the
  internal tree walk and runs faster. Never use for file upload — use
  browser_upload_file instead.
---

## When to Use

Use for a single page interaction: clicking a link or button, filling one
field, or submitting a search. It is the execution half of the
observe-act-verify loop and should follow a read that named the target. When
you already have the selector from browser-read-full or browser-accessibility,
pass it directly so no second tree walk is needed.

## When NOT to Use

- Do not use it to set a file on an upload input; click the upload trigger here
  (the native file picker is intercepted and suppressed) and then call
  browser-upload-file with the absolute path.
- Do not type a file path into a visible text field as an upload workaround.
- Do not use it for navigation when the URL is already known; use
  browser-navigate.
- Do not chain several clicks in one instruction. One action per call.
- Do not guess an element you have not seen in HISTORY.

## Step Pattern

1. browser-read-full (or browser-accessibility for an update) identifies the
   control and returns its selector.
2. browser-interact performs ONE action, passing that exact selector.
3. browser-wait when the page loads content asynchronously.
4. browser-read-page verifies the resulting state.
5. For an upload flow: browser-interact clicks the upload trigger, then
   browser-upload-file sets the path, then browser-wait and browser-read-page
   confirm completion.

## Args Detail

- `selector` is optional but strongly preferred. Use the CSS selector returned
  by browser-read-full or browser-accessibility. Do not invent selectors.
- `instruction` is only needed when no selector is available. Describe one
  action only, e.g. "click Save" or "type python in the search box".
- `value` is optional and only used when typing. It may be empty for clicks.
- If the target is a file input, do not type into it; use browser-upload-file.
- If the element is missing, the call fails with an element-not-found output
  rather than guessing another control.

## Output Shape

Returns `success`, `output` (the resulting page state), `url`, `title`, `text`,
`links`, and an optional `_warning`. Success means the interaction was
dispatched, not that the site accepted its effect; always verify.

## Recovery

If the element was not found, call browser-accessibility with a specific
`target` and retry once with the exact selector. If the page did not change,
call browser-wait and read again before concluding failure. Never repeat the
same failed interaction; use a different selector or emit clarify after two
attempts. Follow the self-correct rule in every recovery.
