"""
Document Reading Tool
=====================
Converts local files to Markdown with Microsoft MarkItDown (MIT), so the
planner can read PDF, PPTX, DOCX, XLSX/XLS, CSV, Markdown, HTML, JSON, XML,
EPUB and plain-text files without opening an app or using OCR.

The converter is imported lazily so a missing optional dependency only breaks
document reads, not agent startup. Text-like formats fall back to a plain
UTF-8 read when MarkItDown is unavailable.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

from .registry import ArgSpec, Tool
from .resolvers import resolve_file

log = logging.getLogger("winbrow.documents")

DOCUMENT_IMPL = "document"
DEFAULT_MAX_CHARS = 60000
MAX_OUTPUT_CHARS = 100000
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_CACHE_CHARS = 2_000_000
_CACHE_SIZE = 4

_TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".xml", ".html",
    ".htm", ".log", ".yaml", ".yml", ".ini", ".cfg", ".py", ".js", ".ts",
    ".css", ".sql", ".rst",
}

_markitdown_instance = None
_markitdown_lock = threading.Lock()
_convert_lock = threading.Lock()
_cache: "OrderedDict[tuple, str]" = OrderedDict()
_cache_lock = threading.Lock()


def _get_markitdown():
    global _markitdown_instance
    if _markitdown_instance is None:
        with _markitdown_lock:
            if _markitdown_instance is None:
                from markitdown import MarkItDown
                _markitdown_instance = MarkItDown(enable_plugins=False)
    return _markitdown_instance


def _fallback_read(path: Path) -> str:
    if path.suffix.lower() not in _TEXT_EXTS:
        raise RuntimeError(
            "MarkItDown is not installed. Install document support with: "
            'pip install "markitdown[pdf,docx,pptx,xlsx,xls]"'
        )
    return path.read_text(encoding="utf-8", errors="replace")


def _convert(path: Path) -> str:
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return cached

    try:
        converter = _get_markitdown()
    except ImportError:
        text = _fallback_read(path)
    else:
        try:
            with _convert_lock:
                result = converter.convert(str(path))
            text = str(getattr(result, "text_content", "") or "")
        except Exception as exc:
            raise RuntimeError(
                f"Could not read {path.name}: {exc}. "
                "If this format needs an extra, install markitdown extras."
            ) from exc

    if len(text) <= MAX_CACHE_CHARS:
        with _cache_lock:
            _cache[key] = text
            _cache.move_to_end(key)
            while len(_cache) > _CACHE_SIZE:
                _cache.popitem(last=False)
    return text


def _resolve_document_path(raw: str) -> Optional[Path]:
    ref = str(raw or "").strip().strip('"').strip("'")
    if not ref:
        return None
    expanded = os.path.expandvars(os.path.expanduser(ref))
    direct = Path(expanded)
    if direct.is_file():
        return direct.resolve()
    found = resolve_file(ref)
    if found and os.path.isfile(found):
        return Path(found).resolve()
    return None


def _parse_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


def read_document(
    path: str,
    max_chars: Any = DEFAULT_MAX_CHARS,
    offset: Any = 0,
) -> dict[str, Any]:
    """Read a document and return a bounded Markdown chunk."""
    target = _resolve_document_path(path)
    if target is None:
        return {
            "success": False,
            "output": (
                f"File not found: {path}. Use find_files to locate the exact "
                "path, then call read_document with it."
            ),
        }
    if not target.is_file():
        return {
            "success": False,
            "output": f"Not a file: {target}. read_document reads files, not folders.",
        }
    try:
        size = target.stat().st_size
    except OSError as exc:
        return {"success": False, "output": f"Could not stat {target}: {exc}"}
    if size > MAX_FILE_BYTES:
        return {
            "success": False,
            "output": (
                f"File is too large to read safely ({size} bytes; limit is "
                f"{MAX_FILE_BYTES}). Extract or split it first."
            ),
        }

    try:
        text = _convert(target)
    except Exception as exc:
        return {"success": False, "output": str(exc)}

    total = len(text)
    limit = _parse_int(max_chars, DEFAULT_MAX_CHARS, 1000, MAX_OUTPUT_CHARS)
    start = _parse_int(offset, 0, 0, max(total, 0))
    chunk = text[start:start + limit]
    fmt = target.suffix.lower().lstrip(".") or "unknown"

    header = (
        f"FILE: {target.name}\n"
        f"PATH: {target}\n"
        f"FORMAT: {fmt}\n"
        f"CHARS: {total}\n"
        f"RANGE: {start}-{start + len(chunk)}"
    )
    if not text.strip():
        return {
            "success": True,
            "output": (
                header + "\n\nNo extractable text found. The file may be "
                "image-only, scanned, or empty."
            ),
            "path": str(target),
            "format": fmt,
            "chars": 0,
            "offset": 0,
            "next_offset": None,
        }

    next_offset = start + len(chunk)
    if next_offset < total:
        chunk += (
            f"\n\n[TRUNCATED: {total - next_offset} chars remain. Call "
            f"read_document again with offset={next_offset}.]"
        )
    else:
        next_offset = None

    return {
        "success": True,
        "output": header + "\n---\n" + chunk,
        "path": str(target),
        "format": fmt,
        "chars": total,
        "offset": start,
        "next_offset": next_offset,
    }


def execute_document_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a document atom by tool name. Never raises."""
    args = args or {}
    if name != "read_document":
        return {"success": False, "output": f"Unknown document tool '{name}'"}
    try:
        return read_document(
            str(args.get("path") or args.get("target") or ""),
            max_chars=args.get("max_chars", DEFAULT_MAX_CHARS),
            offset=args.get("offset", 0),
        )
    except Exception as exc:
        log.warning(f"document tool '{name}' failed: {exc}")
        return {"success": False, "output": f"{name} failed: {exc}"}


def get_document_tools() -> list[Tool]:
    """Tool list registered into the ToolRegistry at init."""
    return [
        Tool(
            name="read_document",
            description=(
                "Read a local document and return its text as Markdown. "
                "Supports PDF, PPTX, DOCX, XLSX/XLS, CSV, Markdown, HTML, "
                "JSON, XML, EPUB and plain text. Use when the user asks what "
                "a file says or to summarize, search or extract its contents. "
                "Do not use for images or web pages."
            ),
            script="",
            impl=DOCUMENT_IMPL,
            args=[
                ArgSpec(
                    name="path", kind="text",
                    instructions="File name or absolute path, e.g. C:\\Docs\\report.pdf",
                ),
                ArgSpec(
                    name="max_chars", kind="text",
                    instructions="Maximum characters to return (1000-100000)",
                    default=str(DEFAULT_MAX_CHARS),
                ),
                ArgSpec(
                    name="offset", kind="text",
                    instructions="Character offset for continuing a long document",
                    default="0",
                ),
            ],
            speak="done",
            examples=[
                "read the pdf report", "what does this pptx say",
                "summarize the csv", "read the markdown file",
            ],
        ),
    ]
