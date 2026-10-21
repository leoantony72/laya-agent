---
name: browser-navigate
skill: browser_navigate
args:
  - name: url
    type: string
    required: true
    description: Fully-resolved URL including the https:// scheme.
  - name: new_tab
    type: bool
    required: false
    default: false
    description: Open in a new tab instead of reusing the active tab.
description: >
  Open a fully-resolved URL, wait for it to render, and return the page title,
  URL, text and links. Use for every page change and for retrying a failed
  navigation with a corrected URL. Do not use to read the page you are already
  on, or when the target URL is still unknown.
---

## When to Use

Use at the start of any browser task, when moving to a known URL, or when
recovering from a navigation that landed on the wrong page. The returned page
state is usable immediately, so a separate read is not required unless the
content looks stale.

## When NOT to Use

- Do not call it when the browser is already on the target page; use
  browser-read-page instead.
- Do not call it to click, type, submit, scroll or upload; use browser-interact
  or browser-upload-file.
- Do not pass a bare domain when a documented URL pattern exists (YouTube
  channel, GitHub user, subreddit). Resolve the pattern first.
- Do not use it for a plain web search; use web_search or navigate to the
  search results URL.

## Step Pattern

1. browser-navigate opens the page and returns state.
2. Inspect the returned title, URL and text against the intended destination.
3. If the page is dynamic or still loading, call browser-wait.
4. To act on the page, call browser-accessibility then browser-interact.
5. Verify the result with browser-read-page before declaring done.

## Args Detail

- `url` must be an absolute URL. A missing scheme is treated as `https://`, but
  always pass the full URL explicitly. Percent-encode spaces and query values.
- `new_tab` accepts `true` or `false`. Use `true` when the current page must
  stay open for a later step; otherwise reuse the active tab.
- If navigation times out, the result reports the URL actually reached. Treat
  that as the current state rather than assuming the target loaded.

## Output Shape

Returns a dict with `success`, `output` (rendered title, URL, page text and
links), `url`, `title`, `text`, `links`, and an optional `_warning`. A
navigation mismatch adds a warning explaining the intended versus actual URL.

## Recovery

Read the warning and the returned URL first. If the page is wrong, use
browser-read-page or browser-accessibility to find the correct link, then
navigate again with the corrected URL. Never repeat the identical failing URL.
After two failed navigations, emit clarify with the URLs tried. Follow the
self-correct rule: diagnose before retrying.
