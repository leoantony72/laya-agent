"""Desktop wallpaper layer: find, download (no browser) and apply.

The search/download engine is the general-purpose one in
:mod:`winbrow.image_tools`; this module only adds wallpaper-specific policy:

* landscape, high-resolution candidates (>= WINBROW_WALLPAPER_MIN, default
  1600x900) from Wallhaven's SFW API, with safe-anime art as a fallback;
* downloads land in ``Pictures\\WinBrow Wallpapers``;
* only JPEG/PNG are kept, because Windows applies those reliably;
* the file is applied with ``SystemParametersInfoW`` and read back to verify.

Any other "download me some pictures" task belongs to ``download_images`` in
``image_tools`` — this module is not the general image path.
"""

from __future__ import annotations

import ctypes
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from .image_tools import (
    IMAGE_EXTS,
    WALLPAPER_FORMATS,
    ImageCandidate,
    ImageToolError,
    clean_query,
    download_image as _download_image,
    search_images as _search_images,
)
from .registry import ArgSpec, Tool

log = logging.getLogger("winbrow.wallpaper")

WALLPAPER_IMPL = "wallpaper"

DEFAULT_MIN_RESOLUTION = (1600, 900)
MAX_CANDIDATES = 8

# SPI_SETDESKWALLPAPER = 20; SPIF_UPDATEINIFILE | SPIF_SENDWININICHANGE
_SPI_SETDESKWALLPAPER = 20
_SPI_GETDESKWALLPAPER = 0x0073
_SPIF_UPDATE_INI_FILE = 0x01
_SPIF_SEND_CHANGE = 0x02
_WALLPAPER_STYLE_FILL = "10"   # Control Panel\Desktop\WallpaperStyle


class WallpaperError(RuntimeError):
    """Raised when no usable wallpaper could be found or applied."""


# Kept as an alias so callers/tests can keep using the wallpaper name.
WallpaperHit = ImageCandidate


# --------------------------------------------------------------------------- config
def _default_query() -> str:
    """Subject used when the user asks for 'a wallpaper' with no topic.

    Read at call time: server.py loads .env after this module is imported.
    """
    return (os.environ.get("WINBROW_WALLPAPER_QUERY") or "anime").strip()


def _resolution_floor() -> tuple[int, int]:
    raw = (os.environ.get("WINBROW_WALLPAPER_MIN") or "1600x900").lower().replace(" ", "")
    match = re.match(r"^(\d{3,5})[x*](\d{3,5})$", raw)
    if not match:
        return DEFAULT_MIN_RESOLUTION
    return int(match.group(1)), int(match.group(2))


def wallpaper_dir() -> Path:
    """Where downloaded wallpapers are kept (created on demand)."""
    override = os.environ.get("WINBROW_WALLPAPER_DIR", "").strip()
    if override:
        path = Path(os.path.expandvars(os.path.expanduser(override)))
    else:
        pictures = Path(os.environ.get("USERPROFILE") or Path.home()) / "Pictures"
        path = pictures / "WinBrow Wallpapers"
    path.mkdir(parents=True, exist_ok=True)
    return path


# --------------------------------------------------------------------------- search
def search_wallpapers(query: str, *, limit: int = MAX_CANDIDATES) -> list[ImageCandidate]:
    """Search keyless wallpaper sources. Never opens a browser.

    Tries the cleaned subject, a relaxed one-word subject, then the configured
    default subject; safe anime art is the last resort.
    """
    cleaned = clean_query(query)
    attempts: list[str] = []
    if cleaned:
        attempts.append(cleaned)
        first = cleaned.split()[0]
        if first != cleaned:
            attempts.append(first)
    default = _default_query()
    if default and default.lower() not in attempts:
        attempts.append(default)

    min_width, min_height = _resolution_floor()
    errors: list[str] = []
    for attempt in attempts:
        try:
            hits = _search_images(
                attempt, count=limit, source="wallhaven",
                min_width=min_width, min_height=min_height, landscape=True,
            )
        except ImageToolError as exc:
            errors.append(str(exc))
            continue
        if hits:
            return hits[:limit]

    try:
        hits = _search_images("anime", count=limit, source="nekosapi")
    except ImageToolError as exc:
        errors.append(str(exc))
    else:
        if hits:
            return hits[:limit]

    raise WallpaperError(
        "No wallpaper found for "
        f"'{query or _default_query()}'. " + "; ".join(errors[-3:])
    )


# --------------------------------------------------------------------------- download
def download_wallpaper(url: str, *, folder: Optional[Path] = None,
                       stem: str = "wallpaper") -> dict[str, Any]:
    """Download one image and keep only formats Windows can apply."""
    try:
        return _download_image(
            url,
            folder=Path(folder) if folder else wallpaper_dir(),
            filename=stem,
            min_width=_resolution_floor()[0],
            min_height=_resolution_floor()[1],
            allowed_formats=WALLPAPER_FORMATS,
            allow_animated=False,
        )
    except ImageToolError as exc:
        raise WallpaperError(str(exc)) from exc


