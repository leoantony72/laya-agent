---
name: download-images
skill: download_images
kind: tool
args:
  - name: query
    type: string
    required: true
    description: >
      Image subject ("golden retriever", "anime scenery", "eiffel tower") or a
      direct http(s) image URL. Never a web page URL.
  - name: count
    type: int
    required: false
    default: 3
    description: How many images to download (1-10).
  - name: folder
    type: enum
    required: false
    default: Pictures
    description: "Where to save: Pictures, Downloads, Desktop, or Documents."
  - name: source
    type: enum
    required: false
    default: auto
    description: "auto, wallhaven (art/photos), wikimedia (licensed photos), nekosapi (safe anime art)."
  - name: min_width
    type: int
    required: false
    default: 800
    description: Minimum image width in pixels.
description: >
  Download images for any task: a subject search or a direct image URL.
  Searches keyless sources and validates every file — never opens a browser
  and never needs an API key. Use this for pictures, illustrations, photos,
  and document assets.
---

# Download Images (any task, no browser)

## When to Use

Any request that needs image files on disk, whatever the purpose:

- "download 3 pictures of a golden retriever"
- "save some anime scenery images to downloads"
- "download this image: https://example.com/photo.jpg"
- "get a photo of the Eiffel Tower for my document"
- "find me cool cyberpunk art and save it"
- "put a picture of a cat on my desktop" (then move/apply it yourself)

This is the general image path. The wallpaper tools (`set_wallpaper`,
`download_wallpaper`) are only for changing or filling the desktop wallpaper.

## When NOT to Use

- Never open a browser (`browser_navigate`) or run `web_search` to obtain an
  image. This tool searches and downloads by itself.
- Never use `download_file` for images: it saves whatever bytes arrive without
  validating that they are a decodable image.
- Not for documents (PDF/DOCX/PPTX), videos, or audio — use `download_file`
  or the document skills.

## Step Pattern

1. Turn the request into a short subject: drop filler such as "download",
   "some", "images", "pictures". "Download some pictures of a golden
   retriever" → `query="golden retriever"`.
2. Pick `folder` from the sentence (Downloads/Desktop/Documents), else leave
   the default Pictures.
3. Call `download_images(query, count, folder, source, min_width)`.
4. Report the saved paths. If the task then needs the image (insert into a
   document, set as wallpaper, attach it), use the returned `files` paths.
   This step is NOT complete until the file exists on disk.

## Args Detail

- `query` is required: a subject or a direct http(s) image URL. A page URL is
  not an image URL.
- `count` defaults to 3 and is clamped to 1–10. Ignored for direct URLs.
- `folder` accepts Pictures (default, saved under `WinBrow Images`),
  Downloads, Desktop, or Documents.
- `source`: `auto` tries Wallhaven art/photos, then licensed Wikimedia photos,
  then safe anime art. Ask for `wikimedia` when the image must have a reusable
  license (documents, publishing). Ask for `wallhaven` for art/wallpaper-style
  work.
- `min_width` defaults to 800 px; raise it when the image will be printed or
  displayed large.

## Output Shape

Returns `success`, `output` (numbered paths with dimensions, source, and
credit when available), plus `files`, `folder`, and `results` (each with
`path`, `width`, `height`, `source`, `page`, `license`, `credit`).

## Recovery

- "No image found for '<x>'": retry once with a shorter subject (one or two
  words) or `source="wikimedia"`, then report the failure honestly. Do NOT
  fall back to the browser.
- "Not an image (...)" / "HTTP 404": the URL is a page or dead link; ask the
  user for a direct image URL or switch to a subject search.
- Some candidates fail while others succeed — that is normal; report the saved
  files, not the skipped ones.
- Network/DNS failures: retry once, then stop and say so.
