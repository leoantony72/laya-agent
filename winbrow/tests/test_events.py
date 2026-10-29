"""Event bus + SQLite task trace: ordering, persistence, and replay."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow import events
from winbrow.tasks.store import TaskStore


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("WINBROW_TASKS_DB", str(tmp_path / "tasks.db"))
    bus = events.EventBus()
    bus._store = TaskStore(tmp_path / "tasks.db")
    return bus


def test_events_are_sequenced_per_task(tmp_path, monkeypatch):
    bus = _fresh(tmp_path, monkeypatch)
    bus.reset_task("t1")
    first = bus.emit("task.started", {"goal": "x"}, task_id="t1")
    second = bus.emit("tool.started", {}, task_id="t1")
    other = bus.emit("task.started", {}, task_id="t2")
    assert first["seq"] == 1
    assert second["seq"] == 2
    assert other["seq"] == 1
    assert second["v"] == 2
    assert second["type"] == "tool.started"


def test_events_persist_and_replay_in_order(tmp_path, monkeypatch):
    bus = _fresh(tmp_path, monkeypatch)
    store = bus.store
    store.start_task("t1", goal="build", session_id="s")
    bus.reset_task("t1")
    bus.emit("task.started", {"goal": "build"}, task_id="t1")
    with events.task_scope("t1"):
        bus.emit("thinking.summary", {"summary": "planning"})
        with events.step_scope("s1"):
            bus.emit("tool.started", {"skill": "read_document"})
            bus.emit("tool.result", {"success": True})
    store.finish_task("t1", "completed", summary="ok")

    task = store.get_task("t1")
    assert task is not None
    assert task["status"] == "completed"
    assert [e["type"] for e in task["events"]] == [
        "task.started", "thinking.summary", "tool.started", "tool.result",
    ]
    assert [e["seq"] for e in task["events"]] == [1, 2, 3, 4]
    assert task["events"][2]["step_id"] == "s1"


def test_answer_stream_emits_deltas_then_final(tmp_path, monkeypatch):
    bus = _fresh(tmp_path, monkeypatch)
    store = bus.store
    store.start_task("t1", goal="hi")
    bus.reset_task("t1")
    bus.stream_answer("hello world", task_id="t1", size=4)
    task = store.get_task("t1")
    types = [e["type"] for e in task["events"]]
    assert types[-1] == "answer.final"
    assert types.count("answer.delta") == 3
    joined = "".join(
        e["payload"]["text"] for e in task["events"]
        if e["type"] == "answer.delta"
    )
    assert joined == "hello world"


def test_subscriber_receives_events(tmp_path, monkeypatch):
    import asyncio

    bus = _fresh(tmp_path, monkeypatch)
    bus.reset_task("t1")

    async def scenario():
        bus.bind_loop()
        queue = bus.subscribe()
        bus.emit("task.started", {}, task_id="t1")
        envelope = await asyncio.wait_for(queue.get(), timeout=2)
        bus.unsubscribe(queue)
        return envelope

    envelope = asyncio.run(scenario())
    assert envelope["type"] == "task.started"
    assert envelope["task_id"] == "t1"


def test_bus_never_raises_on_bad_payload(tmp_path, monkeypatch):
    bus = _fresh(tmp_path, monkeypatch)
    bus.reset_task("t")
    envelope = bus.emit("weird", {"obj": object()}, task_id="t")
    assert envelope["type"] == "weird"


def test_list_tasks_newest_first(tmp_path, monkeypatch):
    store = TaskStore(tmp_path / "tasks.db")
    store.start_task("a", goal="first")
    store.start_task("b", goal="second")
    tasks = store.list_tasks(limit=10)
    assert [t["id"] for t in tasks] == ["b", "a"]


def test_chat_reply_is_not_duplicated_as_thinking(tmp_path, monkeypatch):
    """A chat answer must appear once: as the answer, not also as thinking."""
    from winbrow.brain import agent_loop

    monkeypatch.setenv("WINBROW_TASKS_DB", str(tmp_path / "tasks.db"))
    bus = events.EventBus()
    bus._store = TaskStore(tmp_path / "tasks.db")
    bus.store.start_task("t1", goal="hi")
    bus.reset_task("t1")
    monkeypatch.setattr(agent_loop, "event_bus", lambda: bus)

    with events.task_scope("t1"):
        agent_loop._emit_thinking(
            {"kind": "chat", "response": "Hi! How can I help you today?"},
            "chat",
        )
        agent_loop._emit_thinking(
            {"kind": "action", "why": "Read the notes before editing.",
             "steps": [{"skill": "read_document", "args": {}}]},
            "action",
        )

    task = bus.store.get_task("t1")
    thinking = [e for e in task["events"] if e["type"] == "thinking.summary"]
    assert len(thinking) == 1
    assert thinking[0]["payload"]["summary"] == "Read the notes before editing."
