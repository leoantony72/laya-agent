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

Each turn also records the absolute artifact paths it created/opened, and a
follow-up ("where is the pdf?", "open it") is bound to those paths in the
brain prompt. This prevents a disk-wide search when the user is clearly
referring to the previous turn's output.

Persistence: JSON at tools/session_state.json — survives restarts.
All writes are best-effort and never break command execution.
"""

from __future__ import annotations

import json
import logging
import os
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
FILENAME_RE = re.compile(r"\b[\w\-]+\.(?:png|jpe?g|gif|bmp|webp|pdf|docx?|rtf|odt|xlsx?|pptx?|txt|md|csv|html?|json|py|ps1|mp3|mp4|mkv|avi|zip|psd|aep|jsx)\b", re.IGNORECASE)
# A document name can contain spaces ("AI document about AI.rtf"). Catch the
# quoted/named form without swallowing the prose before an ordinary filename.
_QUOTED_NAME_RE = re.compile(
    r"['\"]([^'\"\r\n]{1,200}?\.[A-Za-z0-9]{2,5})['\"]")
_NAMED_NAME_RE = re.compile(
    r"\b(?:saved as|saved to|named|created|wrote|written to)\s+"
    r"['\"]?([^'\"\r\n]{1,200}?\.[A-Za-z0-9]{2,5})['\"]?",
    re.IGNORECASE,
)

# Absolute-path helpers used to capture the artifacts a turn produced. Args are
# reliable whole values; free-text output is scanned token-by-token so prose
# after a path ("...\index.html and verified") is not swallowed.
_DRIVE_PATH_RE = re.compile(r"^(?:[A-Za-z]:\\|\\\\)")
_ABS_TOKEN_RE = re.compile(r"(?:[A-Za-z]:\\|\\\\)[^\s\"'<>|?*]+")
_ABS_PATH_KEYS = (
    "path", "target", "destination", "dest", "output", "output_path",
    "output_dir", "output_parent", "file", "filename", "folder", "source",
)
# Skills that produce or move a user-visible artifact. run_command is
# deliberately excluded: its cwd/output are working state, not deliverables.
_ARTIFACT_SKILL_TOKENS = frozenset({
    "write", "create", "save", "move", "copy", "rename", "download",
    "export", "generate", "convert", "build", "render", "edit", "publish",
})
_ARTIFACT_SKILL_NAMES = frozenset({
    "run_skill_script", "html_to_pdf", "pdf_tool", "pptx_tool", "docx_tool",
    "xlsx_tool",
})

# Artifact nouns in user follow-ups → extensions that would satisfy them.
_ARTIFACT_NOUN_EXT: dict[str, set[str]] = {
    "pdf": {"pdf"},
    "pptx": {"pptx"},
    "ppt": {"pptx"},
    "powerpoint": {"pptx"},
    "spreadsheet": {"xlsx", "xls", "csv"},
    "xlsx": {"xlsx", "xls"},
    "excel": {"xlsx", "xls"},
    "docx": {"docx", "doc"},
    "word": {"docx", "doc"},
    "rtf": {"rtf"},
    "wordpad": {"rtf"},
    "document": {"docx", "doc", "rtf", "odt", "pdf", "txt", "md"},
    "report": {"docx", "doc", "rtf", "pdf", "txt", "md"},
    "presentation": {"html", "htm", "pptx", "pdf"},
    "deck": {"html", "htm", "pptx", "pdf"},
    "slides": {"html", "htm", "pptx", "pdf"},
    "website": {"html", "htm"},
    "site": {"html", "htm"},
    "html": {"html", "htm"},
    "image": {"png", "jpg", "jpeg", "gif", "webp", "bmp"},
    "photo": {"png", "jpg", "jpeg", "gif", "webp", "bmp"},
    "video": {"mp4", "mkv", "avi", "webm", "mov"},
}
_ARTIFACT_NOUN_RE = re.compile(
    r"\b(" + "|".join(sorted(
        (re.escape(n) for n in _ARTIFACT_NOUN_EXT), key=len, reverse=True
    )) + r")\b",
    re.IGNORECASE,
)
_WHERE_QUERY_RE = re.compile(
    r"\b(where|which|what|find|locate|location|path|saved|stored)\b",
    re.IGNORECASE,
)
_NEW_REQUEST_RE = re.compile(
    r"\b(create|make|build|generate|design|write|start|new|another)\b",
    re.IGNORECASE,
)
_PRONOUN_REF_RE = re.compile(
    r"\b(it|that|this|them|those|these)\b", re.IGNORECASE
)
_ACTION_REF_RE = re.compile(
    r"^(?:open|show|reveal|send|email|export|convert|attach|print)\b",
    re.IGNORECASE,
)
# "the file / the document" is a definite reference too, even when the user is
# complaining about its content ("there is no heading in the file").
_THE_ARTIFACT_RE = re.compile(
    r"\bthe\s+(?:file|document|doc|deck|presentation|spreadsheet|pdf|image|"
    r"photo|video|report)\b",
    re.IGNORECASE,
)


def _clean_arg_path(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip().strip("\"'`")
    if _DRIVE_PATH_RE.match(text) and "\n" not in text and "\r" not in text:
        return text.rstrip(" .,;")
    return ""


def _paths_in_text(value: Any) -> List[str]:
    found: List[str] = []
    for match in _ABS_TOKEN_RE.finditer(str(value or "")):
        path = match.group(0).rstrip(".,;:)]}")
        if path:
            found.append(path)
    return found


def _artifact_noun(text: str) -> str:
    match = _ARTIFACT_NOUN_RE.search(text or "")
    return match.group(1).lower() if match else ""


def _is_temp_path(path: str) -> bool:
    return bool(re.search(r"(?:\\temp\\|\\tmp\\|/tmp/|/temp/)", path, re.IGNORECASE))


_GENERIC_USER_DIR_RE = re.compile(
    r"^[A-Za-z]:\\Users(?:\\[^\\]+)?"
    r"(?:\\(?:Desktop|Documents|Downloads|Pictures|Videos))?\\?$",
    re.IGNORECASE,
)


def _is_generic_user_dir(path: str) -> bool:
    """True for the profile root and the standard user folders."""
    return bool(_GENERIC_USER_DIR_RE.match(str(path or "").strip()))


# Files the user can see but the agent only knows by their bare name (e.g.
# WordPad's save dialog: "saved as 'AI document about Artificial Intelligence.
# rtf'"). Resolving the name against the standard user folders turns it into an
# absolute artifact, so the next turn talks about THAT file instead of an
# unrelated file from an older task.
_STANDARD_USER_DIRS = (
    "Desktop", "Documents", "Downloads",
    r"OneDrive\Desktop", r"OneDrive\Documents", r"OneDrive\Downloads",
)


def _artifact_context_max_age_s() -> float:
    """How long an artifact stays authoritative for follow-up prompts."""
    try:
        minutes = float(os.environ.get("WINBROW_ARTIFACT_CONTEXT_MIN", "120"))
    except (TypeError, ValueError):
        minutes = 120.0
    return max(0.0, minutes * 60.0)


def _standard_user_dirs() -> List[Path]:
    """Folders searched for bare filenames the agent only knows by name."""
    raw = os.environ.get("WINBROW_ARTIFACT_DIRS", "").strip()
    if raw:
        return [Path(part) for part in raw.split(os.pathsep) if part.strip()]
    home = Path.home()
    return [home / relative for relative in _STANDARD_USER_DIRS]


def _session_context_max_age_s() -> float:
    """How long old turns stay in the prompt as "recent conversation"."""
    try:
        minutes = float(os.environ.get("WINBROW_SESSION_CONTEXT_MIN", "720"))
    except (TypeError, ValueError):
        minutes = 720.0
    return max(0.0, minutes * 60.0)


def _case_insensitive_file(directory: Path, name: str) -> str:
    wanted = name.casefold()
    try:
        for entry in directory.iterdir():
            if entry.is_file() and entry.name.casefold() == wanted:
                return str(entry)
    except OSError:
        return ""
    return ""


def _bare_filenames(text: str):
    """Filename candidates that are NOT already part of an absolute path."""
    raw = str(text or "")
    for pattern in (_QUOTED_NAME_RE, _NAMED_NAME_RE):
        for match in pattern.finditer(raw):
            yield match.group(1)
    for match in FILENAME_RE.finditer(raw):
        start = match.start()
        if start > 0 and raw[start - 1] in "\\/":
            continue
        yield match.group(0)


def _under_artifact_dirs(path: str) -> bool:
    """True when ``path`` is inside an explicitly configured artifact folder.

    Only ``WINBROW_ARTIFACT_DIRS`` counts. The default Desktop/Documents
    folders must still reject the generic folder root itself, otherwise
    ``C:\\Users\\USER\\Desktop`` would outrank the real file in a turn.
    """
    raw = os.environ.get("WINBROW_ARTIFACT_DIRS", "").strip()
    if not raw:
        return False
    try:
        target = Path(path).resolve()
    except OSError:
        return False
    for part in raw.split(os.pathsep):
        if not part.strip():
            continue
        try:
            target.relative_to(Path(part).resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def _resolve_bare_filename(name: str) -> str:
    """Absolute path for a bare filename found in tool output, or \"\"."""
    raw = str(name or "").strip().strip("\"'`").rstrip(".,;:)")
    if not raw or "\\" in raw or "/" in raw:
        return ""
    words = raw.split()
    # A greedy sentence match can include leading prose; try each suffix so
    # "saved as AI document about X.rtf" resolves to "AI document about X.rtf".
    for start in range(len(words)):
        candidate = " ".join(words[start:])
        for directory in _standard_user_dirs():
            if not directory.is_dir():
                continue
            hit = _case_insensitive_file(directory, candidate)
            if hit:
                return hit
    return ""


