---
name: browser-wait
skill: browser_wait
args:
  - name: seconds
    type: int
    required: false
    default: 1
    description: Seconds to wait, between 0 and 10.
description: >
  Wait for the current page to load or update, then return its state. Use after
  an interaction, navigation or upload when the content is asynchronous or
  stale. Do not use it as a substitute for reading the page or when the page is
  already stable.
---

## When to Use

Use when HISTORY shows a successful action but the page text still reflects the
old state, or when a site is known to load results, modals or progress bars
asynchronously. It is the correct bridge between an action and its verification
read.

## When NOT to Use

- Do not use it before browser-navigate on a fresh task; navigate first.
- Do not use it repeatedly to burn time when the page is already stable.
- Do not use it instead of browser-read-page; a wait returns state, but the
  explicit read is still the verification step.
- Do not wait longer than 10 seconds in one call; split only if the site is
  known to be slow.

## Step Pattern

1. browser-interact, browser-navigate or browser-upload-file dispatches.
2. browser-wait for 1-3 seconds when content is expected to change.
3. browser-read-page confirms the new state.
4. If unchanged after two waits, switch to browser-accessibility or clarify
   instead of waiting again.

## Args Detail

- `seconds` defaults to 1 and is clamped to 0-10. Use 2-3 seconds for uploads
  and search results, 1 second for simple transitions.
- A wait that returns identical text is not a failure by itself; it means the
  page did not change within the window.

## Output Shape

Returns `success`, `output` (the current page state), `url`, `title`, `text`,
`links`, and an optional `_warning`. The state is captured after the wait, so
it can be used directly by the next planning turn.

## Recovery

If the state did not change, call browser-read-page once to confirm, then
browser-accessibility to find the control that advances the page. Do not call
browser-wait more than twice in a row for the same state. If a progress bar
never disappears, verify with browser-read-page and emit clarify rather than
looping. Follow the self-correct rule.
