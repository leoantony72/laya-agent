"""
Memory Store — Episodic + Semantic (SQLite FTS5 + optional ChromaDB)
====================================================================
Persistent record of everything the agent did and learned:
  • episodic  — every command: utterance, route, tool, args, outcome
  • facts     — user preferences / environment facts ("exports folder is D:\\Exports")
  • corrections — learned utterance normalizations ("crome" → "chrome")

Replaces the old never-imported winbrow/memory.py. ChromaDB is optional;
SQLite FTS5 carries full-text search when it is absent.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

log = logging.getLogger("winbrow.memory.store")

try:
    import chromadb
    from chromadb.config import Settings as ChromaSettings
    CHROMADB_AVAILABLE = True
except ImportError:
    CHROMADB_AVAILABLE = False
    log.info("ChromaDB not installed — episodic search uses SQLite FTS only.")

DB_PATH = Path(__file__).parent.parent.parent / "data" / "memory.db"
CHROMA_PATH = Path(__file__).parent.parent.parent / "data" / "chroma"

# Kinds
K_FILE_OPEN = "file_open"
K_FOLDER_OPEN = "folder_open"
K_APP_LAUNCH = "app_launch"
K_WEB_SEARCH = "web_search"
K_COMMAND = "command"
K_FACT = "fact"
K_CORRECTION = "correction"


@dataclass
class MemoryEntry:
    """A single memory entry."""
    id: str
    timestamp: float
    kind: str
    label: str
    path: str = ""
    app: str = ""
    content: str = ""
    metadata: dict = field(default_factory=dict)


class MemoryStore:
    """Persistent episodic + semantic memory. All writes are best-effort."""

    def __init__(self, db_path: Optional[Path] = None, chroma_path: Optional[Path] = None):
        self.db_path = Path(db_path or DB_PATH)
        self.chroma_path = Path(chroma_path or CHROMA_PATH)
        self._fts = False
        self._init_db()
        self._init_chroma()

    # ------------------------------------------------------------------ setup
    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._db_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    timestamp REAL NOT NULL,
                    kind TEXT NOT NULL,
                    label TEXT NOT NULL,
                    path TEXT,
                    app TEXT,
                    content TEXT,
                    metadata TEXT
                )
            """)
            for col in ("timestamp", "kind", "path", "app"):
                conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{col} ON memories({col})")
            try:
                conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                        label, content, content='memories', content_rowid='rowid'
                    )
                """)
                self._fts = True
            except sqlite3.OperationalError as e:
                log.info(f"FTS5 unavailable ({e}); falling back to LIKE search.")
        if self._fts:
            with self._db_conn() as conn:
                try:
                    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('rebuild')")
                except sqlite3.OperationalError:
                    self._fts = False

    def _init_chroma(self) -> None:
        self.collection = None
        if not CHROMADB_AVAILABLE:
            return
        try:
            self.chroma_path.mkdir(parents=True, exist_ok=True)
            client = chromadb.PersistentClient(
                path=str(self.chroma_path),
                settings=ChromaSettings(anonymized_telemetry=False),
            )
            self.collection = client.get_or_create_collection(
                name="winbrow_memory",
                metadata={"hnsw:space": "cosine"},
            )
        except Exception as e:
            log.info(f"ChromaDB init failed ({e}); vector search disabled.")
            self.collection = None

    @contextmanager
    def _db_conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ write
    def record(
        self,
        kind: str,
        label: str,
        path: str = "",
        app: str = "",
        content: str = "",
        metadata: Optional[dict] = None,
    ) -> str:
        """Record a memory entry. Never raises."""
        entry_id = str(uuid.uuid4())
        ts = time.time()
        try:
            with self._db_conn() as conn:
                conn.execute(
                    """INSERT INTO memories (id, timestamp, kind, label, path, app, content, metadata)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (entry_id, ts, kind, label, path, app, content, json.dumps(metadata or {})),
                )
                if self._fts:
                    rowid = conn.execute(
                        "SELECT rowid FROM memories WHERE id = ?", (entry_id,)
                    ).fetchone()
                    if rowid:
                        conn.execute(
                            "INSERT INTO memories_fts(rowid, label, content) VALUES (?, ?, ?)",
                            (rowid["rowid"], label, content),
                        )
            if self.collection and (content or label):
                try:
                    self.collection.add(
                        ids=[entry_id],
                        documents=[(content or label)[:8000]],
                        metadatas=[{
                            "kind": kind, "label": label[:200], "path": path,
                            "app": app, "timestamp": ts,
                        }],
                    )
                except Exception as e:
                    log.debug(f"Chroma add failed: {e}")
        except Exception as e:
            log.debug(f"Memory record failed (non-fatal): {e}")
        return entry_id

    def record_command(self, utterance: str, route: dict, execution: dict,
                       elapsed_ms: float = 0.0) -> str:
        """Record a full command execution (episodic)."""
        tool = route.get("tool_name") or route.get("kind") or "unknown"
        success = bool(execution.get("success", False))
        output = str(execution.get("output", ""))[:500]
        return self.record(
            kind=K_COMMAND,
            label=utterance,
            app=str(route.get("args", {}).get("app_name", "")) if isinstance(route.get("args"), dict) else "",
            content=json.dumps({
                "utterance": utterance,
                "tool": tool,
                "tier": route.get("tier", ""),
                "args": route.get("args", {}),
                "success": success,
                "output": output,
                "elapsed_ms": elapsed_ms,
            }, ensure_ascii=False),
            metadata={"tool": tool, "success": success, "tier": str(route.get("tier", ""))},
        )

    def record_file_open(self, path: str, label: str = "") -> str:
        p = Path(path)
        return self.record(kind=K_FILE_OPEN, label=label or p.name, path=str(p.absolute()),
                           metadata={"extension": p.suffix.lower()})

    def record_folder_open(self, path: str, label: str = "") -> str:
        p = Path(path)
        return self.record(kind=K_FOLDER_OPEN, label=label or p.name, path=str(p.absolute()))

    def record_app_launch(self, app: str, title: str = "") -> str:
        return self.record(kind=K_APP_LAUNCH, label=title or app, app=app)

    def record_web_search(self, query: str, url: str = "", title: str = "") -> str:
        return self.record(kind=K_WEB_SEARCH, label=title or query, path=url, content=query)

    def add_fact(self, fact: str, source: str = "user") -> str:
        """Store a user/environment fact for future prompts."""
        return self.record(kind=K_FACT, label=fact[:200], content=fact, metadata={"source": source})

    def add_correction(self, wrong: str, right: str) -> str:
        """Learn an utterance normalization ('crome' → 'chrome')."""
        return self.record(kind=K_CORRECTION, label=wrong.lower()[:200], content=right[:200])

    # ------------------------------------------------------------------ read
    def get_recent(self, limit: int = 20, kind: Optional[str] = None) -> List[MemoryEntry]:
        with self._db_conn() as conn:
            if kind:
                rows = conn.execute(
                    "SELECT * FROM memories WHERE kind = ? ORDER BY timestamp DESC LIMIT ?",
                    (kind, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM memories ORDER BY timestamp DESC LIMIT ?", (limit,)
                ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def get_recent_files(self, limit: int = 10) -> List[MemoryEntry]:
        return self.get_recent(limit=limit, kind=K_FILE_OPEN)

    def get_facts(self, limit: int = 50) -> List[MemoryEntry]:
        return self.get_recent(limit=limit, kind=K_FACT)

    def get_correction(self, token: str) -> Optional[str]:
        """Look up a learned normalization for a token/phrase."""
        t = (token or "").strip().lower()
        if not t:
            return None
        with self._db_conn() as conn:
            row = conn.execute(
                "SELECT content FROM memories WHERE kind = ? AND label = ? ORDER BY timestamp DESC LIMIT 1",
                (K_CORRECTION, t),
            ).fetchone()
        return row["content"] if row else None

    def search_episodic(self, query: str, limit: int = 8) -> List[MemoryEntry]:
        """Hybrid search: FTS5 (or LIKE) + recency. Vector search when Chroma is up."""
        if not query:
            return []
        results: List[MemoryEntry] = []
        if self.collection:
            try:
                res = self.collection.query(query_texts=[query], n_results=limit)
                ids = res.get("ids", [[]])[0]
                if ids:
                    ph = ",".join("?" * len(ids))
                    with self._db_conn() as conn:
                        rows = conn.execute(
                            f"SELECT * FROM memories WHERE id IN ({ph})", ids
                        ).fetchall()
                    by_id = {r["id"]: self._row_to_entry(r) for r in rows}
                    results = [by_id[i] for i in ids if i in by_id]
            except Exception as e:
                log.debug(f"Vector search failed: {e}")
        if not results:
            with self._db_conn() as conn:
                if self._fts:
                    try:
                        fts_query = " OR ".join(
                            f'"{w}"' for w in query.split() if len(w) > 1
                        )
                        rows = conn.execute(
                            """SELECT m.* FROM memories m
                               JOIN memories_fts f ON m.rowid = f.rowid
                               WHERE memories_fts MATCH ?
                               ORDER BY m.timestamp DESC LIMIT ?""",
                            (fts_query, limit),
                        ).fetchall()
                    except sqlite3.OperationalError:
                        rows = []
                else:
                    rows = []
                if not rows:
                    like = f"%{query}%"
                    rows = conn.execute(
                        """SELECT * FROM memories
                           WHERE label LIKE ? OR content LIKE ?
                           ORDER BY timestamp DESC LIMIT ?""",
                        (like, like, limit),
                    ).fetchall()
            results = [self._row_to_entry(r) for r in rows]
        return results

    def find_file_by_name(self, name: str) -> Optional[MemoryEntry]:
        with self._db_conn() as conn:
            row = conn.execute(
                """SELECT * FROM memories WHERE kind = ? AND (label LIKE ? OR path LIKE ?)
                   ORDER BY timestamp DESC LIMIT 1""",
                (K_FILE_OPEN, f"%{name}%", f"%{name}%"),
            ).fetchone()
        return self._row_to_entry(row) if row else None

    def get_context_summary(self, max_items: int = 10) -> str:
        """Human-readable recent-activity summary for LLM prompts."""
        recent = self.get_recent(limit=max_items)
        if not recent:
            return "No recent activity."
        lines = []
        for e in recent:
            t = datetime.fromtimestamp(e.timestamp).strftime("%m-%d %H:%M")
            if e.kind == K_COMMAND:
                lines.append(f"  {t} — command: {e.label}")
            elif e.kind == K_FILE_OPEN:
                lines.append(f"  {t} — opened file: {e.label} ({e.path})")
            elif e.kind == K_FOLDER_OPEN:
                lines.append(f"  {t} — opened folder: {e.label} ({e.path})")
            elif e.kind == K_APP_LAUNCH:
                lines.append(f"  {t} — launched app: {e.label}")
            elif e.kind == K_WEB_SEARCH:
                lines.append(f"  {t} — web search: {e.label}")
            elif e.kind == K_FACT:
                lines.append(f"  {t} — fact: {e.label}")
        return "\n".join(lines)

    def _row_to_entry(self, row: sqlite3.Row) -> MemoryEntry:
        return MemoryEntry(
            id=row["id"],
            timestamp=row["timestamp"],
            kind=row["kind"],
            label=row["label"],
            path=row["path"] or "",
            app=row["app"] or "",
            content=row["content"] or "",
            metadata=json.loads(row["metadata"]) if row["metadata"] else {},
        )


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_store_instance: Optional[MemoryStore] = None


def get_store() -> MemoryStore:
    global _store_instance
    if _store_instance is None:
        _store_instance = MemoryStore()
    return _store_instance
