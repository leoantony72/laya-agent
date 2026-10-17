"""
winbrow.memory — persistent memory package.

Modules:
  store    — MemoryStore: episodic + semantic facts (SQLite FTS5 + ChromaDB)
  session  — SessionMemory: per-conversation working memory (entities, turns)

Backwards-compat: the old `winbrow/memory.py` module names are re-exported.
"""

from .store import (
    MemoryEntry,
    MemoryStore,
    get_store,
)
from .session import (
    Entity,
    SessionMemory,
    Turn,
    get_session,
)
from .skills import (
    SkillMemory,
    SkillRecord,
    SkillStep,
    get_skills,
    reset_skills,
)

# Old module API compat (voice.py imports these names from winbrow.memory)
SemanticMemory = MemoryStore


def get_memory() -> MemoryStore:
    return get_store()


__all__ = [
    "MemoryEntry",
    "MemoryStore",
    "get_store",
    "get_memory",
    "get_session",
    "SessionMemory",
    "Entity",
    "Turn",
    "SemanticMemory",
    "SkillMemory",
    "SkillRecord",
    "SkillStep",
    "get_skills",
    "reset_skills",
]
