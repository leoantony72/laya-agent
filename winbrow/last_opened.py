"""
ContextMemory — What Was Just Opened
====================================
Small persistent memory of the most recently opened files, folders, and
apps. Lets follow-ups like "open that file" resolve against something real
instead of failing. Persisted as JSON so it survives restarts; all writes
are best-effort and never break command execution.
"""

from __future__ import annotations

import json
import logging
import pathlib
import time
from typing import Any, Optional

log = logging.getLogger("winbrow.memory")

MEMORY_PATH = pathlib.Path(__file__).parent.parent / "tools" / "context_memory.json"
MAX_HISTORY = 20


class ContextMemory:
    """Last-opened files/folders/apps plus a bounded event history."""

    def __init__(self, path: Optional[pathlib.Path] = None):
        self.path = path or MEMORY_PATH
        self.last_file: Optional[str] = None
        self.last_folder: Optional[str] = None
        self.last_app: Optional[str] = None
        self.history: list[dict[str, Any]] = []
        self.load()

    # -- persistence -------------------------------------------------
    def load(self) -> None:
        try:
            if not self.path.exists():
                return
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.last_file = data.get("last_file")
            self.last_folder = data.get("last_folder")
            self.last_app = data.get("last_app")
            self.history = data.get("history", [])[-MAX_HISTORY:]
        except Exception as e:
            log.debug(f"ContextMemory load failed (non-fatal): {e}")

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({
                    "last_file": self.last_file,
                    "last_folder": self.last_folder,
                    "last_app": self.last_app,
                    "history": self.history[-MAX_HISTORY:],
                }, f, indent=2)
        except Exception as e:
            log.debug(f"ContextMemory save failed (non-fatal): {e}")

    # -- recording ---------------------------------------------------
    def record_open(self, kind: str, path: str, label: str = "") -> None:
        """Record a successful open. kind ∈ {"file", "folder", "app"}."""
        try:
            if kind == "file":
                self.last_file = path
            elif kind == "folder":
                self.last_folder = path
            elif kind == "app":
                self.last_app = path
            else:
                return
            self.history.append({
                "ts": time.time(), "kind": kind, "path": path, "label": label,
            })
            self.history = self.history[-MAX_HISTORY:]
            self.save()
        except Exception as e:
            log.debug(f"ContextMemory record failed (non-fatal): {e}")

    # -- reference resolution ----------------------------------------
    def resolve_reference(self, text: str) -> Optional[str]:
        """Resolve 'that file' / 'this folder' / 'it' against memory.

        Returns a real path or None. Pure lookup — no sentence parsing
        beyond fixed reference phrases.
        """
        refs = (text or "").lower()
        if "that file" in refs or "this file" in refs:
            return self.last_file
        if "that folder" in refs or "this folder" in refs:
            return self.last_folder
        if refs.strip() in ("it", "that", "this", "open it", "close it"):
            return self.last_file or self.last_folder
        return None

    def summary(self) -> str:
        """One-line state summary for model prompts."""
        parts = []
        if self.last_file:
            parts.append(f"last file: {self.last_file}")
        if self.last_folder:
            parts.append(f"last folder: {self.last_folder}")
        if self.last_app:
            parts.append(f"last app: {self.last_app}")
        return "; ".join(parts) if parts else "nothing opened yet"
