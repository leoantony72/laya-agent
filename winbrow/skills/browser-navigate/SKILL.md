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
    description: Force a fresh agent tab instead of reusing the agent's current tab.
description: >
  Open a URL in the real browser. Use ONLY when the task requires actual
  interaction: playing media, uploading files, filling forms, logging in, using
  a web tool, or when the user explicitly asked to open a site. For research,
  comparison, or reading — use web_search or web_fetch instead. Every call
  opens a real browser tab and costs multiple steps.
---

## When to Use

Use only for browser interaction: opening a site the user explicitly asked for,
playing media, uploading a file, filling a form, logging in, using a web-only
tool, downloading something that requires browser interaction, or anything
behind a login wall. The first navigation of a task creates a new agent tab;
the user's existing tabs are never touched. Later navigations reuse that agent
tab unless `new_tab` is true.

## When NOT to Use

- Do not use for research, search, product comparison, price lookup, news, or
  documentation reading. Use `web_search` first, then `web_fetch` for a known
  URL.
- Do not use when the result is text you will read and report; use
  `web_search`/`web_fetch`.
- Do not call it when the browser is already on the target page; use
  browser-read-full or browser-read-page instead.
- Do not call it to click, type, submit, scroll or upload; use browser-interact
  or browser-upload-file.
- Do not pass a bare domain when a documented URL pattern exists (YouTube
  channel, GitHub user, subreddit). Resolve the pattern first.
- Do not use it for a plain web search; use web_search or navigate to the
  search results URL.

## Step Pattern

1. browser-navigate opens the page in the agent tab and returns state.
2. Inspect the returned title, URL and text against the intended destination.
3. If the page is dynamic or still loading, call browser-wait.
4. Call browser-read-full once to get text and interactive elements together.
5. browser-interact with the selector from that read, then verify with
   browser-read-page or browser-wait.

## Args Detail

- `url` must be an absolute URL. A missing scheme is treated as `https://`, but
  always pass the full URL explicitly. Percent-encode spaces and query values.
- `new_tab` accepts `true` or `false`. The first navigation of a task always
  uses a fresh agent tab. Set `true` only when you need a second agent tab
  (for example comparing two sites); `false` reuses the agent tab.
- If navigation times out, the result reports the URL actually reached. Treat
  that as the current state rather than assuming the target loaded.

## Output Shape

Returns a dict with `success`, `output` (rendered title, URL, page text and
links), `url`, `title`, `text`, `links`, `tab` and an optional `_warning`. A
navigation mismatch adds a warning explaining the intended versus actual URL.

## Recovery

Read the warning and the returned URL first. If the page is wrong, use
browser-read-full or browser-accessibility to find the correct link, then
navigate again with the corrected URL. Never repeat the identical failing URL.
After two failed navigations, emit clarify with the URLs tried. Follow the
self-correct rule: diagnose before retrying.
