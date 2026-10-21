---
name: browser-interact
skill: browser_interact
args:
  - name: instruction
    type: string
    required: true
    description: One natural-language action, such as "click the Python filter".
  - name: selector
    type: string
    required: false
    default: ""
    description: CSS selector from browser-accessibility for the target element.
  - name: value
    type: string
    required: false
    default: ""
    description: Text to type into the selected field.
description: >
  Perform exactly one click, type, submit or scroll on the current page and
  return the resulting page state. Use after browser-accessibility identifies
  the target. Do not use for file uploads, multi-action sequences, or when the
  element has not been observed in HISTORY.
---

## When to Use

Use for a single page interaction: clicking a link or button, filling one
field, submitting a search, or scrolling. It is the execution half of the
observe-observe-act-verify loop and should always follow a read or accessibility
lookup that names the target.

## When NOT to Use

- Do not use it to set a file on an upload input; click the upload trigger here
  and then call browser-upload-file with the absolute path.
- Do not type a file path into a visible text field as an upload workaround.
- Do not use it for navigation when the URL is already known; use
  browser-navigate.
- Do not chain several clicks in one instruction. One action per call.
- Do not guess an element you have not seen in HISTORY.

## Step Pattern

1. browser-read-page or browser-accessibility identifies the control.
2. browser-interact performs ONE action, passing the exact selector when known.
3. browser-wait when the page loads content asynchronously.
4. browser-read-page verifies the resulting state.
5. For an upload flow: browser-interact clicks the upload trigger, then
   browser-upload-file sets the path, then browser-wait and browser-read-page
   confirm completion.

## Args Detail

- `instruction` is required. Describe one action only, e.g. "click Save" or
  "type python in the search box".
- `selector` is optional but preferred. Use the CSS selector returned by
  browser-accessibility. Do not invent selectors.
- `value` is optional and only used when typing. It may be empty for clicks.
- If the target is a file input, do not type into it; use browser-upload-file.
- If the element is missing, the call fails with an element-not-found output
  rather than guessing another control.

## Output Shape

Returns `success`, `output` (the resulting page state), `url`, `title`, `text`,
`links`, and an optional `_warning`. Success means the interaction was
dispatched, not that the site accepted its effect; always verify.

## Recovery

If the element was not found, call browser-accessibility again and retry once
with the exact selector. If the page did not change, call browser-wait and read
again before concluding failure. Never repeat the same failed interaction; use
a different selector or emit clarify after two attempts. Follow the
self-correct rule in every recovery.
