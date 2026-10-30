---
name: "set-desktop-wallpaper-from-web"
skill: "set_desktop_wallpaper_from_web"
args:
  - name: "search_query"
    type: "string"
    required: true
    default: null
    description: "Theme or keyword describing the desired wallpaper (e.g. an anime, character, colour scheme or mood)."
  - name: "resolution"
    type: "string"
    required: false
    default: null
    description: "Preferred image resolution such as '1920x1080' or '4K'. If omitted, pick the highest available desktop resolution."
  - name: "wallpaper_site"
    type: "string"
    required: false
    default: null
    description: "Optional preferred wallpaper website to source from. If omitted, use a general wallpaper site that offers free full-resolution downloads."
description: >
  Find a wallpaper matching a theme on a wallpaper website, download the full-
  resolution image, and set it as the desktop background. Use when the user
  wants a new desktop wallpaper sourced from the web by topic. Do not use when
  the image already exists locally or when the user only wants to browse
  wallpapers without applying one.
---

## When to Use

Use when the user asks to change the desktop wallpaper and wants the image sourced from the web based on a theme, keyword or subject.

## When NOT to Use

Do not use when the wallpaper file already exists locally, when the user only wants to browse or preview wallpapers, or when the request is about a non-desktop background such as a phone lock screen.

## Step Pattern

1. Run web_search for '{search_query} HD wallpaper' to find wallpaper sites hosting the theme.
2. browser_navigate to the chosen wallpaper site's search or category page for '{search_query}'.
3. If a bot/security verification page appears, browser_wait a few seconds and re-read the page; if it persists, fall back to an alternative wallpaper site.
4. Read the page and locate a wallpaper thumbnail matching the theme; click it to open the detail page.
5. On the detail page, locate the free full-resolution download control and click it, or extract the direct image URL (og:image / download link).
6. If '{resolution}' was provided, prefer a wallpaper matching that resolution; otherwise choose the highest available desktop resolution.
7. Download the image file to a local path.
8. Set the downloaded image as the desktop background.

## Args Detail

- `search_query` (string, required): Theme or keyword describing the desired wallpaper (e.g. an anime, character, colour scheme or mood).
- `resolution` (string, default=None): Preferred image resolution such as '1920x1080' or '4K'. If omitted, pick the highest available desktop resolution.
- `wallpaper_site` (string, default=None): Optional preferred wallpaper website to source from. If omitted, use a general wallpaper site that offers free full-resolution downloads.

## Output Shape

Returns a result dict with `success`, `output`, and any state fields the
chosen tool provides. A successful step means the tool dispatched the action;
it does not by itself prove the wider goal is complete. Verify with the next
read step before declaring the task done.

## Recovery

If a wallpaper site returns a bot/security challenge or HTTP 403, wait briefly and retry, then switch to a different wallpaper site that offers free full-resolution downloads. If the download control does not yield a direct image URL, extract the image source from the page metadata instead. If the chosen image fails to download, select another wallpaper matching the same theme. If setting the wallpaper fails, verify the downloaded file is a valid image and retry the apply step.
