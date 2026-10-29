"""License-aware web image search and download (Wikimedia Commons).

No API key. Only images with a reusable license (public domain / CC) are
returned, and every result carries the credit line the document must show.
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, asdict
from io import BytesIO
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

from PIL import Image, ImageOps

try:
    from curl_cffi import requests as _http
    _IMPERSONATE = "chrome124"
except Exception:  # pragma: no cover
    import requests as _http
    _IMPERSONATE = None

USER_AGENT = "WinBrow/1.0 (desktop agent; image search; contact: local)"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
ALLOWED_LICENSE_MARKERS = (
    "public domain", "cc0", "cc by", "cc-by", "cc by-sa", "cc-by-sa",
    "attribution", "pd-", "pdm",
)
BLOCKED_LICENSE_MARKERS = ("non-free", "fair use", "fair-use", "copyright")
_TAG_RE = re.compile(r"<[^>]+>")


class ImageSearchError(RuntimeError):
    pass


@dataclass
class ImageResult:
    title: str
    url: str
    page: str
    width: int
    height: int
    license: str
    artist: str
    credit: str
    source: str = "Wikimedia Commons"

    def to_dict(self) -> dict:
        return asdict(self)


def _clean(value: str) -> str:
    text = _TAG_RE.sub(" ", str(value or ""))
    text = html.unescape(text)
    return " ".join(text.split())


def _license_ok(name: str) -> bool:
    low = (name or "").lower()
    if any(marker in low for marker in BLOCKED_LICENSE_MARKERS):
        return False
    return any(marker in low for marker in ALLOWED_LICENSE_MARKERS)


def _get(url: str, *, params: dict | None = None, timeout: int = 30):
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json,image/*,*/*"}
    if _IMPERSONATE:
        response = _http.get(
            url, params=params, headers=headers, impersonate=_IMPERSONATE,
            timeout=timeout,
        )
    else:
        response = _http.get(url, params=params, headers=headers, timeout=timeout)
    return response


def _commons_search(query: str, limit: int, min_width: int) -> list[ImageResult]:
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": f"{query} filetype:bitmap",
        "gsrnamespace": 6,
        "gsrlimit": max(limit * 3, 12),
        "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata",
        "iiurlwidth": 1600,
        "format": "json",
    }
    response = _get(COMMONS_API, params=params)
    if response.status_code == 429:
        # Commons rate-limits bursts. One short retry, then report the limit so
        # callers do not mistake it for "no matching images".
        delay = 1.0
        try:
            delay = min(3.0, max(0.5, float(
                (getattr(response, "headers", {}) or {}).get("Retry-After", 1.0)
            )))
        except (TypeError, ValueError):
            pass
        time.sleep(delay)
        response = _get(COMMONS_API, params=params)
    if response.status_code != 200:
        raise ImageSearchError(f"Wikimedia Commons HTTP {response.status_code}")
    try:
        pages = response.json().get("query", {}).get("pages", {})
    except Exception:
        return []
    results: list[ImageResult] = []
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        mime = str(info.get("mime") or "")
        if not mime.startswith("image/") or mime == "image/svg+xml":
            continue
        width = int(info.get("width") or 0)
        height = int(info.get("height") or 0)
        if width < min_width or height < 300:
            continue
        meta = info.get("extmetadata") or {}
        license_name = _clean((meta.get("LicenseShortName") or {}).get("value", ""))
        if not _license_ok(license_name):
            continue
        artist = _clean((meta.get("Artist") or {}).get("value", ""))
        title = _clean(page.get("title") or "").removeprefix("File:")
        url = str(info.get("thumburl") or info.get("url") or "")
        page_url = str(info.get("descriptionurl") or "")
        if not url:
            continue
        credit = f"{title}"
        if artist:
            credit += f" — {artist}"
        credit += f" ({license_name or 'reusable license'}), Wikimedia Commons"
        results.append(ImageResult(
            title=title, url=url, page=page_url, width=width, height=height,
            license=license_name, artist=artist, credit=credit,
        ))
        if len(results) >= limit:
            break
    return results


def _wikipedia_search(query: str, limit: int, min_width: int) -> list[ImageResult]:
    """Fallback: lead images from English Wikipedia search results."""
    params = {
        "action": "query",
        "generator": "search",
        "gsrsearch": query,
        "gsrlimit": max(limit * 2, 6),
        "prop": "pageimages|info",
        "piprop": "original|thumbnail",
        "pithumbsize": 1600,
        "inprop": "url",
        "format": "json",
    }
    response = _get(WIKIPEDIA_API, params=params)
    if response.status_code != 200:
        return []
    try:
        pages = response.json().get("query", {}).get("pages", {})
    except Exception:
        return []
    results: list[ImageResult] = []
    for page in pages.values():
        thumb = page.get("thumbnail") or {}
        original = page.get("original") or {}
        width = int(original.get("width") or thumb.get("width") or 0)
        height = int(original.get("height") or thumb.get("height") or 0)
        url = thumb.get("source") or original.get("source") or ""
        if not url or width < min_width:
            continue
        title = _clean(page.get("title") or "")
        credit = f"{title} — Wikimedia Commons (check file page for license)"
        results.append(ImageResult(
            title=title, url=url, page=str(page.get("fullurl") or ""),
            width=width, height=height, license="check file page",
            artist="", credit=credit, source="Wikipedia",
        ))
        if len(results) >= limit:
            break
    return results


def search_images(
    query: str,
    *,
    limit: int = 5,
    min_width: int = 900,
) -> list[ImageResult]:
    query = str(query or "").strip()
    if not query:
        return []
    seen: set[str] = set()
    results: list[ImageResult] = []
    # Commons only: every result carries an explicit reusable license. The
    # Wikipedia lead-image fallback does not expose license metadata, so it is
    # intentionally not used for document insertion.
    try:
        for item in _commons_search(query, limit, min_width):
            if item.url in seen:
                continue
            seen.add(item.url)
            results.append(item)
    except ImageSearchError:
        raise
    except Exception:
        return []
    return results


def download_image(
    url: str,
    destination: str | Path,
    *,
    min_width: int = 600,
    max_dimension: int = 2400,
    timeout: int = 45,
) -> dict:
    path = Path(destination).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    last_error = ""
    for attempt in range(2):
        try:
            response = _get(url, timeout=timeout)
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}"
                time.sleep(0.6)
                continue
            content_type = str(response.headers.get("content-type") or "")
            if content_type and not content_type.startswith("image/"):
                last_error = f"not an image ({content_type})"
                break
            raw = response.content
            if len(raw) > 25 * 1024 * 1024:
                last_error = "image is larger than 25 MB"
                break
            with Image.open(BytesIO(raw)) as image:
                image = ImageOps.exif_transpose(image)
                if image.width < min_width:
                    last_error = f"image is only {image.width}px wide"
                    break
                if max(image.size) > max_dimension:
                    image.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
                suffix = path.suffix.lower()
                if suffix in {".jpg", ".jpeg"} and image.mode in {"RGBA", "P"}:
                    background = Image.new("RGB", image.size, (255, 255, 255))
                    background.paste(image, mask=image.split()[-1] if image.mode == "RGBA" else None)
                    image = background
                elif suffix not in {".jpg", ".jpeg"}:
                    path = path.with_suffix(".png")
                    image = image.convert("RGBA" if image.mode in {"RGBA", "P"} else "RGB")
                image.save(path)
                return {"path": str(path), "width": image.width, "height": image.height}
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.6)
    raise ImageSearchError(f"Could not download {url}: {last_error}")


def find_image(
    query: str,
    destination_dir: str | Path,
    *,
    min_width: int = 900,
    limit: int = 6,
) -> dict:
    destination = Path(destination_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    candidates = search_images(query, limit=limit, min_width=min_width)
    errors = []
    for index, candidate in enumerate(candidates, start=1):
        safe = re.sub(r"[^a-z0-9]+", "-", candidate.title.lower()).strip("-")[:50]
        suffix = Path(candidate.url.split("?")[0]).suffix or ".jpg"
        if suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            suffix = ".png"
        target = destination / f"{safe or 'image'}-{index}{suffix}"
        try:
            info = download_image(candidate.url, target, min_width=min_width)
        except Exception as exc:
            errors.append(str(exc))
            continue
        data = candidate.to_dict()
        data.update(info)
        return data
    raise ImageSearchError(
        f"No reusable image found for '{query}'."
        + (f" {errors[-1]}" if errors else " Search returned no licensed images.")
    )


def search_and_download(
    query: str,
    destination_dir: str | Path,
    *,
    count: int = 3,
    min_width: int = 900,
) -> list[dict]:
    destination = Path(destination_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    found: list[dict] = []
    for index, candidate in enumerate(
        search_images(query, limit=count, min_width=min_width), start=1
    ):
        safe = re.sub(r"[^a-z0-9]+", "-", candidate.title.lower()).strip("-")[:50]
        suffix = Path(candidate.url.split("?")[0]).suffix or ".jpg"
        target = destination / f"{safe or 'image'}-{index}{suffix}"
        try:
            info = download_image(candidate.url, target, min_width=min_width)
        except Exception:
            continue
        data = candidate.to_dict()
        data.update(info)
        found.append(data)
    return found
