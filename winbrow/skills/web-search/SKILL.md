---
name: web-search
skill: web_search
kind: tool
args:
  - name: query
    type: string
    required: true
    description: Search query, e.g. "RTX 5090 price Flipkart".
  - name: count
    type: int
    required: false
    default: 5
    description: Number of results to return (1-10).
  - name: freshness
    type: enum
    required: false
    default: ""
    description: "Optional recency filter: day, week, or month."
description: >
  Search the web via API and return ranked results with titles, URLs and
  snippets. No browser opened. Use for ALL research, comparison, product
  lookup, news, documentation search, and any question requiring current
  information. Always try this before considering the browser. Do not use for
  clicking or interaction.
---

# Web Search (API, No Browser)

## When to Use

Use for research, comparison, product specs and prices, news, release notes,
documentation search, and any question that needs current information. This is
Layer 2 of the web hierarchy. It returns titles, URLs, and snippets in one step.

## When NOT to Use

- Do not use for clicking, typing, logging in, uploading, or playing media.
- Do not use for a local file search; use `find_files`.
- Do not use when the user explicitly says "open" or "go to" a site; that is a
  browser interaction.
- Do not open the browser because the snippet was short; use `web_fetch` on the
  result URL first.

## Step Pattern

1. Form one precise query. Include product/model names, year, and the site when
   the user named one.
2. Call `web_search(query, count, freshness)`.
3. Read the ranked results. One search is enough to pick sources: do not re-run
   the query or synonyms. Pick the best 2-3 URLs.
4. If a result URL needs full text, call `web_fetch(url, max_chars, focus)` with
   focus = the facts you need — at most once per URL.
5. Cite titles and URLs in the answer.

## Args Detail

- `query` is required.
- `count` defaults to 5 and is clamped to 1–10.
- `freshness` is optional: `day`, `week`, or `month`.
- Network failure is reported honestly; do not fall back to the browser for a
  research task.

## Output Shape

Returns `success`, `output` (numbered title/URL/snippet blocks), `results`, and
`count`.

## Recovery

- Search provider unavailable: the tool already retries both DuckDuckGo
  endpoints with two browser fingerprints, so retry the same query at most once,
  then report it. Try `web_fetch` on a known URL, or ask the user for the
  source. Do not open the browser.
- No useful results: rewrite the query once with a more specific model/site.
- Result URLs are redirects: the tool unwraps DuckDuckGo redirects.
