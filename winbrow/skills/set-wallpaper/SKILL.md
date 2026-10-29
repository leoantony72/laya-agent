---
name: set-wallpaper
skill: set_wallpaper
kind: tool
args:
  - name: image
    type: string
    required: true
    description: >
      Existing image path or direct image URL, or the subject/style to search
      for, e.g. "anime", "anime sunset", "cyberpunk city", "one piece".
description: >
  Change the desktop wallpaper: local image, direct image URL, or a subject
  such as anime or sunset. Downloads the image from Wallhaven itself — never
  opens a browser or reads a web page. Do not use browser_navigate or
  download_file for wallpapers.
---

# Wallpaper (search + download + apply, no browser)

## When to Use

Any request to change, set, or pick a desktop wallpaper or background — with
or without a subject:

- "change the wallpaper to something anime"
- "set my wallpaper to a cyberpunk city"
- "set the wallpaper to C:\Pictures\sunset.jpg"
- "make the desktop background this image: https://example.com/pic.jpg"

## When NOT to Use

- Never open a browser (`browser_navigate`) to find a wallpaper. This tool
  searches and downloads the image by itself.
- Never use `web_search`, `web_fetch`, or `download_file` for a wallpaper.
  They cost extra steps and produce a web page the user did not ask for.
- To only save images without changing the desktop, use `download_wallpaper`.

## Step Pattern

1. Pass the subject exactly as the user said it, minus filler
   ("anime", not "something related anime wallpaper"). Do not add the word
   "wallpaper" to the query.
2. Call `set_wallpaper(image="anime")`.
3. The result reports the saved file, its resolution, the source and the
   applied wallpaper path. That is the whole task — emit done.

## Args Detail

- `image` (required): an existing image path, a direct image URL, or a search
  subject. A bare subject is the common case.
- Files are saved to `Pictures\WinBrow Wallpapers`, so the user can find them.
- SFW only: Wallhaven is queried with `purity=100`. Do not try to bypass it.

## Output Shape

Returns `success`, `output` (one line naming the file, resolution, source and
the applied path), plus `path`, `applied`, `source`, `query`, `page`, `width`,
`height`.

## Recovery

- "No wallpaper found for '<x>'": retry once with a simpler subject (one or
  two words), then report the failure. Do NOT fall back to the browser.
- Naming an image path that does not exist is a user error: report the exact
  path that failed and ask for a valid one.
- Network/DNS failures are reported honestly; retry once, then stop.
