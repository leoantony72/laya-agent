"""Task-scoped event bus shared by the sidecar and the desktop UI.

Every meaningful step of a run becomes a typed event:

    {"v": 2, "type": "tool.started", "task_id": "...", "step_id": "...",
     "seq": 12, "ts": 1712..., "payload": {...}}

``seq`` is monotonic per task so both windows can order, dedupe, and replay.
The bus persists to SQLite (queryable history) and fans out to WebSocket
subscribers. It never raises into the agent loop: a UI/DB failure must not
break a task.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
import uuid
from contextvars import ContextVar
from typing import Any, AsyncIterator, Dict, Optional, Set

from .tasks.store import get_store

log = logging.getLogger("winbrow.events")

PROTOCOL_VERSION = 2

_task_id: ContextVar[str] = ContextVar("winbrow_task_id", default="")
_step_id: ContextVar[str] = ContextVar("winbrow_step_id", default="")

# Event types the UI knows about. Kept here so the backend and the Electron
# app can be checked against one list.
TYPES = (
    "task.started", "task.completed", "task.failed", "task.cancelled",
    "thinking.delta", "thinking.summary",
    "tool.started", "tool.result",
    "answer.delta", "answer.final",
    "transcript.partial", "transcript.final",
    "agent.speaking",
    "confirm.required",
    "state.changed",
)


def new_task_id(prefix: str = "task") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def new_step_id(skill: str = "step") -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in str(skill or "step"))[:32]
    return f"{safe}_{uuid.uuid4().hex[:8]}"


class EventBus:
    def __init__(self) -> None:
        self._seq: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._subs: Set[asyncio.Queue] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._store = None

    # ------------------------------------------------------------------ wiring
    @property
    def store(self):
        if self._store is None:
            self._store = get_store()
        return self._store

    def bind_loop(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Remember the server loop so worker threads can fan out safely."""
        try:
            self._loop = loop or asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def reset_task(self, task_id: str) -> None:
        with self._lock:
            self._seq[task_id] = 0

    # ------------------------------------------------------------------ publish
    def emit(
        self,
        type: str,
        payload: Optional[dict] = None,
        *,
        task_id: Optional[str] = None,
        step_id: Optional[str] = None,
    ) -> dict:
        """Create, persist, and fan out one event. Returns the envelope."""
        type = str(type or "event")
        task = str(task_id if task_id is not None else _task_id.get() or "")
        step = str(step_id if step_id is not None else _step_id.get() or "")
        with self._lock:
            seq = self._seq.get(task, 0) + 1
            self._seq[task] = seq
        envelope = {
            "v": PROTOCOL_VERSION,
            "type": type,
            "task_id": task,
            "step_id": step,
            "seq": seq,
            "ts": time.time(),
            "payload": payload or {},
        }
        if task:
            try:
                self.store.append_event(
                    task, seq, type, envelope["ts"],
                    envelope["payload"], step_id=step,
                )
            except Exception as exc:
                log.debug(f"event persist failed (non-fatal): {exc}")
        self._fanout(envelope)
        return envelope

    def _fanout(self, envelope: dict) -> None:
        subs = list(self._subs)
        if not subs:
            return

        def _put() -> None:
            for queue in subs:
                try:
                    queue.put_nowait(envelope)
                except asyncio.QueueFull:
                    pass
                except Exception:
                    pass

        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and running is (loop or running):
            _put()  # already on the server loop
            return
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(_put)
                return
            except RuntimeError:
                pass
        _put()

    # ------------------------------------------------------------------ subscribe
    def subscribe(self, maxsize: int = 2000) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subs.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subs.discard(queue)

    @contextlib.asynccontextmanager
    async def subscription(self) -> AsyncIterator[asyncio.Queue]:
        queue = self.subscribe()
        try:
            yield queue
        finally:
            self.unsubscribe(queue)

    # ------------------------------------------------------------------ helpers
    def stream_answer(self, text: str, *, task_id: Optional[str] = None,
                      step_id: Optional[str] = None, size: int = 32) -> None:
        """Emit the final answer as small deltas, then a terminal event."""
        text = str(text or "")
        for start in range(0, len(text), max(1, int(size))):
            self.emit(
                "answer.delta", {"text": text[start:start + size]},
                task_id=task_id, step_id=step_id,
            )
        self.emit(
            "answer.final", {"text": text},
            task_id=task_id, step_id=step_id,
        )


_bus = EventBus()


def bus() -> EventBus:
    return _bus


def bind_loop(loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    _bus.bind_loop(loop)


def emit(type: str, payload: Optional[dict] = None, **kwargs: Any) -> dict:
    return _bus.emit(type, payload, **kwargs)


@contextlib.contextmanager
def task_scope(task_id: str):
    token = _task_id.set(str(task_id or ""))
    try:
        yield
    finally:
        _task_id.reset(token)


@contextlib.contextmanager
def step_scope(step_id: str):
    token = _step_id.set(str(step_id or ""))
    try:
        yield
    finally:
        _step_id.reset(token)


def current_task_id() -> str:
    return _task_id.get()


def current_step_id() -> str:
    return _step_id.get()
