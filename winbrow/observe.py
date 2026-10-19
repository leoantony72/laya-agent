"""
M5 - Windows "observe" background service: proactive suggestions.

Pattern-based, LLM-free, guaranteed <50ms per refresh on 2 cores:

  • repeat_open   — the same file/folder opened ≥2 days recently → "open again?"
  • yesterday     — opened yesterday, not since → "you opened X yesterday"
  • app_pattern    — apps launched on ≥3 distinct recent days → "launch X?"
  • search_repeat — same web query ≥2 times lately → "search X again?"

Suggestions live in RAM; dismissals persist in a tiny JSON so a dismissed
suggestion never resurfaces for COOLDOWN_S hours.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

log = logging.getLogger("winbrow.observe")

REPEAT_OPEN_DAYS = 14       # window for repeat-open detection
APP_PATTERN_DAYS = 14
COOLDOWN_SEC = 12 * 3600    # dismissed suggestion stays quiet for 12h
MAX_SUGGESTIONS = 4

ULARGE_PER_DAY = 86400.0  # seconds per day (query window math)

# kinds constants (mirror memory/store.py to avoid import cycle at module load)
K_FILE_OPEN = "file_open"
K_FOLDER_OPEN = "folder_open"
K_APP_LAUNCH = "app_launch"
K_WEB_SEARCH = "web_search"


@dataclass
class Suggestion:
    id: str
    kind: str                    # repeat_open | yesterday | app_pattern | search_repeat
    title: str                   # short card text
    utterance: str               # what to run if accepted
    why: str                     # human explanation ("3 times this week")
    score: float = 0.0
    target: str = ""             # dedupe/dismiss key basis (path/app/query)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "title": self.title,
            "utterance": self.utterance, "why": self.why,
            "score": round(self.score, 2),
        }


class DismissStore:
    """Persisted dismissal ledger: {id-ish key: timestamp}. Tiny + atomic."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._data = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            log.debug(f"Dismiss save failed: {e}")

    def dismiss(self, key: str) -> None:
        self._data[key] = time.time()
        # prune old entries past 2× cooldown
        cutoff = time.time() - COOLDOWN_SEC * 2
        self._data = {k: v for k, v in self._data.items() if v >= cutoff}
        self.save()

    def dismissed(self, key: str) -> bool:
        ts = self._data.get(key)
        return ts is not None and (time.time() - ts) < COOLDOWN_SEC


def _stable_id(prefix: str, target: str) -> str:
    import hashlib
    return f"{prefix}:{hashlib.sha1(target.lower().encode()).hexdigest()[:10]}"


class Observer:
    """Builds proactive suggestions from episodic memory. Pure + injectable."""

    def __init__(self, store=None, dismiss_path: Optional[Path] = None):
        # store duck-types get_recent(kind=...); first duck-typed source wins
        self._store = store
        if self._store is None:
            from .memory.store import get_store
            self._store = get_store()
        if dismiss_path is None:
            dismiss_path = Path(os.environ.get(
                "WINBROW_OBSERVE_DIR",
                str(Path("data") / "observe"))) / "dismissed.json"
        self.dismissals = DismissStore(dismiss_path)
        self.current: List[Suggestion] = []

    # ------------------------------------------------------------------ main
    def refresh(self, max_n: int = MAX_SUGGESTIONS) -> List[Suggestion]:
        """Rebuild suggestion list from memory. <50ms target on 2 cores."""
        out: List[Suggestion] = []
        now = time.time()
        seen_ids = set()

        def add(sug: Suggestion) -> None:
            if sug.id in seen_ids:
                return
            if self.dismissals.dismissed(sug.id):
                return
            seen_ids.add(sug.id)
            out.append(sug)

        for maker in (self._suggest_files, self._suggest_apps, self._suggest_searches):
            try:
                for sug in maker():
                    add(sug)
            except Exception as e:
                log.debug(f"Suggestion source {maker.__name__} failed: {e}")

        out.sort(key=lambda s: -s.score)
        self.current = out[:max_n]
        return self.current

    # ------------------------------------------------------------------ sources
    def _recent(self, kind: str, days: float, limit: int = 200):
        cutoff = time.time() - days * ULARGE_PER_DAY
        out = []
        for e in self._store.get_recent(limit=limit, kind=kind):
            if e.timestamp >= cutoff:
                out.append(e)
        return out

    def _suggest_files(self) -> List[Suggestion]:
        """Repeat-opened files/folders — the plan's flagship suggestion."""
        events = self._recent(K_FILE_OPEN, REPEAT_OPEN_DAYS) + \
            self._recent(K_FOLDER_OPEN, REPEAT_OPEN_DAYS)
        by_key: dict = {}
        for e in events:
            key = e.path.lower() if e.path else e.label.lower()
            if not key or key == "none":
                continue
            meta = by_key.setdefault(key, {"label": e.label or Path(e.path).name,
                                           "path": e.path, "times": 0,
                                           "last": 0.0, "folder": e.kind == K_FOLDER_OPEN})
            meta["times"] += 1
            meta["last"] = max(meta["last"], e.timestamp)
        out: List[Suggestion] = []
        for key, m in by_key.items():
            now = time.time()
            days_since = (now - m["last"]) / ULARGE_PER_DAY
            if m["times"] >= 2:
                label = m["label"]
                title = f"Open {label} again?"
                why = (f"opened {m['times']} times in the last "
                       f"{REPEAT_OPEN_DAYS} days")
                utt = (f"open {m['path']}" if m["path"]
                       else f"open {label}")
                out.append(Suggestion(
                    id=_stable_id("open", key), kind="repeat_open",
                    title=title, utterance=utt, why=why,
                    score=m["times"] * 2.0 - days_since, target=key))
        return out

    def _suggest_apps(self) -> List[Suggestion]:
        """Apps launched on ≥3 distinct days recently."""
        events = self._recent(K_APP_LAUNCH, APP_PATTERN_DAYS)
        by_app: dict = {}
        for e in events:
            app = (e.app or e.label or "").strip()
            if not app:
                continue
            days = by_app.setdefault(app.lower(), {"app": app, "days": set()})
            days["days"].add(time.strftime("%Y-%m-%d", time.localtime(e.timestamp)))
        out: List[Suggestion] = []
        for app, m in sorted(by_app.items(), key=lambda kv: -len(kv[1]["days"])):
            n_days = len(m["days"])
            if n_days < 3:
                continue
            out.append(Suggestion(
                id=_stable_id("app", app),
                kind="app_pattern",
                title=f"Launch {m['app']}?",
                utterance=f"open {m['app']}",
                why=f"used on {n_days} different days recently",
                score=n_days * 1.5, target=app))
        return out

    def _suggest_searches(self) -> List[Suggestion]:
        """Repeated web searches."""
        events = self._recent(K_WEB_SEARCH, REPEAT_OPEN_DAYS)
        by_q: dict = {}
        for e in events:
            q = (e.content or e.label or "").strip().lower()
            if not q:
                continue
            by_q.setdefault(q, {"first": e.label or e.content or q, "n": 0})
            by_q[q]["n"] += 1
        out: List[Suggestion] = []
        for q, m in sorted(by_q.items(), key=lambda kv: -kv[1]["n"]):
            if m["n"] < 2:
                continue
            display = m["first"]
            utt = display if display.lower().startswith("search") \
                else f"search for {display}"
            out.append(Suggestion(
                id=_stable_id("search", q), kind="search_repeat",
                title=f"Search {display[:40]} again?", utterance=utt,
                why=f"searched {m['n']} times recently",
                score=m["n"] * 1.2, target=q))
        return out
