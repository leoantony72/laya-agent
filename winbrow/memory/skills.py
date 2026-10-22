"""
Procedural Memory — Capture Plans, Replay Skills (M2)
======================================================
When the brain completes a multi-step task cleanly, the whole plan becomes a
proposed skill (JSON in tools/skills.json, human-editable). After review it
can replay without the planner; a parameterized skill makes one small
extraction call to map the new request onto its placeholders.

Storage format (tools/skills.json):
  [{ "id", "name", "utterance", "description", "triggers", "parameters",
     "steps": [{"skill","args"}], "status", "created", "uses", "last_used",
     "source" }]

Matching: token-overlap (Jaccard) on informative tokens, threshold-gated.
          Only approved skills are replayable; brain captures start as
          "proposed" so a human reviews them first.
Replay:   step["skill"] → shared executor (agent.run_skill) with stored args.
          Steps may contain {{parameter}} placeholders; callers resolve them
          with render_steps() before replay.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

log = logging.getLogger("winbrow.memory.skills")

SKILLS_FILENAME = "skills.json"
DEFAULT_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "tools"

MAX_STEPS = 12                                  # replay hard cap
MATCH_THRESHOLD = 0.66                          # Jaccard token-overlap gate
_LOANWORDS = {"a", "an", "the", "and", "or", "in", "on", "for", "to", "my",
              "it", "its", "that", "this", "those", "some", "please", "then",
              "up", "down", "be", "of", "with", "at", "as", "do", "did", "just"}
_PLACEHOLDER_RE = re.compile(
    r"\{\{\s*([A-Za-z_][\w.-]*)\s*(?:\|\s*([A-Za-z_]+)\s*)?\}\}"
)
_VALID_STATUSES = {"proposed", "approved"}


class MissingSkillParameters(ValueError):
    """Replay was attempted without values for required placeholders."""

    def __init__(self, names: List[str]):
        self.names = list(names)
        super().__init__("missing skill parameter(s): " + ", ".join(self.names))


def _canon_tokens(text: str) -> set[str]:
    """Informative-token set for matching (lower, stopword-free)."""
    toks = [t for t in re.split(r"[\W_]+", (text or "").lower()) if t]
    return {t for t in toks if t not in _LOANWORDS}


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60] or "skill"


_SITE_NAMES = (
    ("drive.google.com", "Google Drive"),
    ("docs.google.com", "Google Docs"),
    ("onedrive.live.com", "OneDrive"),
    ("dropbox.com", "Dropbox"),
    ("aspose.app", "Aspose"),
)


def _site_from_steps(steps: List[dict]) -> str:
    for step in steps:
        args = step.get("args") or {}
        raw = str(args.get("url") or "").strip()
        if not raw:
            continue
        if "://" not in raw:
            raw = "https://" + raw
        try:
            host = (urlsplit(raw).hostname or "").lower()
        except ValueError:
            continue
        if host.startswith("www."):
            host = host[4:]
        for domain, label in _SITE_NAMES:
            if host == domain or host.endswith("." + domain):
                return label
    return ""


def suggest_skill_name(steps: List[dict], utterance: str = "") -> str:
    """Return a reusable, intent-based label instead of the raw request.

    Captured skills are matched by their utterance/triggers, so the human-facing
    name can describe the workflow generically and stay useful for every file
    or folder, not just the one used during capture.
    """
    skills = [
        str(step.get("skill") or "").strip()
        for step in steps
        if str(step.get("skill") or "").strip()
    ]
    if not skills:
        return "Captured workflow"

    names = set(skills)
    site = _site_from_steps(steps)
    if "browser_upload_file" in names:
        return f"Upload a file to {site}" if site else "Upload a file in the browser"
    if "download_file" in names:
        if "mcp_filesystem_move_file" in names:
            return "Download a file to a folder"
        return "Download a file"
    if "mcp_filesystem_move_file" in names:
        return "Move a file to a folder"
    if "mcp_filesystem_copy_file" in names:
        return "Copy a file to a folder"
    if "read_document" in names and names & {
        "create_text_file", "mcp_filesystem_write_file",
    }:
        return "Create a document from a file"
    if "browser_navigate" in names and names & {
        "browser_read_full", "browser_read_page",
    }:
        return "Read a web page"
    if names == {"run_command"}:
        return "Run a command"

    readable: List[str] = []
    for skill in skills:
        label = skill.replace("_", " ")
        if label not in readable:
            readable.append(label)
        if len(readable) == 3:
            break
    return ("Run " + ", then ".join(readable))[:80]


@dataclass
class SkillStep:
    skill: str
    args: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"skill": self.skill, "args": self.args}


@dataclass
class SkillParam:
    name: str
    description: str = ""
    required: bool = False
    default: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(item: Any) -> "SkillParam":
        if isinstance(item, str):
            return SkillParam(name=item)
        data = item or {}
        return SkillParam(
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            required=bool(data.get("required", False)),
            default=str(data.get("default", "")),
        )


@dataclass
class SkillRecord:
    id: str
    name: str                      # human-facing label
    utterance: str                 # original request (matching example)
    steps: List[SkillStep]         # replay: tools + args (may hold {{params}})
    description: str = ""          # purpose + when to use it
    triggers: List[str] = field(default_factory=list)   # extra match phrases
    parameters: List[SkillParam] = field(default_factory=list)
    status: str = "approved"       # "proposed" (needs review) | "approved"
    created: float = 0.0
    uses: int = 0
    last_used: float = 0.0
    source: str = "brain"          # "brain" | "manual"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["steps"] = [{"skill": s.skill, "args": s.args} for s in self.steps]
        d["parameters"] = [p.to_dict() for p in self.parameters]
        return d

    @staticmethod
    def from_dict(item: dict) -> "SkillRecord":
        steps = [SkillStep(skill=s["skill"], args={k: str(v) for k, v in (s.get("args") or {}).items()})
                 for s in (item.get("steps") or [])]
        status = str(item.get("status") or "approved")
        if status not in _VALID_STATUSES:
            status = "proposed"
        return SkillRecord(
            id=item["id"], name=item.get("name", item["id"]),
            utterance=item.get("utterance", ""), steps=steps,
            description=str(item.get("description", "")),
            triggers=[str(t) for t in (item.get("triggers") or []) if str(t).strip()],
            parameters=[SkillParam.from_dict(p) for p in (item.get("parameters") or [])],
            status=status,
            created=float(item.get("created", 0.0)),
            uses=int(item.get("uses", 0)),
            last_used=float(item.get("last_used", 0.0)),
            source=item.get("source", "brain"),
        )

    def placeholder_names(self) -> set[str]:
        names: set[str] = set()
        for step in self.steps:
            for value in step.args.values():
                names.update(m.group(1) for m in _PLACEHOLDER_RE.finditer(str(value)))
        return names

    def render_steps(self, values: Optional[Dict[str, Any]] = None) -> List[dict]:
        """Resolve {{placeholders}} for one replay.

        Values come from the new request; parameter defaults fill gaps. Raises
        MissingSkillParameters instead of replaying literal {{...}} text.
        """
        supplied = {str(k): "" if v is None else str(v) for k, v in (values or {}).items()}
        resolved: Dict[str, str] = {}
        for p in self.parameters:
            value = supplied.get(p.name)
            if value is None or not value.strip():
                value = p.default
            resolved[p.name] = value or ""

        missing = [p.name for p in self.parameters
                   if p.required and not resolved.get(p.name)]
        missing.extend(n for n in self.placeholder_names() if n not in resolved)
        missing = list(dict.fromkeys(missing))
        if missing:
            raise MissingSkillParameters(missing)

        def _sub(value: Any) -> str:
            def repl(m: re.Match) -> str:
                key = m.group(1)
                filt = (m.group(2) or "").lower()
                out = resolved[key]
                if filt in ("basename", "filename"):
                    out = re.split(r"[\\/]", out.rstrip("\\/"))[-1]
                elif filt == "lower":
                    out = out.lower()
                elif filt == "upper":
                    out = out.upper()
                return out

            return _PLACEHOLDER_RE.sub(repl, str(value))

        return [
            {"skill": s.skill, "args": {k: _sub(v) for k, v in s.args.items()}}
            for s in self.steps
        ]


_WIN_ABS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_BARE_FILENAME_RE = re.compile(r"^[^\\/\s]+\.[A-Za-z0-9]{1,8}$")
_PATH_ARG_KEYS = {"path", "file_path", "folder", "destination", "source",
                  "target", "file", "filename", "name", "dir", "directory"}


def _param_description(key: str, is_file: bool) -> str:
    if is_file:
        return f"File path for '{key}' from the new request."
    return f"Folder path for '{key}' from the new request."


def parameterize_steps(
    steps: List[dict], utterance: str
) -> Tuple[List[dict], List[SkillParam]]:
    """Turn request-specific paths/filenames into {{placeholders}}.

    Conservative: only values that appeared in the original request and look
    like an absolute path or a filename are parameterized. Everything else is
    left literal so a reviewed skill cannot silently change behaviour.
    """
    low_utterance = (utterance or "").lower().replace("/", "\\")
    params: List[SkillParam] = []
    used: Dict[str, str] = {}
    out: List[dict] = []

    def _name_for(key: str, value: str, is_file: bool, is_abs: bool) -> str:
        if key.lower() in ("filename", "file"):
            base = "filename"
        elif is_abs and is_file:
            base = "file_path"
        elif is_abs:
            base = "folder_path"
        else:
            base = "filename"
        n = 1
        candidate = base
        while candidate in used and used[candidate] != value:
            n += 1
            candidate = f"{base}_{n}"
        used[candidate] = value
        return candidate

    for step in steps:
        args = dict(step.get("args") or {})
        for key, raw in list(args.items()):
            value = str(raw)
            if not value or _PLACEHOLDER_RE.search(value):
                continue
            if key.lower() not in _PATH_ARG_KEYS:
                continue
            is_abs = bool(_WIN_ABS_PATH_RE.match(value))
            is_file = bool(_BARE_FILENAME_RE.match(value))
            if not (is_abs or is_file):
                continue
            haystack = value.lower().replace("/", "\\")
            basename = re.split(r"[\\/]", haystack.rstrip("\\"))[-1]
            stem = basename.rsplit(".", 1)[0]
            norm_utterance = re.sub(r"[\s_\-]+", "", low_utterance)
            basename_hit = any(
                len(candidate) >= 4
                and re.sub(r"[\s_\-]+", "", candidate) in norm_utterance
                for candidate in (basename, stem)
            )
            if haystack not in low_utterance and not basename_hit:
                continue
            file_like = is_file or bool(re.search(r"\.[A-Za-z0-9]{1,8}$", basename))
            name = _name_for(key, value, file_like, is_abs)
            args[key] = "{{" + name + "}}"
            if not any(p.name == name for p in params):
                params.append(SkillParam(
                    name=name,
                    description=_param_description(key, file_like),
                    required=True,
                ))
        out.append({**step, "args": args})
    return out, params


def match_score(utterance: str, stored: str) -> float:
    """Jaccard overlap of informative tokens (0..1)."""
    a, b = _canon_tokens(utterance), _canon_tokens(stored)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class SkillMemory:
    """JSON-file store of captured multi-step skills (procedural memory)."""

    def __init__(self, path: Optional[Path] = None):
        if path is None:
            env_dir = os.environ.get("WINBROW_SKILLS_DIR")
            path = (Path(env_dir) / SKILLS_FILENAME) if env_dir else (DEFAULT_SKILLS_DIR / SKILLS_FILENAME)
        self.path = Path(path)
        self._records: List[SkillRecord] = self._load()

    # ------------------------------------------------------------------ IO
    def _load(self) -> List[SkillRecord]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            recs = [SkillRecord.from_dict(x) for x in data]
            log.info(f"Loaded {len(recs)} skills from {self.path}")
            return recs
        except FileNotFoundError:
            return []
        except Exception as e:
            log.warning(f"Corrupt skills file ignored ({self.path}): {e}")
            return []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump([r.to_dict() for r in self._records], f, indent=2)
        os.replace(tmp, self.path)

    def reload(self) -> None:
        self._records = self._load()

    # ------------------------------------------------------------------ API
    def __len__(self) -> int:
        return len(self._records)

    def list(self) -> List[dict]:
        return [r.to_dict() for r in self._records]

    def get(self, skill_id: str) -> Optional[SkillRecord]:
        low = skill_id.lower()
        return next((r for r in self._records if r.id.lower() == low), None)

    def save(
        self,
        name: str,
        utterance: str,
        steps: List[dict],
        source: str = "brain",
        description: str = "",
        triggers: Optional[List[str]] = None,
        parameters: Optional[List[dict]] = None,
        status: str = "approved",
    ) -> Optional[SkillRecord]:
        """Capture a successful plan. Dedups by id; truncated to MAX_STEPS.

        Brain-initiated captures pass status="proposed" so they are reviewed
        before becoming replayable. Manual saves default to approved.
        """
        plan = []
        for s in steps[:MAX_STEPS]:
            name_ok = (s.get("skill") or "").strip()
            if not name_ok:
                continue
            plan.append(SkillStep(skill=name_ok,
                                  args={str(k): str(v) for k, v in (s.get("args") or {}).items()}))
        if not plan:
            return None
        if status not in _VALID_STATUSES:
            status = "proposed"
        params = [SkillParam.from_dict(p) for p in (parameters or [])]
        rid = _slugify(name or utterance)
        now = time.time()
        rec = self.get(rid)
        if rec is not None:
            rec.utterance = utterance or rec.utterance
            rec.steps = plan
            rec.description = description or rec.description
            if triggers is not None:
                rec.triggers = [str(t) for t in triggers if str(t).strip()]
            rec.parameters = params
            rec.status = status
            rec.source = source
            rec.last_used = now
            log.info(f"Skill '{rid}' updated ({len(plan)} steps, status={status})")
        else:
            rec = SkillRecord(id=rid, name=name or rid, utterance=utterance or name,
                              steps=plan, description=description,
                              triggers=[str(t) for t in (triggers or []) if str(t).strip()],
                              parameters=params, status=status,
                              created=now, last_used=now, source=source)
            self._records.append(rec)
            log.info(f"Skill '{rid}' captured ({len(plan)} steps, status={status}) "
                     f"from '{utterance[:60]}'")
        self._save()
        return rec

    def find_match(self, utterance: str, threshold: float = MATCH_THRESHOLD
                   ) -> Optional[Tuple[SkillRecord, float]]:
        """Best approved skill for an utterance (≥ threshold) or None."""
        if not self._records or not utterance:
            return None
        best = None
        best_score = 0.0
        for rec in self._records:
            if rec.status != "approved":
                continue
            phrases = [rec.utterance or rec.name, rec.description, *rec.triggers]
            score = max((match_score(utterance, p) for p in phrases if p), default=0.0)
            if score > best_score or (best is None and score >= threshold):
                if score > best_score:
                    best, best_score = rec, score
        if best is None:
            return None
        if best_score >= threshold:
            return (best, round(best_score, 3))
        return None

    def approve(self, skill_id: str) -> bool:
        """Enable a proposed skill after human review."""
        rec = self.get(skill_id)
        if rec is None:
            return False
        rec.status = "approved"
        self._save()
        return True

    def proposed(self) -> List[dict]:
        """Skills awaiting review (not replayable yet)."""
        return [r.to_dict() for r in self._records if r.status == "proposed"]

    def mark_used(self, skill_id: str) -> None:
        rec = self.get(skill_id)
        if rec:
            rec.uses += 1
            rec.last_used = time.time()
            self._save()

    def delete(self, skill_id: str) -> bool:
        low = skill_id.lower()
        before = len(self._records)
        self._records = [r for r in self._records if r.id.lower() != low]
        if len(self._records) < before:
            self._save()
            return True
        return False

    def clear(self) -> None:
        self._records = []
        self._save()


# ---------------------------------------------------------------------------
# singleton (path independent via WINBROW_SKILLS_DIR)
# ---------------------------------------------------------------------------
_skills: Optional[SkillMemory] = None


def get_skills() -> SkillMemory:
    global _skills
    if _skills is None:
        _skills = SkillMemory()
    return _skills


def reset_skills() -> SkillMemory:
    """Fresh instance (path could have changed); used by tests & server settings."""
    global _skills
    _skills = SkillMemory()
    return _skills
