"""Task/trace persistence for the desktop UI.

The append-only JSONL trace (``logs/trace.jsonl``) stays the raw audit log.
This SQLite database is the queryable index the island/dashboard use to list
tasks, reopen a finished run, and replay it event by event after a restart.

One writer (the event bus) keeps it consistent; WAL mode lets the UI read
while a task is running.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("winbrow.tasks.store")

DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "tasks.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    session_id  TEXT DEFAULT '',
    goal        TEXT DEFAULT '',
    status      TEXT DEFAULT 'running',
    provider    TEXT DEFAULT '',
    model       TEXT DEFAULT '',
    summary     TEXT DEFAULT '',
    error       TEXT DEFAULT '',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    skill       TEXT DEFAULT '',
    args_json   TEXT DEFAULT '{}',
    status      TEXT DEFAULT 'running',
    output      TEXT DEFAULT '',
    warning     TEXT DEFAULT '',
    success     INTEGER DEFAULT 0,
    started_at  REAL NOT NULL,
    ended_at    REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_steps_task ON steps(task_id, idx);
CREATE TABLE IF NOT EXISTS events (
    task_id    TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    step_id    TEXT DEFAULT '',
    type       TEXT NOT NULL,
    ts         REAL NOT NULL,
    payload    TEXT DEFAULT '{}',
    PRIMARY KEY (task_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_events_task_ts ON events(task_id, ts);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
"""


def db_path() -> Path:
    raw = os.environ.get("WINBROW_TASKS_DB", "").strip()
    return Path(raw) if raw else DEFAULT_DB


