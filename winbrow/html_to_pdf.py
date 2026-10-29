"""
Render a local HTML file to PDF with headless Chrome or Edge.

Used by the html-ppt deck exporter and the PDF skill's ``from_html`` operation.
No extra dependency: it drives the browser already installed on Windows.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional, Tuple

PAGE_SIZES: dict[str, Optional[Tuple[float, float]]] = {
    # Slide sizes match PowerPoint defaults (inches).
    "16:9": (13.333, 7.5),      # 1920x1080 design canvas
    "16:10": (13.333, 8.333),
    "4:3": (10.0, 7.5),
    # Document sizes.
    "letter": (8.5, 11.0),
    "legal": (8.5, 14.0),
    "a4": (8.27, 11.69),
    "a5": (5.83, 8.27),
    "auto": None,               # infer from the HTML
}
SLIDE_PAGES = frozenset({"16:9", "16:10", "4:3"})
DOCUMENT_PAGES = frozenset({"letter", "legal", "a4", "a5"})
DEFAULT_SLIDE_PAGE = "16:9"
DEFAULT_DOCUMENT_PAGE = "a4"
DEFAULT_PAGE = "auto"

# A deck has slide sections (or the .deck canvas). A resume/official document
# should use a paper page, never a 16:9 slide.
_DECK_HTML_RE = re.compile(
    r'<(?:section|div)\b[^>]*class\s*=\s*["\'][^"\']*\b(?:slide|deck)\b',
    re.IGNORECASE,
)
_RESUME_HTML_RE = re.compile(
    r"\b(?:resume|curriculum\s+vitae|cover[\s_-]*letter|official|memo)\b",
    re.IGNORECASE,
)


def detect_document_kind(html: str) -> str:
    """Return ``presentation``, ``resume`` or ``document`` for page policy."""
    text = str(html or "")
    if _DECK_HTML_RE.search(text):
        return "presentation"
    head = text[:6000]
    if _RESUME_HTML_RE.search(head):
        return "resume"
    return "document"


def resolve_page(page: str | None, html: str = "") -> str:
    """Resolve ``auto``/None to a slide size for decks, paper otherwise."""
    raw = (page or DEFAULT_PAGE).strip().lower()
    if raw in PAGE_SIZES and raw != "auto":
        return raw
    kind = detect_document_kind(html)
    return DEFAULT_SLIDE_PAGE if kind == "presentation" else DEFAULT_DOCUMENT_PAGE


def browser_candidates() -> list[Path]:
    candidates: list[Path] = []
    env = os.environ.get("WINBROW_BROWSER", "").strip()
    if env:
        candidates.append(Path(env))
    for name in (
        "chrome", "chrome.exe", "msedge", "msedge.exe",
        "chromium", "chromium.exe",
    ):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for base in (
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("LOCALAPPDATA"),
    ):
        if not base:
            continue
        root = Path(base)
        candidates.extend([
            root / "Google/Chrome/Application/chrome.exe",
            root / "Microsoft/Edge/Application/msedge.exe",
            root / "Chromium/Application/chrome.exe",
        ])
    unique: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path).lower()
        if key in seen or not path.is_file():
            continue
        seen.add(key)
        unique.append(path)
    return unique


def find_browser() -> Optional[Path]:
    found = browser_candidates()
    return found[0] if found else None


def _page_css(page: str, *, deck: bool = False) -> str:
    size = PAGE_SIZES.get((page or "").strip().lower(), PAGE_SIZES[DEFAULT_PAGE])
    if size is None:
        return ""
    width, height = size
    # Print backgrounds/colors exactly as authored. Without this Chrome drops
    # dark slide backgrounds, leaving light text on white paper.
    base = (
        f"@page {{ size: {width}in {height}in; margin: 0; }}\n"
        "@media print {\n"
        "  * { -webkit-print-color-adjust: exact !important; "
        "print-color-adjust: exact !important; }\n"
        "  html, body { margin: 0 !important; padding: 0 !important; }\n"
    )
    if deck:
        # One .slide == one PDF page, regardless of whether base.css loaded.
        # Every declaration is !important because template rules like
        # `.tpl-name .slide` have higher specificity.
        return base + (
            f"  html, body {{ width: {width}in !important; height: {height}in "
            "!important; overflow: visible !important; }\n"
            f"  .deck {{ width: {width}in !important; height: auto !important; "
            "transform: none !important; display: block !important; "
            "min-height: 0 !important; max-height: none !important; "
            "overflow: visible !important; }}\n"
            f"  .slide {{ position: relative !important; inset: auto !important; "
            f"width: {width}in !important; height: {height}in !important; "
            f"min-height: {height}in !important; max-height: {height}in "
            "!important; opacity: 1 !important; visibility: visible !important; "
            "transform: none !important; display: flex !important; "
            "page-break-after: always; break-after: page; "
            "page-break-inside: avoid; break-inside: avoid; "
            "overflow: hidden !important; }\n"
            "  .slide:last-child { page-break-after: auto; break-after: auto; }\n"
            "  .notes, .notes-overlay, .overview, .deck-header, .deck-footer, "
            ".progress-bar { display: none !important; }\n"
            "}\n"
        )
    return base + (
        "  html, body { width: auto !important; height: auto !important; "
        "overflow: visible !important; }\n"
        "}\n"
    )


def _inject_css(html: str, css: str) -> str:
    if not css:
        return html
    tag = f"<style data-winbrow-print>{css}</style>"
    if re.search(r"</head\s*>", html, re.I):
        return re.sub(r"</head\s*>", tag + "</head>", html, count=1, flags=re.I)
    return tag + html


def html_to_pdf(
    html_path: str | Path,
    pdf_path: str | Path,
    *,
    page: str = DEFAULT_PAGE,
    timeout: int = 120,
) -> dict:
    """Render ``html_path`` to ``pdf_path``. Raises RuntimeError on failure.

    ``page="auto"`` uses 16:9 for slide decks and A4 for documents/resumes.
    """
    source = Path(html_path).resolve()
    target = Path(pdf_path).resolve()
    if not source.is_file():
        raise RuntimeError(f"HTML file not found: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)

    browser = find_browser()
    if browser is None:
        raise RuntimeError(
            "Chrome or Edge was not found. Set WINBROW_BROWSER to the browser "
            "executable path."
        )

    original = source.read_text(encoding="utf-8", errors="replace")
    kind = detect_document_kind(original)
    resolved_page = resolve_page(page, original)
    deck_mode = kind == "presentation"
    css = _page_css(resolved_page, deck=deck_mode)
    width_in, height_in = PAGE_SIZES.get(resolved_page) or (0.0, 0.0)
    temporary = source
    profile_dir = Path(tempfile.gettempdir()) / f"winbrow-print-{uuid.uuid4().hex}"
    try:
        if css:
            patched = _inject_css(original, css)
            temporary = source.with_name(f".winbrow_print_{uuid.uuid4().hex}.html")
            temporary.write_text(patched, encoding="utf-8")
        command = [
            str(browser),
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            "--allow-file-access-from-files",
            "--no-pdf-header-footer",
            "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=8000",
            f"--user-data-dir={profile_dir}",
            f"--print-to-pdf={target}",
            temporary.as_uri(),
        ]
        log_path = profile_dir / "chrome.log"
        profile_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(log_path, "w", encoding="utf-8", errors="replace") as log:
                subprocess.run(
                    command, stdout=log, stderr=log, timeout=timeout,
                )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"PDF render timed out after {timeout}s.") from exc
        if not target.is_file() or target.stat().st_size == 0:
            detail = log_path.read_text(encoding="utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Browser did not write {target}."
                + (f" {detail[-400:]}" if detail else "")
            )
        return {
            "path": str(target),
            "browser": str(browser),
            "bytes": target.stat().st_size,
            "page": resolved_page,
            "document_kind": kind,
            "width_in": width_in,
            "height_in": height_in,
        }
    finally:
        try:
            if temporary != source and temporary.exists():
                temporary.unlink()
        except OSError:
            pass
        shutil.rmtree(profile_dir, ignore_errors=True)


def html_to_png(
    html_path: str | Path,
    png_path: str | Path,
    *,
    width: int = 1920,
    height: int = 1080,
    slide: int = 1,
    timeout: int = 90,
) -> dict:
    """Screenshot one slide of a deck (``#/N`` deep-link) to PNG."""
    source = Path(html_path).resolve()
    target = Path(png_path).resolve()
    if not source.is_file():
        raise RuntimeError(f"HTML file not found: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    browser = find_browser()
    if browser is None:
        raise RuntimeError(
            "Chrome or Edge was not found. Set WINBROW_BROWSER to the browser "
            "executable path."
        )
    url = source.as_uri() + (f"#/{slide}" if slide > 1 else "")
    profile_dir = Path(tempfile.gettempdir()) / f"winbrow-shot-{uuid.uuid4().hex}"
    command = [
        str(browser),
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--hide-scrollbars",
        "--allow-file-access-from-files",
        "--run-all-compositor-stages-before-draw",
        "--virtual-time-budget=4000",
        f"--window-size={width},{height}",
        f"--user-data-dir={profile_dir}",
        f"--screenshot={target}",
        url,
    ]
    log_path = profile_dir / "chrome.log"
    profile_dir.mkdir(parents=True, exist_ok=True)
    try:
        try:
            with open(log_path, "w", encoding="utf-8", errors="replace") as log:
                subprocess.run(command, stdout=log, stderr=log, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"PNG render timed out after {timeout}s.") from exc
        if not target.is_file() or target.stat().st_size == 0:
            detail = log_path.read_text(encoding="utf-8", errors="replace").strip()
            raise RuntimeError(
                f"Browser did not write {target}."
                + (f" {detail[-400:]}" if detail else "")
            )
        return {"path": str(target), "browser": str(browser)}
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)


def count_slides(html_path: str | Path) -> int:
    html = Path(html_path).read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(
        r'<section[^>]*class="([^"]*\s)?slide(\s[^"]*)?"', re.I,
    )
    return len(pattern.findall(html))
