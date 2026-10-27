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
    "16:9": (13.333, 7.5),      # 1920x1080 slide
    "16:10": (13.333, 8.333),
    "4:3": (10.0, 7.5),
    "letter": (8.5, 11.0),
    "a4": (8.27, 11.69),
    "auto": None,               # let the document's own CSS decide
}
DEFAULT_PAGE = "16:9"


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


def _page_css(page: str) -> str:
    size = PAGE_SIZES.get((page or "").strip().lower(), PAGE_SIZES[DEFAULT_PAGE])
    if size is None:
        return ""
    width, height = size
    return (
        f"@page {{ size: {width}in {height}in; margin: 0; }}\n"
        f"@media print {{ html, body {{ width: {width}in; }} }}\n"
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
    """Render ``html_path`` to ``pdf_path``. Raises RuntimeError on failure."""
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

    css = _page_css(page)
    temporary = source
    profile_dir = Path(tempfile.gettempdir()) / f"winbrow-print-{uuid.uuid4().hex}"
    try:
        if css:
            original = source.read_text(encoding="utf-8", errors="replace")
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
            "--virtual-time-budget=6000",
            f"--user-data-dir={profile_dir}",
            f"--print-to-pdf={target}",
            source.as_uri() if temporary == source else temporary.as_uri(),
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
            "page": page,
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