class TaskStore:
    """Small, dependency-free SQLite store. Safe to call from any thread."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else db_path()
        self._lock = threading.RLock()
        self._init()

    # ------------------------------------------------------------------ plumbing
    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init(self) -> None:
        try:
            with self._lock, self._connect() as conn:
                conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            log.warning(f"task store init failed ({exc}); trace replay disabled.")

    def _write(self, sql: str, params: tuple) -> None:
        try:
            with self._lock, self._connect() as conn:
                conn.execute(sql, params)
        except sqlite3.Error as exc:
            # Persistence must never break a running task.
            log.debug(f"task store write failed ({exc}).")

    # ------------------------------------------------------------------ tasks
    def start_task(
        self,
        task_id: str,
        goal: str = "",
        session_id: str = "",
        provider: str = "",
        model: str = "",
    ) -> None:
        now = time.time()
        self._write(
            "INSERT INTO tasks (id, session_id, goal, status, provider, model,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET goal=excluded.goal,"
            " session_id=excluded.session_id, status='running',"
            " provider=excluded.provider, model=excluded.model,"
            " updated_at=excluded.updated_at",
            (task_id, session_id, goal, "running", provider, model, now, now),
        )

    def finish_task(
        self,
        task_id: str,
        status: str,
        summary: str = "",
        error: str = "",
    ) -> None:
        self._write(
            "UPDATE tasks SET status=?, summary=?, error=?, updated_at=?"
            " WHERE id=?",
            (status, summary, error, time.time(), task_id),
        )
        if status != "running":
            # A cancelled/failed run must not leave a spinner in the UI.
            self._write(
                "UPDATE steps SET status=?, ended_at=? "
                "WHERE task_id=? AND status='running'",
                ("cancelled" if status == "cancelled" else "failed",
                 time.time(), task_id),
            )

    def get_task(self, task_id: str, events: int = 5000) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row:
                return None
            steps = conn.execute(
                "SELECT * FROM steps WHERE task_id=? ORDER BY idx",
                (task_id,),
            ).fetchall()
            evs = conn.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY seq LIMIT ?",
                (task_id, events),
            ).fetchall()
        task = dict(row)
        task["steps"] = [
            {**dict(s), "args": _loads(s["args_json"]), "success": bool(s["success"])}
            for s in steps
        ]
        task["events"] = [
            {**dict(e), "payload": _loads(e["payload"])} for e in evs
        ]
        return task

    def list_tasks(self, limit: int = 40, session_id: str = "") -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit or 40), 200))
        with self._lock, self._connect() as conn:
            if session_id:
                rows = conn.execute(
                    "SELECT t.*, (SELECT COUNT(*) FROM steps s WHERE s.task_id=t.id)"
                    " AS step_count FROM tasks t WHERE session_id=?"
                    " ORDER BY created_at ASC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT t.*, (SELECT COUNT(*) FROM steps s WHERE s.task_id=t.id)"
                    " AS step_count FROM tasks t"
                    " ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]

    def list_sessions(self, limit: int = 50) -> List[Dict[str, Any]]:
        """List distinct chat sessions with metadata for the dashboard."""
        limit = max(1, min(int(limit or 50), 200))
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT 
                    session_id,
                    MIN(created_at) AS created_at,
                    MAX(updated_at) AS updated_at,
                    COUNT(*) AS task_count,
                    (SELECT goal FROM tasks t2 WHERE t2.session_id = t.session_id ORDER BY t2.created_at ASC LIMIT 1) AS title,
                    (SELECT status FROM tasks t3 WHERE t3.session_id = t.session_id ORDER BY t3.created_at DESC LIMIT 1) AS status,
                    (SELECT id FROM tasks t4 WHERE t4.session_id = t.session_id ORDER BY t4.created_at DESC LIMIT 1) AS latest_task_id
                FROM tasks t
                WHERE session_id IS NOT NULL AND session_id != ''
                GROUP BY session_id
                ORDER BY MAX(updated_at) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_session(self, session_id: str) -> bool:
        """Delete all tasks, steps, and events belonging to a session."""
        if not session_id:
            return False
        with self._lock, self._connect() as conn:
            task_rows = conn.execute("SELECT id FROM tasks WHERE session_id=?", (session_id,)).fetchall()
            task_ids = [r["id"] for r in task_rows]
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                conn.execute(f"DELETE FROM events WHERE task_id IN ({placeholders})", task_ids)
                conn.execute(f"DELETE FROM steps WHERE task_id IN ({placeholders})", task_ids)
            conn.execute("DELETE FROM tasks WHERE session_id=?", (session_id,))
            conn.commit()
        return True

    # ------------------------------------------------------------------ steps
    def start_step(
        self,
        task_id: str,
        step_id: str,
        idx: int,
        skill: str,
        args: Optional[dict] = None,
    ) -> None:
        self._write(
            "INSERT INTO steps (id, task_id, idx, skill, args_json, status,"
            " started_at) VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET status='running',"
            " args_json=excluded.args_json, started_at=excluded.started_at",
            (
                step_id, task_id, int(idx), skill,
                json.dumps(args or {}, ensure_ascii=False, default=str),
                "running", time.time(),
            ),
        )

    def finish_step(
        self,
        step_id: str,
        success: bool,
        output: str = "",
        warning: str = "",
    ) -> None:
        self._write(
            "UPDATE steps SET status=?, output=?, warning=?, success=?,"
            " ended_at=? WHERE id=?",
            (
                "success" if success else "failed", str(output or "")[:20000],
                str(warning or "")[:2000], 1 if success else 0,
                time.time(), step_id,
            ),
        )

    # ------------------------------------------------------------------ events
    def append_event(
        self,
        task_id: str,
        seq: int,
        type: str,
        ts: float,
        payload: Optional[dict] = None,
        step_id: str = "",
    ) -> None:
        if not task_id:
            return
        self._write(
            "INSERT OR REPLACE INTO events (task_id, seq, step_id, type, ts, payload)"
            " VALUES (?,?,?,?,?,?)",
            (
                task_id, int(seq), step_id or "", type, float(ts),
                json.dumps(payload or {}, ensure_ascii=False, default=str),
            ),
        )
        # Mirror tool lifecycle into the steps table so replay can render
        # cards without re-deriving them from the event log.
        if step_id and type == "tool.started":
            self.start_step(
                task_id, step_id, int(seq),
                str((payload or {}).get("skill") or ""),
                (payload or {}).get("args") or {},
            )
        elif step_id and type == "tool.result":
            self.finish_step(
                step_id,
                bool((payload or {}).get("success")),
                str((payload or {}).get("output") or ""),
                str((payload or {}).get("warning") or ""),
            )

    def prune(self, days: int = 30) -> None:
        if days <= 0:
            return
        cutoff = time.time() - days * 86400
        try:
            with self._lock, self._connect() as conn:
                old = [r["id"] for r in conn.execute(
                    "SELECT id FROM tasks WHERE created_at < ?", (cutoff,))]
                for task_id in old:
                    conn.execute("DELETE FROM events WHERE task_id=?", (task_id,))
                    conn.execute("DELETE FROM steps WHERE task_id=?", (task_id,))
                    conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        except sqlite3.Error as exc:
            log.debug(f"task prune failed ({exc}).")


def _loads(raw: Any) -> Any:
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}


_default_store: Optional[TaskStore] = None


def get_store() -> TaskStore:
    global _default_store
    if _default_store is None or _default_store.path != db_path():
        _default_store = TaskStore()
    return _default_store