# --------------------------------------------------------------------------- apply
def _ensure_fill_style() -> None:
    """Use 'Fill' so any aspect ratio covers the screen without letterboxing."""
    if os.name != "nt" or os.environ.get("WINBROW_WALLPAPER_KEEP_STYLE") == "1":
        return
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop", 0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.SetValueEx(key, "WallpaperStyle", 0, winreg.REG_SZ, _WALLPAPER_STYLE_FILL)
            winreg.SetValueEx(key, "TileWallpaper", 0, winreg.REG_SZ, "0")
    except Exception as exc:      # styling is best-effort, never fatal
        log.debug("Could not set wallpaper fill style: %s", exc)


def set_desktop_wallpaper(path: str | Path) -> str:
    """Apply an image as the desktop wallpaper and verify the change."""
    if os.name != "nt":
        raise WallpaperError("Setting the desktop wallpaper requires Windows")
    resolved = str(Path(path).expanduser().resolve())
    if not os.path.isfile(resolved):
        raise WallpaperError(f"Wallpaper file does not exist: {resolved}")
    _ensure_fill_style()
    flags = _SPIF_UPDATE_INI_FILE | _SPIF_SEND_CHANGE
    if not ctypes.windll.user32.SystemParametersInfoW(
        _SPI_SETDESKWALLPAPER, 0, resolved, flags
    ):
        raise ctypes.WinError()
    applied = get_desktop_wallpaper()
    if applied and os.path.normcase(applied) != os.path.normcase(resolved):
        log.warning("Wallpaper read-back differs: %r vs %r", applied, resolved)
    return applied or resolved


def get_desktop_wallpaper() -> str:
    """Current wallpaper path as Windows reports it ('' when unknown)."""
    if os.name != "nt":
        return ""
    try:
        buf = ctypes.create_unicode_buffer(32_768)
        if ctypes.windll.user32.SystemParametersInfoW(
            _SPI_GETDESKWALLPAPER, len(buf), buf, 0
        ):
            return str(buf.value or "")
    except Exception as exc:
        log.debug("Could not read the current wallpaper: %s", exc)
    return ""


# --------------------------------------------------------------------------- orchestration
def _existing_image(ref: str) -> Optional[Path]:
    """Resolve a local image reference (path, ~/path, or bare filename)."""
    text = str(ref or "").strip().strip('"').strip("'")
    if not text:
        return None
    if text.lower().startswith("file://"):
        text = text[7:]
    candidate = Path(os.path.expandvars(os.path.expanduser(text)))
    if candidate.is_file():
        return candidate
    if Path(text).suffix.lower() in IMAGE_EXTS \
            and not any(sep in text for sep in "\\/"):
        from .files import find_file_by_name

        found = find_file_by_name(text)
        if found:
            return Path(found)
    return None


def _looks_like_path(ref: str) -> bool:
    text = str(ref or "").strip()
    return bool(re.match(r"^[A-Za-z]:[\\/]", text)) or text.startswith(("~", "\\\\"))


def set_wallpaper(ref: str = "", *, query_fallback: bool = True) -> dict[str, Any]:
    """Set the wallpaper from a local file, a direct URL, or a search subject.

    No browser is opened at any point; every remote step is a plain HTTP
    request handled by :mod:`winbrow.image_tools`.
    """
    ref = str(ref or "").strip()
    if not ref:
        ref = _default_query()

    source = "local"
    page = ""
    hit: Optional[ImageCandidate] = None

    local = _existing_image(ref)
    if local is not None:
        path = local
    elif ref.lower().startswith(("http://", "https://")):
        source = "url"
        info = download_wallpaper(
            ref, stem=Path(urlparse(ref).path).stem or "wallpaper"
        )
        path = Path(info["path"])
    elif _looks_like_path(ref) or Path(ref).suffix.lower() in IMAGE_EXTS:
        raise WallpaperError(
            f"No image found at '{ref}'. Give a full path to an existing image, "
            "or a subject to search for (for example 'anime')."
        )
    elif not query_fallback:
        raise WallpaperError("A search subject is required")
    else:
        hits = search_wallpapers(ref)
        if not hits:
            raise WallpaperError(f"No wallpaper found for '{ref}'")
        errors: list[str] = []
        path = None
        for candidate in hits:
            try:
                info = download_wallpaper(
                    candidate.url,
                    stem=f"{clean_query(ref) or 'wallpaper'}-{candidate.id}",
                )
            except Exception as exc:
                errors.append(f"{candidate.source} {candidate.id}: {exc}")
                continue
            path = Path(info["path"])
            source = candidate.source
            page = candidate.page
            hit = candidate
            break
        if path is None:
            raise WallpaperError(
                f"Found {len(hits)} candidate(s) for '{ref}' but none downloaded. "
                + "; ".join(errors[-2:])
            )

    applied = set_desktop_wallpaper(path)
    detail = f"{hit.width}x{hit.height} {source}" if hit and hit.width else source
    message = f"Wallpaper set to {path.name} ({detail}): {applied}"
    return {
        "success": True,
        "output": message,
        "path": str(path),
        "applied": applied,
        "source": source,
        "query": clean_query(ref) if source not in {"local", "url"} else "",
        "page": page,
        "width": hit.width if hit else "",
        "height": hit.height if hit else "",
    }


