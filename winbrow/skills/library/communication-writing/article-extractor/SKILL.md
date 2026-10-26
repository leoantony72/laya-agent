---
name: article-extractor
skill: playbook_article_extractor
kind: playbook
category: communication-writing
source: https://github.com/michalparkola/tapestry-skills-for-claude-code/tree/main/article-extractor
license: see upstream repository
args: []
description: >
  Extract clean article text and metadata from a web page. Use to save a blog
  post, tutorial, or news article without ads or navigation clutter.
---

# Article Extractor

## When to Use

Use when GOAL gives a URL and asks to save, extract, summarize, or archive the
article content.

## When NOT to Use

- Do not use for a PDF or a local document; use `read_document`.
- Do not use for a page that requires a login unless the user is already signed
  in in the browser.
- Do not bypass a paywall or a robots restriction.
- Do not copy the whole page into chat when a short summary was requested.

## Step Pattern

1. Call `browser_navigate(url)`.
2. Call `browser_read_full` once. It returns page text and interactive elements.
3. Identify the article title, byline, date, and main body. Ignore menus,
   cookie banners, related links, and comments.
4. If the page is mostly script-rendered and the text is empty, try the `fetch`
   MCP tool on the same URL. If both fail, ask the user to open the page and
   paste the text.
5. Compose clean Markdown:
   `# Title`, `Byline — Date`, `Source: URL`, then the body with headings.
6. Save with `create_text_file` or `mcp_filesystem_write_file` when the user
   asked for a file, then read it back.
7. Summarize the article in 3–6 bullets if the user asked for a summary.

## Output Shape

Clean Markdown text and/or a saved `.md`/`.txt` file. Include the source URL and
the extraction date.

## Recovery

- 403/blocked: report it; do not retry with a different user agent or bypass.
- Paywall: save only the visible preview and say it is partial.
- Infinite scroll: scroll once, re-read, then stop; do not loop.
- Wrong page: navigate again with the exact URL from the user.
