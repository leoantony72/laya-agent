"""
Semantic Memory - SQLite + ChromaDB
====================================
Persistent memory of user interactions, files, apps, and context.
Enables follow-ups like "send that file to John" by remembering what "that file" refers to.
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
from typing import Any, Optional, List, Dict, Generator

log = logging.getLogger("winbrow.memory")

# Try to import ChromaDB, fall back to SQLite-only if not available
try:
    import chromadb
    from chromadb.config import Settings
    CHROMADB_AVAILABLE = True
except ImportError:
    CHROMADB_AVAILABLE = False
    log.warning("ChromaDB not available. Install with: pip install chromadb. Using SQLite-only mode.")


DB_PATH = Path(__file__).parent.parent / "data" / "memory.db"
CHROMA_PATH = Path(__file__).parent.parent / "data" / "chroma"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
CHROMA_PATH.mkdir(parents=True, exist_ok=True)


@dataclass
class MemoryEntry:
    """A single memory entry."""
    id: str
    timestamp: float
    kind: str  # "file_open", "folder_open", "app_launch", "web_search", "command", "interaction"
    label: str  # Human-readable description
    path: str = ""  # File/folder path or URL
    app: str = ""  # Application name
    content: str = ""  # Extracted text content (for files/pages)
    metadata: dict = field(default_factory=dict)
    embedding: list[float] = field(default_factory=list)  # For vector search


class SemanticMemory:
    """Persistent semantic memory with SQLite + optional ChromaDB vector search."""
    
    def __init__(self, db_path: Optional[Path] = None, chroma_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self.chroma_path = chroma_path or CHROMA_PATH
        
        self._init_db()
        self._init_chroma()
    
    def _init_db(self) -> None:
        """Initialize SQLite database."""
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
                    metadata TEXT,
                    embedding TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_timestamp ON memories(timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_kind ON memories(kind)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_path ON memories(path)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_app ON memories(app)
            """)
    
    def _init_chroma(self) -> None:
        """Initialize ChromaDB for vector search."""
        if not CHROMADB_AVAILABLE:
            self.chroma_client = None
            self.collection = None
            return
        
        try:
            self.chroma_client = chromadb.PersistentClient(
                path=str(self.chroma_path),
                settings=Settings(anonymized_telemetry=False)
            )
            self.collection = self.chroma_client.get_or_create_collection(
                name="winbrow_memory",
                metadata={"hnsw:space": "cosine"}
            )
        except Exception as e:
            log.warning(f"ChromaDB init failed: {e}. Vector search disabled.")
            self.chroma_client = None
            self.collection = None
    
    @contextmanager
    def _db_conn(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager for database connections."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def record(
        self,
        kind: str,
        label: str,
        path: str = "",
        app: str = "",
        content: str = "",
        metadata: Optional[dict] = None,
        embedding: Optional[list[float]] = None
    ) -> str:
        """Record a new memory entry."""
        entry_id = str(uuid.uuid4())
        timestamp = time.time()
        
        entry = MemoryEntry(
            id=entry_id,
            timestamp=timestamp,
            kind=kind,
            label=label,
            path=path,
            app=app,
            content=content,
            metadata=metadata or {},
            embedding=embedding or [],
        )
        
        with self._db_conn() as conn:
            conn.execute("""
                INSERT INTO memories (id, timestamp, kind, label, path, app, content, metadata, embedding)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry.id,
                entry.timestamp,
                entry.kind,
                entry.label,
                entry.path,
                entry.app,
                entry.content,
                json.dumps(entry.metadata),
                json.dumps(entry.embedding),
            ))
        
        # Add to ChromaDB if available
        if self.collection and entry.content:
            try:
                self.collection.add(
                    ids=[entry.id],
                    documents=[entry.content[:8000]],  # Limit length
                    metadatas=[{
                        "kind": entry.kind,
                        "label": entry.label,
                        "path": entry.path,
                        "app": entry.app,
                        "timestamp": entry.timestamp,
                    }],
                    embeddings=[entry.embedding] if entry.embedding else None,
                )
            except Exception as e:
                log.debug(f"ChromaDB add failed: {e}")
        
        return entry_id
    
    def record_file_open(self, path: str, label: str = "", content: str = "") -> str:
        """Record a file open event."""
        path_obj = Path(path)
        return self.record(
            kind="file_open",
            label=label or path_obj.name,
            path=str(path_obj.absolute()),
            content=content,
            metadata={"extension": path_obj.suffix.lower()},
        )
    
    def record_folder_open(self, path: str, label: str = "") -> str:
        """Record a folder open event."""
        path_obj = Path(path)
        return self.record(
            kind="folder_open",
            label=label or path_obj.name,
            path=str(path_obj.absolute()),
        )
    
    def record_app_launch(self, app: str, title: str = "") -> str:
        """Record an app launch event."""
        return self.record(
            kind="app_launch",
            label=title or app,
            app=app,
        )
    
    def record_web_search(self, query: str, url: str = "", title: str = "") -> str:
        """Record a web search event."""
        return self.record(
            kind="web_search",
            label=title or query,
            path=url,
            content=query,
            metadata={"query": query, "url": url},
        )
    
    def record_command(self, command: str, result: str = "") -> str:
        """Record a command execution."""
        return self.record(
            kind="command",
            label=command,
            content=result,
        )
    
    def get_recent(self, limit: int = 20, kind: Optional[str] = None) -> List[MemoryEntry]:
        """Get recent memory entries."""
        with self._db_conn() as conn:
            if kind:
                rows = conn.execute(
                    "SELECT * FROM memories WHERE kind = ? ORDER BY timestamp DESC LIMIT ?",
                    (kind, limit)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM memories ORDER BY timestamp DESC LIMIT ?",
                    (limit,)
                ).fetchall()
        
        return [self._row_to_entry(row) for row in rows]
    
    def get_recent_files(self, limit: int = 10) -> List[MemoryEntry]:
        """Get recently opened files."""
        return self.get_recent(limit=limit, kind="file_open")
    
    def get_recent_folders(self, limit: int = 10) -> List[MemoryEntry]:
        """Get recently opened folders."""
        return self.get_recent(limit=limit, kind="folder_open")
    
    def get_recent_apps(self, limit: int = 10) -> List[MemoryEntry]:
        """Get recently launched apps."""
        return self.get_recent(limit=limit, kind="app_launch")
    
    def search_by_content(self, query: str, limit: int = 10) -> List[MemoryEntry]:
        """Search memories by content using vector similarity (if ChromaDB available)."""
        if not self.collection:
            # Fallback to SQLite LIKE search
            with self._db_conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM memories WHERE content LIKE ? OR label LIKE ? ORDER BY timestamp DESC LIMIT ?",
                    (f"%{query}%", f"%{query}%", limit)
                ).fetchall()
            return [self._row_to_entry(row) for row in rows]
        
        try:
            results = self.collection.query(
                query_texts=[query],
                n_results=limit,
            )
            
            ids = results.get("ids", [[]])[0]
            if not ids:
                return []
            
            placeholders = ",".join("?" * len(ids))
            with self._db_conn() as conn:
                rows = conn.execute(
                    f"SELECT * FROM memories WHERE id IN ({','.join('?' * len(ids))})",
                    ids
                ).fetchall()
            
            # Sort by the order returned by ChromaDB
            id_to_row = {self._row_to_entry(row).id: self._row_to_entry(row) for row in rows}
            return [id_to_row[id] for id in ids if id in id_to_row]
        except Exception as e:
            log.warning(f"Vector search failed: {e}")
            return []
    
    def find_file_by_name(self, name: str) -> Optional[MemoryEntry]:
        """Find a recently opened file by name."""
        with self._db_conn() as conn:
            row = conn.execute(
                "SELECT * FROM memories WHERE kind = 'file_open' AND (label LIKE ? OR path LIKE ?) ORDER BY timestamp DESC LIMIT 1",
                (f"%{name}%", f"%{name}%")
            ).fetchone()
            if row:
                return self._row_to_entry(row)
        return None
    
    def get_context_summary(self, max_items: int = 10) -> str:
        """Get a human-readable summary of recent context for LLM prompts."""
        recent = self.get_recent(limit=max_items)
        if not recent:
            return "No recent activity."
        
        lines = []
        for entry in recent:
            time_str = datetime.fromtimestamp(entry.timestamp).strftime("%H:%M")
            if entry.kind == "file_open":
                lines.append(f"  {time_str} - Opened file: {entry.label} ({entry.path})")
            elif entry.kind == "folder_open":
                lines.append(f"  {time_str} - Opened folder: {entry.label} ({entry.path})")
            elif entry.kind == "app_launch":
                lines.append(f"  {time_str} - Launched app: {entry.label}")
            elif entry.kind == "web_search":
                lines.append(f"  {time_str} - Searched web: {entry.label}")
            elif entry.kind == "command":
                lines.append(f"  {time_str} - Command: {entry.label}")
        
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
            embedding=json.loads(row["embedding"]) if row["embedding"] else [],
        )


# Global instance
_memory_instance: Optional[SemanticMemory] = None


def get_memory() -> SemanticMemory:
    """Get global memory instance (singleton)."""
    global _memory_instance
    if _memory_instance is None:
        _memory_instance = SemanticMemory()
    return _memory_instance


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)
    
    async def test():
        mem = get_memory()
        
        # Record some test entries
        mem.record_file_open("C:/Users/test/document.pdf", "Quarterly Report", "Q3 revenue up 15%")
        mem.record_folder_open("C:/Users/Downloads")
        mem.record_app_launch("Code", "Visual Studio Code")
        mem.record_web_search("Python asyncio tutorial", "https://google.com/search?q=asyncio", "Python asyncio tutorial - Google Search")
        
        # Get recent
        print("Recent activity:")
        for entry in mem.get_recent(10):
            print(f"  {entry.kind}: {entry.label} ({entry.path})")
        
        # Search
        print("\nSearch for 'report':")
        results = mem.search_by_content("report")
        for r in results:
            print(f"  {r.kind}: {r.label}")
    
    asyncio.run(test())