def find_wallpapers(query: str, count: int = 3) -> dict[str, Any]:
    """Download wallpaper-shaped images without changing the desktop."""
    query = str(query or "").strip() or _default_query()
    try:
        limit = max(1, min(int(str(count).strip() or "3"), 8))
    except (TypeError, ValueError):
        limit = 3
    hits = search_wallpapers(query, limit=limit * 2)
    saved: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for candidate in hits:
        if len(saved) >= limit:
            break
        if candidate.url in seen:
            continue
        seen.add(candidate.url)
        try:
            info = download_wallpaper(
                candidate.url,
                stem=f"{clean_query(query) or 'wallpaper'}-{candidate.id}",
            )
        except Exception as exc:
            errors.append(f"{candidate.source} {candidate.id}: {exc}")
            continue
        info.update({"source": candidate.source, "page": candidate.page})
        saved.append(info)
    if not saved:
        raise WallpaperError(
            f"Could not download any wallpaper for '{query}'. "
            + "; ".join(errors[-2:])
        )
    lines = [
        f"{index}. {item['path']} ({item['width']}x{item['height']}, {item['source']})"
        for index, item in enumerate(saved, start=1)
    ]
    return {
        "success": True,
        "output": f"Downloaded {len(saved)} wallpaper(s) for '{query}' "
                  f"into {wallpaper_dir()}:\n" + "\n".join(lines),
        "query": query,
        "folder": str(wallpaper_dir()),
        "files": [item["path"] for item in saved],
        "results": saved,
    }


def execute_wallpaper_tool(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    args = args or {}
    try:
        if tool_name == "set_wallpaper":
            ref = str(
                args.get("image") or args.get("query") or args.get("subject") or ""
            ).strip()
            return set_wallpaper(ref)
        if tool_name == "download_wallpaper":
            return find_wallpapers(
                str(args.get("query") or args.get("image") or ""),
                args.get("count", 3),
            )
        return {"success": False, "output": f"Unknown wallpaper tool '{tool_name}'"}
    except WallpaperError as exc:
        return {"success": False, "output": str(exc)}
    except Exception as exc:       # network/DNS/pillow failures must not crash the loop
        log.warning("Wallpaper tool '%s' failed: %s", tool_name, exc)
        return {"success": False, "output": f"{tool_name} failed: {exc}"}


# --------------------------------------------------------------------------- tools
def get_wallpaper_tools() -> list[Tool]:
    return [
        Tool(
            name="set_wallpaper",
            description=(
                "Change the Windows desktop wallpaper to a local image, a direct "
                "image URL, or a subject/style to search for (anime, sunset, "
                "cyberpunk...). It searches Wallhaven's keyless API, downloads a "
                "full-resolution image into Pictures\\WinBrow Wallpapers and "
                "applies it. No browser is opened. For saving images for any "
                "other purpose use download_images instead."
            ),
            script="",
            impl=WALLPAPER_IMPL,
            args=[
                ArgSpec(
                    name="image", kind="text",
                    instructions=(
                        "An existing image path/URL, or the subject or style to "
                        "search for, e.g. 'anime', 'anime sunset', 'cyberpunk city'. "
                        "Never paste a web page URL — only a direct image URL."
                    ),
                ),
            ],
            speak="result",
            examples=[
                "change wallpaper to anime",
                "set wallpaper to a cyberpunk city",
                "set wallpaper to C:\\Pictures\\sunset.jpg",
            ],
        ),
        Tool(
            name="download_wallpaper",
            description=(
                "Download wallpaper-shaped images (landscape, 1600x900+) for a "
                "subject into Pictures\\WinBrow Wallpapers without changing the "
                "desktop. No browser opened. For ordinary images use "
                "download_images."
            ),
            script="",
            impl=WALLPAPER_IMPL,
            args=[
                ArgSpec(
                    name="query", kind="text",
                    instructions="Subject/style, e.g. 'anime', 'anime scenery', 'neon city'.",
                ),
                ArgSpec(
                    name="count", kind="text",
                    instructions="How many wallpapers to download (1-8).",
                    default="3",
                ),
            ],
            speak="result",
            examples=[
                "download some anime wallpapers",
                "save 5 cyberpunk wallpapers",
            ],
        ),
    ]
