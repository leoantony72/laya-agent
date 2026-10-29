"""Bounded tool output with a full copy kept on disk.

The planner should see enough of a result to act, but one runaway command
must not blow up the prompt. ``clip_middle`` keeps head and tail, inserts an
explicit omission notice with the path of the full text, and writes that text
only when clipping actually happens.

Saved files may contain user data (screen text, command output, fetched
pages). They live under ``logs/results`` which is gitignored, and are pruned
after ``WINBROW_RESULTS_RETENTION_DAYS`` (default 7).
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

log = logging.getLogger("winbrow.utils.clip")

DEFAULT_RESULTS_DIR = Path(__file__).resolve().parents[2] / "logs" / "results"
DEFAULT_RETENTION_DAYS = 7
_PRUNE_INTERVAL_S = 3600.0
_last_prune: dict[str, float] = {}


def results_dir() -> Path:
    raw = os.environ.get("WINBROW_RESULTS_DIR", "").strip()
    return Path(raw) if raw else DEFAULT_RESULTS_DIR


def scripts_dir() -> Path:
    raw = os.environ.get("WINBROW_SCRIPTS_DIR", "").strip()
    if raw:
        return Path(raw)
    return results_dir().parent / "scripts"


def _safe_label(label: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(label or "result"))
    return cleaned.strip("._")[:80] or "result"


def _prune(directory: Path) -> None:
    """Delete old result files at most once per hour per directory."""
    key = str(directory)
    now = time.time()
    if now - _last_prune.get(key, 0.0) < _PRUNE_INTERVAL_S:
        return
    _last_prune[key] = now
    try:
        days = float(os.environ.get(
            "WINBROW_RESULTS_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS)))
    except (TypeError, ValueError):
        days = DEFAULT_RETENTION_DAYS
    if days <= 0:
        return
    cutoff = now - days * 86400
    try:
        for path in directory.iterdir():
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
    except OSError as exc:
        log.debug(f"result retention skipped: {exc}")


def save_full_text(
    text: str,
    label: str = "result",
    save_dir: Optional[Path] = None,
    filename: Optional[str] = None,
) -> str:
    """Write the full text and return its path, or "" when the write fails."""
    text = str(text or "")
    directory = Path(save_dir) if save_dir else results_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        name = filename or (
            f"{_safe_label(label)}_{time.strftime('%Y%m%d_%H%M%S')}_"
            f"{uuid.uuid4().hex[:8]}.txt"
        )
        path = directory / name
        path.write_text(text, encoding="utf-8")
        _prune(directory)
        return str(path)
    except OSError as exc:
        log.debug(f"full output save failed: {exc}")
        return ""


def clip_head(text: str, limit: int, save_dir: Optional[Path] = None,
              label: str = "result") -> str:
    """Keep the head only (safe for JSON-ish payloads), save the full text."""
    text = str(text or "")
    limit = max(1, int(limit))
    if len(text) <= limit:
        return text
    path = save_full_text(text, label=label, save_dir=save_dir)
    notice = f"\n... [truncated {len(text) - limit} chars"
    notice += f"; full output at {path}]" if path else "]"
    return text[:limit].rstrip() + notice


def clip_middle(
    text: str,
    limit: int,
    save_dir: Optional[Path] = None,
    label: str = "result",
    head_ratio: float = 0.3,
    tail_ratio: float = 0.3,
) -> str:
    """Keep 30% head and 30% tail, save the full text, insert a notice.

    If ``save_dir`` is given the file is written there; otherwise
    ``WINBROW_RESULTS_DIR`` (default ``logs/results``) is used. The full
    text is written byte-for-byte before any clipping.
    """
    text = str(text or "")
    limit = max(1, int(limit))
    if len(text) <= limit:
        return text

    head_ratio = min(max(float(head_ratio), 0.0), 0.8)
    tail_ratio = min(max(float(tail_ratio), 0.0), 0.8)
    if head_ratio + tail_ratio > 0.95:
        tail_ratio = max(0.0, 0.95 - head_ratio)
    head_len = int(limit * head_ratio)
    tail_len = int(limit * tail_ratio)
    if head_len + tail_len >= limit:
        tail_len = max(0, limit - head_len - 1)

    path = save_full_text(text, label=label, save_dir=save_dir)
    omitted = len(text) - head_len - tail_len
    where = f"; full output at {path}" if path else ""
    notice = f"\n\n[... {omitted} chars omitted{where}]\n\n"
    return text[:head_len] + notice + (text[-tail_len:] if tail_len else "")
