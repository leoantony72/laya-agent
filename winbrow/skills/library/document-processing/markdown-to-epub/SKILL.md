---
name: markdown-to-epub
skill: playbook_markdown_to_epub
kind: playbook
category: document-processing
source: https://github.com/smerchek/claude-epub-skill
license: see upstream repository
args: []
description: >
  Convert Markdown, notes, or chat summaries into an EPUB 3 ebook. Use when the
  user wants a Kindle or e-reader file.
---

# Markdown to EPUB

## When to Use

Use when GOAL asks for an `.epub`, an ebook, a Kindle-ready file, or a
multi-chapter document for an e-reader.

## When NOT to Use

- Do not use for plain text, PDF, DOCX, or HTML output.
- Do not use when the user only wants the text in chat.
- Do not add DRM or claim Kindle Store compatibility.
- Do not invent chapters that are not in the source.

## Prerequisites

- The builder is `run_skill_script` with `skill="markdown-to-epub"` and
  `script="build_epub.py"`.
- It uses only the Python standard library; no install is required.

## Step Pattern

1. Get the source text. If it is a file, use `read_document`; if it is chat
   content already in HISTORY, use it directly.
2. Make sure the top-level title is a single `# Heading`. Each chapter should
   use `# Chapter title`, with `##` for subsections.
3. Call `run_skill_script`:
   `{"operation":"build","output_path":"C:\\Users\\me\\Desktop\\book.epub","title":"Book title","author":"Author","content":"# Chapter 1\\n\\nText..."}`
   or supply `chapters`:
   `{"chapters":[{"title":"Chapter 1","content":"Text..."}]}`
4. Verify the return value and the file path. Optionally unzip and inspect
   `OEBPS/content.opf` with `run_command` if the user needs proof of structure.
5. Report the path, title, author, and chapter count.

## Spec Detail

- `content` supports `#`/`##` headings, bullets, numbered lines, and blank
  lines.
- `content_path` reads from a UTF-8 file.
- `chapters` overrides `content` when present.
- `language` defaults to `en`; set it for a non-English book.
- The output extension is forced to `.epub`.

## Output Shape

Returns `success`, `output`, `path`, and `count` (chapters).

## Recovery

- No content: ask for the source or write the requested summary first.
- Output path is locked: choose a new filename and retry once.
- The EPUB opens with a missing table of contents: inspect `nav.xhtml`; the
  builder always writes it, so report the exact error instead of guessing.
- Never claim Kindle compatibility beyond standard EPUB 3.
