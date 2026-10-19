"""
Procedural Memory — Capture Plans, Replay Skills (M2)
======================================================
When the brain successfully completes a multi-step task, the whole plan is
captured as a named skill (JSON in tools/skills.json, human-editable).
The next similar utterance replays the exact steps locally — no LLM, no
planning latency (<2s for CLI/atom flows).

Storage format (tools/skills.json):
  [{ "id", "name", "utterance", "steps": [{"skill","args"}], "created",
     "uses", "last_used", "source" }]

Matching: token-overlap (Jaccard) on informative tokens, threshold-gated.
Replay:   step["skill"] → shared executor (agent.run_skill) with stored args.
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

log = logging.getLogger("winbrow.memory.skills")

SKILLS_FILENAME = "skills.json"
DEFAULT_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent / "tools"

MAX_STEPS = 12                                  # replay hard cap
MATCH_THRESHOLD = 0.66                          # Jaccard token-overlap gate
_LOANWORDS = {"a", "an", "the", "and", "or", "in", "on", "for", "to", "my",
              "it", "its", "that", "this", "those", "some", "please", "then",
              "up", "down", "be", "of", "with", "at", "as", "do", "did", "just"}


def _canon_tokens(text: str) -> set[str]:
    """Informative-token set for matching (lower, stopword-free)."""
    toks = [t for t in re.split(r"[\W_]+", (text or "").lower()) if t]
    return {t for t in toks if t not in _LOANWORDS}


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60] or "skill"


@dataclass
class SkillStep:
    skill: str
    args: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"skill": self.skill, "args": self.args}


@dataclass
class SkillRecord:
    id: str
    name: str                      # human-facing name (from planner/utterance)
    utterance: str                 # matching phrase (what the user said)
    steps: List[SkillStep]         # replay: exact tools + args
    created: float = 0.0
    uses: int = 0
    last_used: float = 0.0
    source: str = "brain"          # "brain" | "manual"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["steps"] = [{"skill": s.skill, "args": s.args} for s in self.steps]
        return d

    @staticmethod
    def from_dict(item: dict) -> "SkillRecord":
        steps = [SkillStep(skill=s["skill"], args={k: str(v) for k, v in (s.get("args") or {}).items()})
                 for s in (item.get("steps") or [])]
        return SkillRecord(
            id=item["id"], name=item.get("name", item["id"]),
            utterance=item.get("utterance", ""), steps=steps,
            created=float(item.get("created", 0.0)),
            uses=int(item.get("uses", 0)),
            last_used=float(item.get("last_used", 0.0)),
            source=item.get("source", "brain"),
        )


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

    def list(self) -> List[str]:
        return [r.to_dict() for r in self._records]

    def get(self, skill_id: str) -> Optional[SkillRecord]:
        low = skill_id.lower()
        return next((r for r in self._records if r.id.lower() == low), None)

    def save(
        self, name: str, utterance: str, steps: List[dict], source: str = "brain"
    ) -> Optional[SkillRecord]:
        """Capture a successful plan. Dedups by id; truncated to MAX_STEPS."""
        plan = []
        for s in steps[:MAX_STEPS]:
            name_ok = (s.get("skill") or "").strip()
            if not name_ok:
                continue
            plan.append(SkillStep(skill=name_ok,
                                  args={str(k): str(v) for k, v in (s.get("args") or {}).items()}))
        if not plan:
            return None
        rid = _slugify(name or utterance)
        now = time.time()
        rec = self.get(rid)
        if rec is not None:
            rec.utterance = utterance or rec.utterance
            rec.steps = plan
            rec.source = source
            rec.last_used = now
            log.info(f"Skill '{rid}' updated ({len(plan)} steps)")
        else:
            rec = SkillRecord(id=rid, name=name or rid, utterance=utterance or name,
                              steps=plan, created=now, last_used=now, source=source)
            self._records.append(rec)
            log.info(f"Skill '{rid}' captured ({len(plan)} steps) from '{utterance[:60]}'")
        self._save()
        return rec

    def find_match(self, utterance: str, threshold: float = MATCH_THRESHOLD
                   ) -> Optional[Tuple[SkillRecord, float]]:
        """Best matching skill for an utterance (≥ threshold) or None."""
        if not self._records or not utterance:
            return None
        best = None
        best_score = 0.0
        key = _canon_tokens(utterance)
        for rec in self._records:
            stored = rec.utterance or rec.name
            score = match_score(utterance, stored)
            if score > best_score or (best is None and score >= threshold):
                if score > best_score:
                    best, best_score = rec, score
        if best is None:
            return None
        if best_score >= threshold:
            return (best, round(best_score, 3))
        return None

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
