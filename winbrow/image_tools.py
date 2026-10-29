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
1. **General web images** via ``ddgs`` (DuckDuckGo/Bing metasearch) — the
   default for real-world subjects. Results carry a source page; their license
   is unknown unless a license filter is requested.
2. **Wikimedia Commons** — real photos with reusable licenses. Every result
   carries its license and credit line, which document workflows must show.
3. **Wallhaven** — art/wallpapers; ``purity=100`` keeps it SFW. Used only for
   art/anime/wallpaper intent, and anime is excluded for real subjects.
4. **NekosAPI** — random safe anime illustrations. Anime intent only; never a
   fallback for a real-world subject.

The search-engine path is a scraper (no SLA). It is validated with Pillow and
used at low volume; for reusable document assets use ``image_search.py``
(Wikimedia/CC) instead. ``download_file`` is still not used because it cannot
validate that the bytes are a real image.
"""

from __future__ import annotations

import hashlib
import html as _html
import logging
import os
import re
import time
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

# Intent terms. "art" alone is deliberately absent: "art museum" is a photo
# request, not a request for a digital painting.
_ANIME_TERMS = frozenset({
    "anime", "manga", "waifu", "vtuber", "fanart", "chibi", "isekai",
})
_ART_TERMS = frozenset({
    "anime", "manga", "waifu", "vtuber", "fanart", "chibi", "isekai",
    "illustration", "artwork", "wallpaper", "wallpapers", "concept",
    "cyberpunk", "fantasy",
})

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


# Repeated planner retries should not re-hit the search engine.
_WEB_CACHE_TTL_S = 10 * 60
_WEB_CACHE: dict[tuple, tuple[float, list[ImageCandidate]]] = {}


# --------------------------------------------------------------------------- http
def user_agent() -> str:
    return os.environ.get(
        "WINBROW_IMAGE_UA",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    )


def http_get(
    url: str,
    *,
    params: Optional[dict] = None,
    timeout: float = SEARCH_TIMEOUT_S,
    headers: Optional[dict[str, str]] = None,
):
    """GET with a browser TLS fingerprint when curl_cffi is installed.

    Returns an object exposing ``status_code``, ``headers`` and ``content``.
    Raises on transport errors so callers can try the next source.
    """
    request_headers = {
        "User-Agent": user_agent(),
        "Accept": "application/json,image/*,*/*",
    }
    if headers:
        request_headers.update(headers)
    headers = request_headers
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


def _query_terms(query: str) -> set[str]:
    """Raw intent words; keep 'wallpaper' even though clean_query drops it."""
    return set(re.findall(r"[a-z0-9]+", str(query or "").lower()))


def is_anime_query(query: str) -> bool:
    return bool(_query_terms(query) & _ANIME_TERMS)


def is_art_query(query: str) -> bool:
    if is_anime_query(query):
        return True
    raw = str(query or "").lower()
    return bool(_query_terms(query) & _ART_TERMS) or "digital art" in raw


def wallhaven_categories(query: str) -> str:
    """Wallhaven bitmask: general=100, anime=010, people=001."""
    return "010" if is_anime_query(query) else "101"


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
                     categories: str = "111",
                     max_bytes: int = MAX_DOWNLOAD_BYTES) -> list[ImageCandidate]:
    categories = str(categories or "111").strip()
    if len(categories) != 3 or set(categories) - {"0", "1"}:
        categories = "111"
    params = {
        "q": query,
        "purity": "100",            # SFW only — never relaxed
        "categories": categories,   # general=100, anime=010, people=001
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
        category = str(item.get("category") or "").lower()
        if (
            (category == "anime" and categories[1] == "0")
            or (category == "general" and categories[0] == "0")
            or (category == "people" and categories[2] == "0")
        ):
            continue
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


def _web_rows(query: str, limit: int, **kwargs: Any) -> list[dict]:
    """Thin ddgs wrapper so tests can replace the network call."""
    from ddgs import DDGS

    return DDGS().images(query=query, max_results=limit, **kwargs)


def search_web_images(
    query: str,
    count: int = MAX_CANDIDATES,
    *,
    min_width: int = DEFAULT_MIN_WIDTH,
    min_height: int = 0,
    landscape: bool = False,
    photo: bool = True,
    license_filter: str = "",
) -> list[ImageCandidate]:
    """Keyless general web image search through DuckDuckGo/Bing (ddgs).

    This is the general-purpose path: it finds real photos of arbitrary
    subjects that art/CC sources do not cover. Results carry the source page;
    their license is unknown unless ``license_filter`` is set.
    """
    want = _int(count, MAX_CANDIDATES, 1, MAX_COUNT)
    cache_key = (
        query, want, min_width, min_height, landscape, photo, license_filter,
    )
    cached = _WEB_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < _WEB_CACHE_TTL_S:
        return list(cached[1])

    limit = max(1, min(want * 2, 30))
    kwargs: dict[str, Any] = {"region": "wt-wt", "safesearch": "moderate"}
    if photo:
        kwargs["type_image"] = "photo"
    kwargs["size"] = "Large"
    if license_filter:
        kwargs["license_image"] = license_filter
    try:
        rows = _web_rows(query, limit, **kwargs)
    except TypeError:
        # Older ddgs versions reject some filters; keep the search working.
        try:
            rows = _web_rows(query, limit, safesearch="moderate")
        except Exception as exc:
            raise ImageToolError(f"Web image search failed: {exc}") from exc
    except ImportError as exc:  # pragma: no cover - in requirements
        raise ImageToolError(
            "General web image search needs the 'ddgs' package. "
            "Install it with: pip install ddgs"
        ) from exc
    except Exception as exc:
        raise ImageToolError(f"Web image search failed: {exc}") from exc

    hits: list[ImageCandidate] = []
    seen: set[str] = set()
    for row in rows or []:
        url = _html.unescape(str(row.get("image") or "")).strip()
        if url.startswith("//"):
            url = "https:" + url
        if not url or url in seen:
            continue
        page = str(row.get("url") or "").strip()
        width = _int(row.get("width"), 0, 0, 100_000)
        height = _int(row.get("height"), 0, 0, 100_000)
        if width and width < min_width:
            continue
        if min_height and height and height < min_height:
            continue
        if landscape and width and height and width < height:
            continue
        title = str(row.get("title") or "").strip()
        domain = str(row.get("source") or "").strip() or urlparse(url).netloc
        if not title:
            title = domain or "web image"
        seen.add(url)
        hits.append(ImageCandidate(
            id=hashlib.sha1(url.encode("utf-8")).hexdigest()[:10],
            url=url,
            page=page,
            width=width,
            height=height,
            source=f"Web ({domain})" if domain else "Web",
            title=title,
            license="unknown",
            credit=f"{title} — {page}" if page else title,
        ))
        if len(hits) >= want:
            break
    if hits:
        _WEB_CACHE[cache_key] = (time.time(), list(hits))
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

    ``source``: auto, web (general DuckDuckGo/Bing photos), wallhaven,
    wikimedia, or nekosapi. ``auto`` is intent-aware: real subjects use general
    web photos first; art/anime uses Wallhaven; NekosAPI is anime-only and is
    never a fallback for a real-world subject.
    """
    source = (source or "auto").strip().lower()
    if source not in {"auto", "web", "wallhaven", "wikimedia", "nekosapi", "commons"}:
        raise ImageToolError(
            f"Unknown image source '{source}' "
            "(use auto, web, wallhaven, wikimedia, or nekosapi)"
        )
    if source == "commons":
        source = "wikimedia"
    cleaned = clean_query(query) or str(query or "").strip()

    if source == "nekosapi":
        hits = search_nekosapi(count)
        if hits:
            return hits
        raise ImageToolError("NekosAPI returned no safe images")

    if source == "web":
        try:
            hits = search_web_images(
                cleaned or query, count, min_width=min_width,
                min_height=min_height, landscape=landscape, photo=True,
            )
        except Exception as exc:
            raise ImageToolError(f"Web image search failed: {exc}") from exc
        if hits:
            return hits
        raise ImageToolError(f"No web image found for '{query}'")

    if source == "wikimedia":
        hits = search_wikimedia(cleaned or query, count, min_width=min_width)
        if hits:
            return hits
        raise ImageToolError(f"No licensed Wikimedia image found for '{query}'")

    errors: list[str] = []

    def wallhaven_attempts(categories: str) -> list[ImageCandidate]:
        tries: list[str] = []
        if cleaned:
            tries.append(cleaned)
            first = cleaned.split()[0]
            if first != cleaned:
                tries.append(first)
        for attempt in tries:
            try:
                found = search_wallhaven(
                    attempt, count, min_width=min_width,
                    min_height=min_height, landscape=landscape,
                    categories=categories,
                )
            except Exception as exc:
                errors.append(f"Wallhaven '{attempt}': {exc}")
                continue
            if found:
                return found
            errors.append(f"Wallhaven '{attempt}': no matches")
        return []

    if source == "wallhaven":
        hits = wallhaven_attempts(wallhaven_categories(query))
        if hits:
            return hits
        raise ImageToolError(
            f"No Wallhaven image found for '{query or 'random'}'. "
            + "; ".join(errors[-2:])
        )

    anime = is_anime_query(query)
    art = is_art_query(query)

    if anime:
        # Anime requests get art first, then the anime API, then general web.
        hits = wallhaven_attempts("010")
        if hits:
            return hits
        try:
            hits = search_nekosapi(count)
        except Exception as exc:
            errors.append(f"NekosAPI: {exc}")
        else:
            if hits:
                return hits
            errors.append("NekosAPI: no matches")
        try:
            hits = search_web_images(
                cleaned or query, count, min_width=min_width,
                min_height=min_height, landscape=landscape, photo=False,
            )
        except Exception as exc:
            errors.append(f"web: {exc}")
        else:
            if hits:
                return hits
            errors.append("web: no matches")
    elif art:
        # Digital art/illustrations: art source first, no anime mixed in.
        hits = wallhaven_attempts("101")
        if hits:
            return hits
        try:
            hits = search_web_images(
                cleaned or query, count, min_width=min_width,
                min_height=min_height, landscape=landscape, photo=False,
            )
        except Exception as exc:
            errors.append(f"web: {exc}")
        else:
            if hits:
                return hits
            errors.append("web: no matches")
        try:
            hits = search_wikimedia(cleaned or query, count, min_width=min_width)
        except Exception as exc:
            errors.append(f"Wikimedia: {exc}")
        else:
            if hits:
                return hits
            errors.append("Wikimedia: no matches")
    else:
        # Real-world subjects: general web photos are the default.
        try:
            hits = search_web_images(
                cleaned or query, count, min_width=min_width,
                min_height=min_height, landscape=landscape, photo=True,
            )
        except Exception as exc:
            errors.append(f"web: {exc}")
        else:
            if hits:
                return hits
            errors.append("web: no matches")

        try:
            hits = search_wikimedia(cleaned or query, count, min_width=min_width)
        except Exception as exc:
            errors.append(f"Wikimedia: {exc}")
        else:
            if hits:
                return hits
            errors.append("Wikimedia: no matches")

        # Last resort is still art without anime — never NekosAPI.
        hits = wallhaven_attempts("101")
        if hits:
            return hits

    raise ImageToolError(
        f"No image found for '{query}'. " + "; ".join(errors[-4:])
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
    referer: str = "",
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

    response = http_get(
        url,
        timeout=timeout,
        headers={"Referer": referer} if referer else None,
    )
    status = int(getattr(response, "status_code", 0))
    if status != 200:
        raise ImageToolError(f"HTTP {status} downloading {urlparse(url).netloc}")
    content_type = str(response.headers.get("content-type") or "").lower()
    if (
        content_type
        and not content_type.startswith("image/")
        and content_type not in {"application/octet-stream", "binary/octet-stream"}
    ):
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
                referer=candidate.page,
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
        if item.get("license") == "unknown":
            line += "\n   license: unknown — check the source page before publishing"
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
                "'office interior', 'eiffel tower') or a direct image URL. "
                "Searches general web photos first (DuckDuckGo/Bing), then "
                "licensed Wikimedia photos; uses Wallhaven/NekosAPI only for "
                "art, anime, or wallpaper requests. Validates each file with "
                "Pillow and saves it into Pictures\\WinBrow Images or the folder "
                "the user asked for. No browser is opened. Use this for "
                "standalone saved pictures instead of browser_navigate, "
                "web_search, or download_file. For images embedded in a "
                "document, prefer the document skill's licensed image step."
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
                        "auto": "general web photos, then licensed Wikimedia photos; art/anime uses Wallhaven",
                        "web": "general web photos (DuckDuckGo/Bing), license unknown",
                        "wallhaven": "art/wallpapers only (SFW; no anime unless asked)",
                        "wikimedia": "licensed real photos with credit lines",
                        "nekosapi": "random safe anime illustrations (anime only)",
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
                "get an office interior photo for my presentation",
                "save some anime scenery images to downloads",
                "download this image https://example.com/pic.jpg",
                "get images of the eiffel tower for my document",
            ],
        ),
    ]
