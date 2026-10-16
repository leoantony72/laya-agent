"""
Session Memory — Working Memory for Multi-Turn Commands
========================================================
Per-conversation state that makes follow-ups work:
    "open leoantony.png"  →  "make it fullscreen"  →  "close it again"
    "and mute the volume after"

Tracks conversation turns and a registry of *entities* the user mentioned
(files, folders, apps, urls, queries). Rule-based resolution of demonstrative
references ("that file", "it", "the same app") runs BEFORE routing; anything
the rules can't resolve is handed to the brain as context.

Persistence: JSON at tools/session_state.json — survives restarts.
All writes are best-effort and never break command execution.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("winbrow.memory.session")

SESSION_PATH = Path(__file__).parent.parent.parent / "tools" / "session_state.json"
MAX_TURNS = 24

URL_RE = re.compile(r"(https?://\S+|[\w\-]+\.(?:com|org|net|io|dev|ai|gov|edu|co|me|app)(?:/\S*)?)", re.IGNORECASE)
PATH_RE = re.compile(r"[A-Za-z]:\\[\w\-\\. ]+|/[\w\-./ ]{3,}")
FILENAME_RE = re.compile(r"\b[\w\-]+\.(?:png|jpe?g|gif|bmp|webp|pdf|docx?|xlsx?|pptx?|txt|csv|mp3|mp4|mkv|avi|zip|psd|aep|jsx)\b", re.IGNORECASE)

# Reference phrases → entity kind they want
_REF_PATTERNS: list[tuple[str, str]] = [
    (r"\b(?:that|this|the same|same)\s+files?\b", "file"),
    (r"\b(?:that|this|the same|same)\s+(?:folders?|directories?|directory|dir)\b", "folder"),
    (r"\b(?:that|this|the same|same)\s+(?:apps?|applications?|programs?)\b", "app"),
    (r"\b(?:that|this|the same|same)\s+(?:website|site|page|url|tab)\b", "url"),
    (r"\b(?:that|this|the same|same)\s+(?:search|query|topic)\b", "query"),
]
# Bare pronoun (whole utterance or after command verb)
_PRONOUN_RE = re.compile(r"\b(?:it|that|this|them)\b", re.IGNORECASE)


@dataclass
class Entity:
    kind: str            # "file" | "folder" | "app" | "url" | "query"
    value: str           # concrete value (path / exe / url / text)
    label: str = ""      # human label (filename, app name…)
    turn: int = 0
    ts: float = 0.0

    def to_dict(self) -> dict:
        return {"kind": self.kind, "value": self.value, "label": self.label,
                "turn": self.turn, "ts": self.ts}


@dataclass
class Turn:
    ts: float
    utterance: str
    route_kind: str = ""
    tool_name: str = ""
    success: bool = False
    output_snippet: str = ""

    def to_dict(self) -> dict:
        return {"ts": self.ts, "utterance": self.utterance, "route_kind": self.route_kind,
                "tool_name": self.tool_name, "success": self.success,
                "output_snippet": self.output_snippet[:200]}


class SessionMemory:
    """Working memory for one conversation session."""

    def __init__(self, path: Optional[Path] = None, session_id: str = "default"):
        self.path = path or SESSION_PATH
        self.session_id = session_id
        self.turns: List[Turn] = []
        # Per-kind entity stacks, most-recent-first
        self.entities: Dict[str, List[Entity]] = {"file": [], "folder": [], "app": [], "url": [], "query": []}
        self.load()

    # ------------------------------------------------------------------ persistence
    def load(self) -> None:
        try:
            if not self.path.exists():
                return
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            sessions = data.get("sessions", {})
            raw = sessions.get(self.session_id)
            if not raw:
                return
            self.turns = [Turn(**t) for t in raw.get("turns", [])][-MAX_TURNS:]
            ents = raw.get("entities", {})
            self.entities = {k: [Entity(**e) for e in v] for k, v in ents.items()}
        except Exception as e:
            log.debug(f"Session load failed (non-fatal): {e}")

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            if self.path.exists():
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = {}
            sessions = data.setdefault("sessions", {})
            sessions[self.session_id] = {
                "turns": [t.to_dict() for t in self.turns],
                "entities": {k: [e.to_dict() for e in v][:10] for k, v in self.entities.items()},
            }
            # keep only the 5 most recent sessions on disk
            if len(sessions) > 5:
                for key in list(sessions)[:-5]:
                    del sessions[key]
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.debug(f"Session save failed (non-fatal): {e}")

    def reset(self) -> None:
        self.turns = []
        self.entities = {k: [] for k in self.entities}
        self.save()

    # ------------------------------------------------------------------ entity writes
    def add_entity(self, kind: str, value: str, label: str = "") -> None:
        if not value or kind not in self.entities:
            return
        # de-dup: move to front
        self.entities[kind] = [e for e in self.entities[kind] if e.value != value]
        self.entities[kind].insert(0, Entity(
            kind=kind, value=value, label=label or value,
            turn=len(self.turns), ts=time.time(),
        ))
        self.entities[kind] = self.entities[kind][:10]

    def last_entity(self, kind: Optional[str] = None) -> Optional[Entity]:
        kinds = [kind] if kind else ["file", "folder", "app", "url", "query"]
        for k in kinds:
            stack = self.entities.get(k) or []
            if stack:
                return stack[0]
        return None

    # ------------------------------------------------------------------ extraction
    def extract_entities(self, utterance: str, route: dict, execution: dict) -> None:
        """Pull entities out of a completed turn (route + execution)."""
        tool = route.get("tool_name") or ""
        args = route.get("args") or {}
        success = bool(execution.get("success", False))
        output = str(execution.get("output", ""))

        if tool == "open_file" and success:
            self.add_entity("file", str(args.get("target", "")), Path(str(args.get("target", ""))).name)
        elif tool == "open_folder" and success:
            self.add_entity("folder", str(args.get("folder", "")))
        elif tool in ("app_focus_or_launch", "open_common_app") and success:
            self.add_entity("app", str(args.get("app_name") or args.get("app") or ""))
        elif tool in ("web_search", "laya_ultrafast_browser"):
            q = str(args.get("query") or args.get("goal") or "")
            m = URL_RE.search(q)
            self.add_entity("url", m.group(0)) if m else None
            if q:
                self.add_entity("query", q)
        # generic: any URL or explicit path mentioned in the utterance
        for m in URL_RE.finditer(utterance):
            self.add_entity("url", m.group(0))
        for m in FILENAME_RE.finditer(utterance):
            self.add_entity("file", m.group(0), m.group(0))

    # ------------------------------------------------------------------ turn recording
    def record_turn(self, utterance: str, route: dict, execution: dict) -> None:
        self.turns.append(Turn(
            ts=time.time(),
            utterance=utterance,
            route_kind=str(route.get("kind", "")),
            tool_name=str(route.get("tool_name") or ""),
            success=bool(execution.get("success", False)),
            output_snippet=str(execution.get("output", "")),
        ))
        self.turns = self.turns[-MAX_TURNS:]
        self.extract_entities(utterance, route, execution)
        self.save()

    # ------------------------------------------------------------------ reference resolution
    def resolve_references(self, text: str) -> Tuple[str, Dict[str, str]]:
        """Rewrite demonstrative references with concrete entity values.

        Returns (rewritten_text, {"that file": "C:\\...\\x.png", ...}).
        Only rewrites when a real entity exists — otherwise leaves text intact
        and lets the brain resolve with full context.
        """
        resolutions: Dict[str, str] = {}
        out = text

        for pattern, kind in _REF_PATTERNS:
            def _sub(match: re.Match) -> str:
                ent = self.last_entity(kind)
                if ent:
                    phrase = match.group(0)
                    resolutions[phrase] = ent.value
                    return ent.value
                return match.group(0)
            out = re.sub(pattern, _sub, out, flags=re.IGNORECASE)

        # Bare pronoun utterances: "close it", "open that", "minimize it"
        # Only substitute when the utterance is essentially just a command + pronoun.
        if _PRONOUN_RE.search(out) and not URL_RE.search(out) and not FILENAME_RE.search(out):
            words = out.split()
            if len(words) <= 4:
                ent = self.last_entity()
                if ent:
                    # lambda replacement: literal text (entity values contain
                    # backslashes that re.sub would treat as escapes)
                    out = re.sub(r"\b(?:it|that|this|them)\b", lambda m: ent.value, out, flags=re.IGNORECASE)
                    resolutions["pronoun"] = ent.value

        return out, resolutions

    # ------------------------------------------------------------------ prompt context
    def context_block(self, max_turns: int = 6) -> str:
        """Compact conversation context for the brain prompt."""
        lines = []
        if self.turns:
            lines.append("Recent conversation:")
            for t in self.turns[-max_turns:]:
                status = "ok" if t.success else "failed"
                lines.append(f"  user: {t.utterance}  [{t.tool_name or t.route_kind} → {status}]")
        ents = []
        for kind, stack in self.entities.items():
            for e in stack[:3]:
                ents.append(f"{kind}={e.value}")
        if ents:
            lines.append("Mentioned entities (most recent first): " + "; ".join(ents[:10]))
        return "\n".join(lines) if lines else "No prior conversation."

    def summary(self) -> str:
        n = len(self.turns)
        if not n:
            return "empty session"
        last = self.turns[-1]
        ent = self.last_entity()
        return (f"{n} turns; last: '{last.utterance}' → {last.tool_name or last.route_kind}"
                + (f"; last entity: {ent.kind}={ent.value}" if ent else ""))


def get_session(session_id: str = "default", path: Optional[Path] = None) -> SessionMemory:
    """Get (or create) a session by id."""
    return SessionMemory(path=path, session_id=session_id)
