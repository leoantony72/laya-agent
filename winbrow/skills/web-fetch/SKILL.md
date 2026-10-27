---
name: web-fetch
skill: web_fetch
kind: tool
args:
  - name: url
    type: string
    required: true
    description: Full http(s) URL to read.
  - name: max_chars
    type: int
    required: false
    default: 12000
    description: Maximum characters to return (1000-40000).
  - name: focus
    type: string
    required: false
    default: ""
    description: >
      The question or facts this page must answer. When set, only the most
      relevant sections are returned (~6000 chars).
description: >
  Fetch a specific URL and return its readable main content (navigation, ads,
  and boilerplate removed). No browser opened. Use after web_search when a
  result URL needs to be read in full, or when you know the exact URL of a
  public page. Pass focus with the exact facts you need to keep only the
  relevant sections. Does not work for login-required or JavaScript-heavy pages
  — use browser_navigate for those.
---

# Web Fetch (HTTP GET, No Browser)

## When to Use

Use when `web_search` returned a URL that needs to be read in full, or when you
already know the exact URL of a public documentation page, product listing,
article, API, or JSON endpoint. This is Layer 3 of the web hierarchy.

## When NOT to Use

- Do not use for pages behind a login or a paywall; use the browser after the
  user has signed in.
- Do not use for pages that require JavaScript to render the content; use the
  browser.
- Do not use when the task requires clicking, typing, uploading, or submitting.
- Do not use to search; use `web_search`.

## Step Pattern

1. Get the exact URL from `web_search`, the user, or HISTORY.
2. Call `web_fetch(url, max_chars, focus)` with focus = the specific facts or
   question this page must answer.
3. Read the returned text. It stays in HISTORY; do not fetch the same URL
   again. If it was truncated, make at most ONE retry with a larger
   `max_chars` (up to 40000), or use a different source.
4. Cite the final URL and the relevant passage.
5. If the page is a login wall or empty JavaScript shell, switch to
   `browser_navigate` + browser read skills.

## Args Detail

- `url` must be a full `http://` or `https://` URL.
- `max_chars` defaults to 12000 and is clamped to 1000–40000.
- `focus` is optional but recommended for research. It makes the tool return
  only the sections that match the question (about 6000 chars), plus an
  "Additional relevant facts" block for prices, dates, and numbers.
- HTML is reduced to main content with Trafilatura (Markdown, tables and code
  kept); nav, ads, and footers are removed. PDFs use pdfminer/pypdf; JSON is
  pretty-printed.

## Output Shape

Returns `success`, `output` with URL/STATUS/CONTENT-TYPE/CHARS/EXTRACTOR/
RAW-CHARS (plus FOCUS and SELECTED when focus was used) and the text, plus
`url`, `status`, `content_type`, `chars`, `truncated`, `extractor`,
`raw_chars`, `focus`, `selected_sections`, and `total_sections`.

## Recovery

- HTTP 404/403: report the status; do not retry the same URL in the browser
  unless the task requires interaction.
- Timeout: retry once with a shorter expected page, or ask for another source.
- Empty text: the page is likely JavaScript-rendered; use the browser.
- PDF extraction fails: download the file and use `read_document`.