def _skill_tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[\W_]+", (name or "").lower()) if t}


def _is_artifact_skill(skill: str) -> bool:
    """True when this skill can create, move, or export a user-visible file."""
    name = str(skill or "").strip()
    if not name or name == "run_command":
        return False
    if name in _ARTIFACT_SKILL_NAMES:
        return True
    return bool(_skill_tokens(name) & _ARTIFACT_SKILL_TOKENS)

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
    artifacts: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        data = {
            "ts": self.ts, "utterance": self.utterance,
            "route_kind": self.route_kind, "tool_name": self.tool_name,
            "success": self.success,
            # Was 600: paths and headings late in the output were lost, so a
            # follow-up ("there is no heading in the file") could not be
            # answered from the previous turn.
            "output_snippet": self.output_snippet[:4000],
        }
        if self.artifacts:
            data["artifacts"] = self.artifacts[:12]
        return data


class SessionMemory:
    """Working memory for one conversation session."""

    def __init__(self, path: Optional[Path] = None, session_id: str = "default"):
        self.path = path or SESSION_PATH
        self.session_id = session_id
        self.turns: List[Turn] = []
        # Per-kind entity stacks, most-recent-first
        self.entities: Dict[str, List[Entity]] = {"file": [], "folder": [], "app": [], "url": [], "query": []}
        # A brain clarify question waiting for the user's next message.
        self.pending_clarification: Optional[Dict[str, Any]] = None
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
            # Migrate older sessions that predate artifact tracking: recover
            # paths from the stored snippet so follow-ups still have a target.
            for turn in self.turns:
                if not isinstance(turn.artifacts, list):
                    turn.artifacts = []
                else:
                    turn.artifacts = [str(p) for p in turn.artifacts if p]
                if turn.artifacts:
                    continue
                paths: List[str] = []
                for path in _paths_in_text(turn.output_snippet):
                    if (_is_temp_path(path) or _is_generic_user_dir(path)
                            or path in paths):
                        continue
                    parts = Path(path).parts
                    if not Path(path).suffix and len(parts) < 4:
                        continue
                    paths.append(path)
                turn.artifacts = paths[:12]
            ents = raw.get("entities", {})
            self.entities = {k: [Entity(**e) for e in v] for k, v in ents.items()}
            for turn in self.turns:
                for path in turn.artifacts:
                    kind = "folder" if not Path(path).suffix else "file"
                    self.add_entity(
                        kind, path, Path(path).name or path, ts=turn.ts)
            pending = raw.get("pending_clarification")
            if isinstance(pending, dict):
                self.pending_clarification = pending
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
                "pending_clarification": self.pending_clarification,
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
        self.pending_clarification = None
        self.save()

    # ------------------------------------------------------------------ entity writes
    def add_entity(self, kind: str, value: str, label: str = "",
                   ts: Optional[float] = None) -> None:
        if not value or kind not in self.entities:
            return
        # de-dup: move to front
        self.entities[kind] = [e for e in self.entities[kind] if e.value != value]
        self.entities[kind].insert(0, Entity(
            kind=kind, value=value, label=label or value,
            turn=len(self.turns), ts=float(ts if ts is not None else time.time()),
        ))
        self.entities[kind] = self.entities[kind][:10]

    def last_entity(self, kind: Optional[str] = None) -> Optional[Entity]:
        kinds = [kind] if kind else ["file", "folder", "app", "url", "query"]
        max_age = _session_context_max_age_s()
        now = time.time()
        for k in kinds:
            for entity in (self.entities.get(k) or []):
                if max_age > 0 and (now - float(entity.ts or 0.0)) > max_age:
                    continue
                return entity
        return None

    # ------------------------------------------------------------------ extraction
    @staticmethod
    def _paths_from_args(args: Any) -> List[str]:
        """Absolute paths carried by a step/tool's args (including nested spec)."""
        found: List[str] = []

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if isinstance(item, str) and str(key).lower() in _ABS_PATH_KEYS:
                        path = _clean_arg_path(item)
                        if path:
                            found.append(path)
                    elif isinstance(item, (dict, list)):
                        visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(args or {})
        return found

    def extract_artifacts(
        self, utterance: str, route: dict, execution: dict,
    ) -> List[str]:
        """Return absolute paths the turn created/modified/opened.

        Primary paths come from write/create/move/export steps; if a turn only
        read or opened something, that path is kept as a fallback so follow-ups
        like "where is it?" still have a concrete target.
        """
        primary: List[str] = []
        secondary: List[str] = []

        def add(bucket: List[str], path: str) -> None:
            path = str(path or "").strip()
            if (not path or path in bucket
                    or (not _under_artifact_dirs(path)
                        and (_is_temp_path(path) or _is_generic_user_dir(path)))):
                return
            bucket.append(path)

        steps = execution.get("steps")
        if isinstance(steps, list):
            for step in steps:
                if not isinstance(step, dict):
                    continue
                skill = str(step.get("skill") or "")
                bucket = primary if _is_artifact_skill(skill) else secondary
                for path in self._paths_from_args(step.get("args")):
                    add(bucket, path)
                for path in _paths_in_text(step.get("output")):
                    add(bucket, path)
                for name in _bare_filenames(str(step.get("output") or "")):
                    add(bucket, _resolve_bare_filename(name))

        tool = str(route.get("tool_name") or "")
        bucket = primary if _is_artifact_skill(tool) else secondary
        for path in self._paths_from_args(route.get("args")):
            add(bucket, path)
        for path in _paths_in_text(execution.get("output")):
            add(primary if _is_artifact_skill(tool) or primary else secondary, path)
        for name in _bare_filenames(str(execution.get("output") or "")):
            add(
                primary if _is_artifact_skill(tool) or primary else secondary,
                _resolve_bare_filename(name),
            )

        return (primary or secondary)[:12]

    def extract_entities(
        self, utterance: str, route: dict, execution: dict,
        artifacts: Optional[List[str]] = None,
    ) -> None:
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
            # Prefer the absolute path when the bare name resolves in a
            # standard user folder; the plain name is a weak fallback.
            resolved = _resolve_bare_filename(m.group(0))
            value = resolved or m.group(0)
            self.add_entity("file", value, Path(value).name)
        # Artifacts produced/opened by this turn become first-class entities so
        # "the pdf", "that deck" and "it" resolve without a disk-wide search.
        if artifacts is None:
            artifacts = self.extract_artifacts(utterance, route, execution)
        for path in artifacts:
            kind = "folder" if not Path(path).suffix or path.endswith(("\\", "/")) else "file"
            self.add_entity(kind, path, Path(path).name or path)

    # ------------------------------------------------------------------ turn recording
    def record_turn(self, utterance: str, route: dict, execution: dict) -> None:
        artifacts = self.extract_artifacts(utterance, route, execution)
        self.turns.append(Turn(
            ts=time.time(),
            utterance=utterance,
            route_kind=str(route.get("kind", "")),
            tool_name=str(route.get("tool_name") or ""),
            success=bool(execution.get("success", False)),
            output_snippet=str(execution.get("output", "")),
            artifacts=artifacts,
        ))
        self.turns = self.turns[-MAX_TURNS:]
        if execution.get("needs_input"):
            # Keep the original goal + question (and clickable options) so the
            # next short answer can be resolved by the brain.
            options = execution.get("options")
            self.pending_clarification = {
                "goal": utterance,
                "question": str(
                    execution.get("question") or execution.get("output") or ""
                ).strip(),
                "options": [
                    str(option).strip() for option in (options or [])
                    if str(option).strip()
                ] if isinstance(options, list) else [],
            }
        else:
            self.pending_clarification = None
        self.extract_entities(utterance, route, execution, artifacts=artifacts)
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
    def _last_artifact_turn(self) -> Optional[Turn]:
        for turn in reversed(self.turns):
            if turn.artifacts:
                return turn
        return None

    def _fresh_artifact_turn(self, lookback: int = 8) -> Optional[Turn]:
        """Most recent artifact turn that still belongs to this task.

        A turn from an older, unrelated request must never become the answer
        to a follow-up. Without the age bound, "there is no heading in the
        file" was answered about a presentation produced days earlier because
        the WordPad turn itself tracked no absolute path.
        """
        max_age = _artifact_context_max_age_s()
        now = time.time()
        for turn in reversed(self.turns[-max(lookback, 1):]):
            if not turn.artifacts:
                continue
            if max_age > 0 and (now - float(turn.ts or 0.0)) > max_age:
                return None
            return turn
        return None

    def last_artifacts(self, limit: int = 6) -> List[str]:
        """Paths produced/opened by the most recent turn that had any."""
        turn = self._last_artifact_turn()
        return list(turn.artifacts[:limit]) if turn else []

    def reference_hint(self, utterance: str) -> str:
        """Explain what a short follow-up ('where is the pdf?') refers to.

        Attached to the brain prompt so the model answers with the artifact
        from the previous turn instead of searching the whole disk. Also flags
        a format mismatch (asked for a PDF, produced HTML) so the reply is
        honest and offers conversion.
        """
        text = str(utterance or "").strip()
        if not text:
            return ""
        max_age = _artifact_context_max_age_s()
        now = time.time()
        turns_with = [
            t for t in self.turns[-8:]
            if t.artifacts and (max_age <= 0 or now - float(t.ts or 0.0) <= max_age)
        ]
        if not turns_with:
            return ""
        low = text.lower()
        noun = _artifact_noun(text)
        where_q = bool(_WHERE_QUERY_RE.search(low))
        pronoun = bool(_PRONOUN_REF_RE.search(low))
        action_ref = bool(_ACTION_REF_RE.search(low)) and pronoun
        definite = bool(_THE_ARTIFACT_RE.search(low))
        if not (noun or definite or (where_q and pronoun) or action_ref):
            return ""
        # A fresh "create/make a new X" request is not a reference to the old
        # artifact unless it explicitly points back at it ("... of it").
        if _NEW_REQUEST_RE.search(low) and not where_q and not action_ref and not pronoun:
            return ""

        expected = _ARTIFACT_NOUN_EXT.get(noun) if noun else None
        latest = turns_with[-1]
        matching: Optional[Turn] = None
        if expected:
            for candidate in reversed(turns_with):
                if any(
                    Path(path).suffix.lower().lstrip(".") in expected
                    for path in candidate.artifacts
                ):
                    matching = candidate
                    break
        focus = matching or latest
        focus_paths = list(focus.artifacts[:6])
        if expected:
            focus_paths.sort(key=lambda p: 0 if Path(p).suffix.lower().lstrip(".")
                            in expected else 1)
        latest_paths = list(latest.artifacts[:6])
        actual_exts = {
            Path(path).suffix.lower().lstrip(".") for path in latest_paths
            if Path(path).suffix
        }

        lines = [
            "FOLLOW-UP ARTIFACT CONTEXT (authoritative — do NOT run a "
            "disk-wide search for this):",
            f"  Most recent artifact-producing request: {latest.utterance[:300]}",
        ]
        snippet = re.sub(r"\s+", " ", latest.output_snippet or "").strip()
        if snippet:
            lines.append(f"  Its result: {snippet[:400]}")
        lines.append("  Its artifact(s):")
        for path in latest_paths:
            lines.append(f"    - {path}")
        if matching is not None and matching is not latest:
            lines.append(f"  Most recent artifact matching '{noun}':")
            lines.append(f"    request: {matching.utterance[:200]}")
            match_snippet = re.sub(
                r"\s+", " ", matching.output_snippet or "").strip()
            if match_snippet:
                lines.append(f"    result: {match_snippet[:240]}")
            for path in focus_paths:
                lines.append(f"    - {path}")
        if expected and matching is None:
            actual = ", ".join(sorted(actual_exts)) or "none"
            lines.append(
                f"  The user asked for '{noun}', but the most recent artifact "
                f"format is: {actual}."
            )
            lines.append(
                "  Say so explicitly and offer to export/convert it. Do NOT "
                "substitute an unrelated file that merely shares the extension."
            )
        elif expected:
            if matching is not None and matching is not latest:
                latest_ext = ", ".join(sorted(actual_exts)) or "unknown"
                lines.append(
                    f"  Note: the most recent artifact is {latest_ext}, not "
                    f"{noun}. If the user means the thing just created, use "
                    "the latest path listed first and offer conversion; if "
                    "they mean the most recent "
                    f"{noun}, use the matching path."
                )
            else:
                lines.append(
                    f"  The user's '{noun}' most likely refers to the matching "
                    "path(s) above. Answer with the exact path or act on it."
                )
        else:
            lines.append(
                "  The user's reference most likely points at the latest "
                "artifact(s) above. Answer with the exact path or act on it."
            )
        return "\n".join(lines)

    def context_block(self, max_turns: int = 6) -> str:
        """Compact conversation context for the brain prompt."""
        max_age = _session_context_max_age_s()
        now = time.time()
        # Never present a days-old turn as "recent": a new chat must not inherit
        # the artifact/answer of an unrelated earlier task.
        live_turns = [
            t for t in self.turns
            if max_age <= 0 or now - float(t.ts or 0.0) <= max_age
        ]
        lines = []
        if live_turns:
            lines.append("Recent conversation:")
            for t in live_turns[-max_turns:]:
                status = "ok" if t.success else "failed"
                line = f"  user: {t.utterance}  [{t.tool_name or t.route_kind} → {status}]"
                snippet = re.sub(r"\s+", " ", t.output_snippet or "").strip()
                if snippet:
                    # 1500 (was 300) so the document headings/body produced in
                    # the previous turn are still visible to the planner.
                    line += f" → {snippet[:1500]}"
                if t.artifacts:
                    line += " | artifacts: " + ", ".join(t.artifacts[:4])
                lines.append(line)
        if self.pending_clarification:
            goal = str(self.pending_clarification.get("goal") or "").strip()
            question = str(self.pending_clarification.get("question") or "").strip()
            options = [
                str(option).strip()
                for option in (self.pending_clarification.get("options") or [])
                if str(option).strip()
            ]
            lines.append("Pending clarification:")
            if goal:
                lines.append(f"  Original goal: {goal}")
            if question:
                lines.append(f"  Question asked: {question}")
            if options:
                lines.append("  Options: " + " | ".join(
                    f"{index}) {option}"
                    for index, option in enumerate(options, 1)
                ))
            lines.append(
                "  If the latest user message is a short answer, a clicked "
                "option, or another choice, apply it to the Original goal and "
                "continue from there. Do not restart discovery."
            )
        live_text = " ".join(
            [t.utterance for t in live_turns]
            + [path for t in live_turns for path in t.artifacts]
        ).casefold()
        ents = []
        for kind, stack in self.entities.items():
            for e in stack[:3]:
                if max_age > 0 and (now - float(e.ts or 0.0)) > max_age:
                    continue
                # An entity is only current when a live turn still mentions
                # it; otherwise a days-old file leaks into a new request.
                if str(e.value or "").casefold() not in live_text:
                    continue
                ents.append(f"{kind}={e.value}")
        if ents:
            lines.append("Mentioned entities (most recent first): " + "; ".join(ents[:10]))
        last_artifact_turn = self._fresh_artifact_turn()
        if last_artifact_turn is not None:
            lines.append("Last artifacts (from the most recent producing turn):")
            for path in last_artifact_turn.artifacts[:6]:
                lines.append(f"  - {path}")
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
