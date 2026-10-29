"""Desktop API surface: task replay endpoints, CORS, WS actions."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from server import app
from winbrow.tasks.store import TaskStore

client = TestClient(app)


def _store(tmp_path, monkeypatch) -> TaskStore:
    monkeypatch.setenv("WINBROW_TASKS_DB", str(tmp_path / "tasks.db"))
    return TaskStore(tmp_path / "tasks.db")


def test_tasks_list_and_detail(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.start_task("t1", goal="do the thing", session_id="desktop")
    store.finish_task("t1", "completed", summary="done")
    store.append_event("t1", 1, "tool.started", 1.0, {"skill": "read_document"},
                       step_id="s1")
    store.append_event("t1", 2, "tool.result", 1.1,
                       {"success": True, "output": "ok"}, step_id="s1")

    listing = client.get("/api/tasks")
    assert listing.status_code == 200
    tasks = listing.json()["tasks"]
    assert tasks and tasks[0]["id"] == "t1"

    detail = client.get("/api/tasks/t1")
    assert detail.status_code == 200
    task = detail.json()
    assert task["goal"] == "do the thing"
    assert task["steps"][0]["skill"] == "read_document"
    assert task["steps"][0]["status"] == "success"


def test_unknown_task_is_404(tmp_path, monkeypatch):
    _store(tmp_path, monkeypatch)
    assert client.get("/api/tasks/nope").status_code == 404


def test_cors_allows_the_desktop_shell_only():
    allowed = client.get("/api/tasks", headers={"Origin": "null"})
    assert allowed.headers.get("access-control-allow-origin") == "null"
    blocked = client.get(
        "/api/tasks", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in blocked.headers


def test_ws_replay_and_subscribe(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.start_task("t1", goal="replay me")
    store.append_event("t1", 1, "task.started", 1.0, {"goal": "replay me"})

    with client.websocket_connect("/ws") as ws:
        ws.send_json({"action": "subscribe", "task_ids": ["t1"]})
        ack = ws.receive_json()
        assert ack["type"] == "subscribed"
        assert ack["task_ids"] == ["t1"]

        ws.send_json({"action": "replay", "task_id": "t1"})
        replay = ws.receive_json()
        assert replay["type"] == "replay"
        assert replay["data"]["id"] == "t1"
        assert replay["data"]["events"][0]["type"] == "task.started"


def test_voice_status_endpoint():
    response = client.get("/api/voice")
    assert response.status_code == 200
    assert "running" in response.json()


def test_run_agent_task_accepts_custom_endpoint(tmp_path, monkeypatch):
    """Regression: the WS path passed custom_endpoint and crashed the runner."""
    import asyncio

    import server

    monkeypatch.setenv("WINBROW_TASKS_DB", str(tmp_path / "tasks.db"))
    seen: dict = {}

    async def fake_execute(cmd, **kwargs):
        seen.update(kwargs)
        return {
            "execution": {"success": True, "output": "ok"},
            "total_elapsed_ms": 1.0,
        }

    monkeypatch.setattr(server.agent, "execute", fake_execute)
    result = asyncio.run(server._run_agent_task(
        "hi", task_id="task-custom", session_id="desktop",
        custom_endpoint="http://example.invalid/v1",
    ))
    assert seen.get("custom_endpoint") == "http://example.invalid/v1"
    assert result["execution"]["success"] is True
