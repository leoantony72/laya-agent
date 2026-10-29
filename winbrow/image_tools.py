"""General-purpose image search and download — no browser, no API keys.

This is the shared engine behind every "get me an image" task, not just
wallpapers:

  download_images(query="a golden retriever", count=3, folder="Pictures")
  download_images(query="https://example.com/photo.jpg")   # direct URL
  download_images(query="anime", source="wallhaven")

Wallpaper handling (``winbrow.wallpaper``) is a thin layer on top: it calls
``search_images`` with landscape/high-resolution defaults, then applies the
downloaded file with ``SystemParametersInfoW``.

Sources (all keyless, all plain HTTP)
-------------------------------------
1. **Wallhaven** — general/anime/people art and photography; ``purity=100``
   keeps it SFW. Best for wallpapers, concept art, illustrations.
2. **Wikimedia Commons** — real photos with reusable licenses. Every result
   carries its license and credit line, which document workflows must show.
3. **NekosAPI** — random safe anime illustrations. Last resort.

Deliberately not used: Safebooru/nekos.best (HTTP 403 bot rules on many
networks), Unsplash/Pexels/Pixabay (need API keys), Bing/Google image scraping
(brittle and ToS-hostile), and ``download_file`` (it cannot validate that the
bytes are a real image).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from .registry import ArgSpec, Tool

log = logging.getLogger("winbrow.image_tools")

IMAGE_IMPL = "image"

WALLHAVEN_API = "https://wallhaven.cc/api/v1/search"
NEKOSAPI_RANDOM = "https://api.nekosapi.com/v4/images/random"

DEFAULT_COUNT = 3
MAX_COUNT = 10
DEFAULT_MIN_WIDTH = 800
MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024
SEARCH_TIMEOUT_S = 15
DOWNLOAD_TIMEOUT_S = 60
MAX_CANDIDATES = 12

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
# Windows and most document tools handle these directly; everything else is
# transcoded so a downloaded image is always usable.
NATIVE_FORMATS = ("JPEG", "PNG", "WEBP", "BMP", "GIF")
WALLPAPER_FORMATS = ("JPEG", "PNG")

FOLDER_NAMES = ("pictures", "downloads", "desktop", "documents")

_QUERY_FILLER = frozenset({
    "image", "images", "img", "imgs", "picture", "pictures", "pic", "pics",
    "photo", "photos", "photograph", "photographs", "wallpaper", "wallpapers",
    "background", "backgrounds", "download", "downloads", "save", "get",
    "fetch", "grab", "me", "some", "any", "a", "an", "the", "of", "for",
    "from", "to", "on", "in", "please", "internet", "web", "online", "file",
    "files", "related", "something", "about",
})


class ImageToolError(RuntimeError):
    """A search or download could not produce a usable image."""


@dataclass
class ImageCandidate:
    id: str
    url: str
    page: str
    width: int
    height: int
    source: str
    title: str = ""
    license: str = ""
    credit: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- http
def user_agent() -> str:
    return os.environ.get(
        "WINBROW_IMAGE_UA",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    )


def http_get(url: str, *, params: Optional[dict] = None, timeout: float = SEARCH_TIMEOUT_S):
    """GET with a browser TLS fingerprint when curl_cffi is installed.

    Returns an object exposing ``status_code``, ``headers`` and ``content``.
    Raises on transport errors so callers can try the next source.
    """
    headers = {"User-Agent": user_agent(), "Accept": "application/json,image/*,*/*"}
    try:
        from curl_cffi import requests as _cffi  # type: ignore
    except Exception:
        _cffi = None
    if _cffi is not None:
        return _cffi.get(
            url, params=params, headers=headers, impersonate="chrome124",
            timeout=timeout, allow_redirects=True,
        )
    import requests

    return requests.get(url, params=params, headers=headers, timeout=timeout,
                        allow_redirects=True)


def http_json(url: str, params: Optional[dict] = None,
              timeout: float = SEARCH_TIMEOUT_S) -> Any:
    response = http_get(url, params=params, timeout=timeout)
    if int(getattr(response, "status_code", 0)) != 200:
        raise ImageToolError(f"HTTP {response.status_code} from {urlparse(url).netloc}")
    return response.json()


# --------------------------------------------------------------------------- helpers
def clean_query(query: str) -> str:
    """Strip request filler so a subject line becomes real search terms."""
    words = [
        word for word in re.findall(r"[A-Za-z0-9'.-]+", str(query or "").lower())
        if word.strip(".") not in _QUERY_FILLER and len(word.strip(".")) > 1
    ]
    if not words:
        return ""
    return " ".join(words[:6])


def slug(text: str, fallback: str = "image") -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return cleaned[:40] or fallback


def unique_path(folder: Path, stem: str, suffix: str) -> Path:
    candidate = folder / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = folder / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


def resolve_folder(name: str = "", *, subfolder: str = "WinBrow Images") -> Path:
    """Desktop/Downloads/Pictures/Documents (or a real path) -> directory.

    ``WINBROW_IMAGE_DIR`` overrides every friendly alias with one exact folder;
    an explicit path argument always wins over both.
    """
    raw = str(name or "").strip()
    override = os.environ.get("WINBROW_IMAGE_DIR", "").strip()
    if raw and re.match(r"^([A-Za-z]:[\\/]|~|\\\\)", raw):
        folder = Path(os.path.expandvars(os.path.expanduser(raw)))
    elif override:
        folder = Path(os.path.expandvars(os.path.expanduser(override)))
    else:
        from . import files

        key = (raw.lower().rstrip(":") or "pictures")
        base = files._special_folder(key if key in FOLDER_NAMES else "pictures")
        if not base:
            base = os.path.join(files.user_home(), "Pictures")
        folder = Path(base)
        if key not in FOLDER_NAMES or key == "pictures":
            folder = folder / subfolder
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


def _looks_like_url(text: str) -> bool:
    parsed = urlparse(str(text or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


# --------------------------------------------------------------------------- sources
def search_wallhaven(query: str, count: int = MAX_CANDIDATES, *,
                     min_width: int = DEFAULT_MIN_WIDTH, min_height: int = 0,
                     landscape: bool = False,
                     max_bytes: int = MAX_DOWNLOAD_BYTES) -> list[ImageCandidate]:
    params = {
        "q": query,
        "purity": "100",            # SFW only — never relaxed
        "categories": "111",        # general + anime + people
        "sorting": "relevance" if query else "toplist",
        "order": "desc",
        "page": 1,
    }
    if min_width or min_height:
        params["atleast"] = f"{min_width or 1}x{min_height or 1}"
    if not query:
        params["topRange"] = "1M"
    data = http_json(WALLHAVEN_API, params=params)
    hits: list[ImageCandidate] = []
    for item in (data.get("data") or [])[: count * 2]:
        url = str(item.get("path") or "").strip()
        width = int(item.get("dimension_x") or 0)
        height = int(item.get("dimension_y") or 0)
        size = int(item.get("file_size") or 0)
        if not url or size > max_bytes:
            continue
        if width and width < min_width:
            continue
        if height and min_height and height < min_height:
            continue
        if landscape and width and height and width < height:
            continue
        hits.append(ImageCandidate(
            id=str(item.get("id") or ""),
            url=url,
            page=str(item.get("url") or ""),
            width=width,
            height=height,
            source="Wallhaven",
            title=str(item.get("id") or "image"),
        ))
        if len(hits) >= count:
            break
    return hits


def search_wikimedia(query: str, count: int = MAX_CANDIDATES, *,
                     min_width: int = DEFAULT_MIN_WIDTH) -> list[ImageCandidate]:
    """Licensed photos with credit lines (public domain / CC)."""
    from .image_search import search_images as commons_search

    results = commons_search(query, limit=count, min_width=max(400, min_width))
    return [
        ImageCandidate(
            id=str(index),
            url=item.url,
            page=item.page,
            width=item.width,
            height=item.height,
            source=item.source or "Wikimedia Commons",
            title=item.title,
            license=item.license,
            credit=item.credit,
        )
        for index, item in enumerate(results, start=1)
    ]


def search_nekosapi(count: int = MAX_CANDIDATES) -> list[ImageCandidate]:
    """Random safe anime art. No keyword search; fallback only."""
    data = http_json(NEKOSAPI_RANDOM, params={"limit": count, "rating": "safe"})
    if isinstance(data, dict):          # some versions wrap the list
        data = data.get("items") or data.get("data") or []
    hits: list[ImageCandidate] = []
    for item in data or []:
        url = str(item.get("url") or "").strip()
        if not url or str(item.get("rating") or "safe").lower() != "safe":
            continue
        hits.append(ImageCandidate(
            id=str(item.get("id") or ""),
            url=url,
            page=str(item.get("source") or ""),
            width=int(item.get("width") or 0),
            height=int(item.get("height") or 0),
            source="NekosAPI",
            title="anime art",
        ))
        if len(hits) >= count:
            break
    return hits


def search_images(
    query: str,
    *,
    count: int = DEFAULT_COUNT,
    source: str = "auto",
    min_width: int = DEFAULT_MIN_WIDTH,
    min_height: int = 0,
    landscape: bool = False,
) -> list[ImageCandidate]:
    """Search keyless image sources. Never opens a browser.

    ``source``: auto (Wallhaven -> Wikimedia -> NekosAPI), wallhaven,
    wikimedia, or nekosapi. Wallhaven is tried with the cleaned query and then
    a relaxed one-word query before the next source is used.
    """
    source = (source or "auto").strip().lower()
    if source not in {"auto", "wallhaven", "wikimedia", "nekosapi", "commons"}:
        raise ImageToolError(
            f"Unknown image source '{source}' (use auto, wallhaven, wikimedia, or nekosapi)"
        )
    if source == "commons":
        source = "wikimedia"

    if source == "nekosapi":
        hits = search_nekosapi(count)
        if hits:
            return hits
        raise ImageToolError("NekosAPI returned no safe images")

    if source == "wikimedia":
        hits = search_wikimedia(query, count, min_width=min_width)
        if hits:
            return hits
        raise ImageToolError(f"No licensed Wikimedia image found for '{query}'")

    cleaned = clean_query(query) or str(query or "").strip()
    wallhaven_queries: list[str] = []
    if cleaned:
        wallhaven_queries.append(cleaned)
        first = cleaned.split()[0]
        if first != cleaned:
            wallhaven_queries.append(first)

    errors: list[str] = []
    if source in {"auto", "wallhaven"}:
        for attempt in wallhaven_queries:
            try:
                hits = search_wallhaven(
                    attempt, count, min_width=min_width,
                    min_height=min_height, landscape=landscape,
                )
            except Exception as exc:
                errors.append(f"Wallhaven '{attempt}': {exc}")
                continue
            if hits:
                return hits
            errors.append(f"Wallhaven '{attempt}': no matches")
        if source == "wallhaven":
            raise ImageToolError(
                f"No Wallhaven image found for '{query or 'random'}'. "
                + "; ".join(errors[-2:])
            )

    if source in {"auto", "wikimedia"}:
        try:
            hits = search_wikimedia(cleaned or query, count, min_width=min_width)
        except Exception as exc:
            errors.append(f"Wikimedia: {exc}")
        else:
            if hits:
                return hits
            errors.append(f"Wikimedia '{cleaned or query}': no matches")

    if source == "auto":
        try:
            hits = search_nekosapi(count)
        except Exception as exc:
            errors.append(f"NekosAPI: {exc}")
        else:
            if hits:
                return hits
            errors.append("NekosAPI: no matches")

    raise ImageToolError(
        f"No image found for '{query}'. " + "; ".join(errors[-3:])
    )


# --------------------------------------------------------------------------- download
def download_image(
    url: str,
    *,
    folder: Optional[str | Path] = None,
    filename: str = "",
    min_width: int = 200,
    min_height: int = 0,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
    max_dimension: int = 0,
    timeout: float = DOWNLOAD_TIMEOUT_S,
    allowed_formats: tuple[str, ...] = NATIVE_FORMATS,
    allow_animated: bool = False,
) -> dict[str, Any]:
    """Download one image, validate it with Pillow, and save it locally.

    ``folder`` may be a directory or a friendly name (Pictures/Downloads/...).
    ``filename`` is used as the stem only; the extension follows the real
    format (or the transcoded format when ``allowed_formats`` excludes it).
    """
    if not _looks_like_url(url):
        raise ImageToolError(f"Not an http(s) image URL: {url}")
    target_dir = (Path(folder) if isinstance(folder, Path)
                  else resolve_folder(str(folder or "")))
    target_dir.mkdir(parents=True, exist_ok=True)

    response = http_get(url, timeout=timeout)
    status = int(getattr(response, "status_code", 0))
    if status != 200:
        raise ImageToolError(f"HTTP {status} downloading {urlparse(url).netloc}")
    content_type = str(response.headers.get("content-type") or "").lower()
    if content_type and not content_type.startswith("image/"):
        raise ImageToolError(f"Not an image ({content_type})")
    raw = response.content
    if len(raw) > max_bytes:
        raise ImageToolError(f"Image is larger than {max_bytes // (1024 * 1024)} MB")
    if len(raw) < 512:
        raise ImageToolError("Response is too small to be an image")

    from PIL import Image, ImageOps

    stem = slug(filename or Path(urlparse(url).path).stem or "image")
    with Image.open(BytesIO(raw)) as probe:
        fmt = (probe.format or "").upper()
        if getattr(probe, "n_frames", 1) > 1 and not allow_animated:
            raise ImageToolError("Animated image — not usable here")
        image = ImageOps.exif_transpose(probe)
        width, height = image.size
        if width < min_width or (min_height and height < min_height):
            raise ImageToolError(
                f"Only {width}x{height}, below {min_width}x{min_height or 0}"
            )
        must_convert = fmt not in allowed_formats
        must_resize = bool(max_dimension and max(image.size) > max_dimension)
        if not must_convert and not must_resize:
            suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp",
                      "BMP": ".bmp", "GIF": ".gif"}.get(fmt, ".png")
            path = unique_path(target_dir, stem, suffix)
            path.write_bytes(raw)
        else:
            suffix = ".jpg" if ("JPEG" in allowed_formats and fmt == "JPEG") else ".png"
            if must_resize:
                image.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
            if suffix == ".png":
                image = image.convert(
                    "RGBA" if image.mode in {"RGBA", "LA", "P"} else "RGB"
                )
            elif image.mode != "RGB":
                image = image.convert("RGB")
            path = unique_path(target_dir, stem, suffix)
            image.save(path, "PNG" if suffix == ".png" else "JPEG", quality=95)
            width, height = image.size

    return {
        "path": str(path),
        "filename": path.name,
        "width": width,
        "height": height,
        "bytes": path.stat().st_size,
        "format": path.suffix.lstrip(".").upper(),
        "url": url,
        "folder": str(target_dir),
    }


# --------------------------------------------------------------------------- orchestration
def download_images(
    query: str,
    *,
    count: Any = DEFAULT_COUNT,
    folder: str = "Pictures",
    source: str = "auto",
    min_width: Any = DEFAULT_MIN_WIDTH,
    landscape: bool = False,
) -> dict[str, Any]:
    """Search (or take a direct URL) and save 1-N images for any task."""
    query = str(query or "").strip()
    if not query:
        raise ImageToolError("An image subject or direct image URL is required.")
    want = _int(count, DEFAULT_COUNT, 1, MAX_COUNT)
    floor = _int(min_width, DEFAULT_MIN_WIDTH, 100, 8000)

    if _looks_like_url(query):
        info = download_image(query, folder=folder, min_width=min(floor, 200))
        info.update({"source": "URL", "page": ""})
        return {
            "success": True,
            "output": f"Downloaded 1 image from {query}:\n1. {info['path']} "
                      f"({info['width']}x{info['height']})",
            "query": query,
            "folder": info["folder"],
            "files": [info["path"]],
            "results": [info],
        }

    candidates = search_images(
        query, count=want * 2, source=source, min_width=floor, landscape=landscape,
    )
    saved: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if len(saved) >= want:
            break
        if candidate.url in seen:
            continue
        seen.add(candidate.url)
        try:
            info = download_image(
                candidate.url,
                folder=folder,
                filename=f"{slug(clean_query(query) or 'image')}-{slug(candidate.id, 'img')}",
                min_width=floor,
            )
        except Exception as exc:
            errors.append(f"{candidate.source} {candidate.id}: {exc}")
            continue
        info.update({
            "source": candidate.source,
            "page": candidate.page,
            "title": candidate.title,
            "license": candidate.license,
            "credit": candidate.credit,
        })
        saved.append(info)
    if not saved:
        raise ImageToolError(
            f"Could not download any image for '{query}'. " + "; ".join(errors[-2:])
        )

    folder_used = saved[0]["folder"]
    lines = []
    for index, item in enumerate(saved, start=1):
        line = (f"{index}. {item['path']} ({item['width']}x{item['height']}, "
                f"{item['source']})")
        if item.get("credit"):
            line += f"\n   credit: {item['credit']}"
        lines.append(line)
    return {
        "success": True,
        "output": f"Downloaded {len(saved)} image(s) for '{query}' into "
                  f"{folder_used}:\n" + "\n".join(lines),
        "query": query,
        "folder": folder_used,
        "files": [item["path"] for item in saved],
        "results": saved,
    }


def execute_image_tool(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    args = args or {}
    try:
        if tool_name == "download_images":
            return download_images(
                str(args.get("query") or args.get("image") or args.get("url") or ""),
                count=args.get("count", DEFAULT_COUNT),
                folder=str(args.get("folder") or "Pictures"),
                source=str(args.get("source") or "auto"),
                min_width=args.get("min_width", DEFAULT_MIN_WIDTH),
            )
        return {"success": False, "output": f"Unknown image tool '{tool_name}'"}
    except ImageToolError as exc:
        return {"success": False, "output": str(exc)}
    except Exception as exc:      # network/DNS/Pillow failures must not crash the loop
        log.warning("Image tool '%s' failed: %s", tool_name, exc)
        return {"success": False, "output": f"{tool_name} failed: {exc}"}


# --------------------------------------------------------------------------- tools
def get_image_tools() -> list[Tool]:
    return [
        Tool(
            name="download_images",
            description=(
                "Download images for ANY task: pass a subject ('golden retriever', "
                "'anime scenery', 'eiffel tower') or a direct image URL. Searches "
                "keyless sources (Wallhaven art, licensed Wikimedia photos, safe "
                "anime art), validates each file with Pillow and saves it into "
                "Pictures\\WinBrow Images or the folder the user asked for. No "
                "browser is opened. Use this instead of browser_navigate, "
                "web_search, or download_file whenever the user wants pictures."
            ),
            script="",
            impl=IMAGE_IMPL,
            args=[
                ArgSpec(
                    name="query", kind="text",
                    instructions=(
                        "Image subject (e.g. 'golden retriever', 'anime scenery') "
                        "or a direct http(s) image URL. Never a web page URL."
                    ),
                ),
                ArgSpec(
                    name="count", kind="text",
                    instructions="How many images to download (1-10).",
                    default=str(DEFAULT_COUNT),
                ),
                ArgSpec(
                    name="folder", kind="enum",
                    instructions="Where to save the images.",
                    criteria={
                        "Pictures": "Pictures\\WinBrow Images (default)",
                        "Downloads": "the Downloads folder",
                        "Desktop": "the Desktop",
                        "Documents": "the Documents library",
                    },
                    default="Pictures",
                ),
                ArgSpec(
                    name="source", kind="enum",
                    instructions="Optional source preference.",
                    criteria={
                        "auto": "Wallhaven, then licensed Wikimedia photos, then safe anime art",
                        "wallhaven": "art and photography (SFW)",
                        "wikimedia": "licensed real photos with credit lines",
                        "nekosapi": "random safe anime illustrations",
                    },
                    default="auto",
                ),
                ArgSpec(
                    name="min_width", kind="text",
                    instructions="Minimum width in pixels (default 800).",
                    default=str(DEFAULT_MIN_WIDTH),
                ),
            ],
            speak="result",
            examples=[
                "download 3 pictures of a golden retriever",
                "save some anime scenery images to downloads",
                "download this image https://example.com/pic.jpg",
                "get images of the eiffel tower for my document",
            ],
        ),
    ]
