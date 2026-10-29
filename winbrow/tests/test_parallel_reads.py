"""Flag-gated parallel read-only batches preserve plan order and serialise mixes."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.brain.agent_loop import AgentBrain, BrainStepResult
from winbrow.brain.model_gateway import GatewayResponse
from winbrow.memory.store import MemoryStore


class _Gateway:
    def __init__(self, plans):
        from types import SimpleNamespace
        self.providers = [SimpleNamespace(name="fake", model="fake-1")]
        self._plans = list(plans)

    async def chat(self, messages, **kwargs):
        plan = self._plans.pop(0) if self._plans else {"kind": "done"}
        return GatewayResponse(
            content=json.dumps(plan), provider="fake", model="fake-1")


class _Registry:
    def available(self, ctx):
        return []

    def all_tools(self):
        return []

    def get(self, name):
        return object()


class _Ctx:
    def to_dict(self):
        return {"active_app": "Desktop", "running_apps": []}


class _Session:
    def context_block(self):
        return "none"


def _brain(plans):
    store = MemoryStore(
        db_path=Path(tempfile.gettempdir()) / "wb_parallel.db",
        chroma_path=Path(tempfile.gettempdir()) / "wb_parallel_chroma",
    )
    return AgentBrain(_Gateway(plans), _Registry(), store, executor=None)


def _action(*steps):
    return {"kind": "action", "steps": list(steps)}


def test_read_only_batch_runs_concurrently_and_keeps_order(monkeypatch):
    monkeypatch.setenv("WINBROW_PARALLEL_READS", "1")
    brain = _brain([
        _action(
            {"skill": "web_fetch", "args": {"url": "https://a"}},
            {"skill": "read_document", "args": {"path": "a.txt"}},
        ),
        {"kind": "done", "summary": "ok"},
    ])
    active = {"now": 0, "max": 0}

    async def fake_run(skill, args, why="", ctx=None):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.05)
        active["now"] -= 1
        return BrainStepResult(skill, args, True, f"ok {skill}")

    brain._run_step = fake_run
    result = asyncio.run(brain.handle("read both", _Ctx(), _Session()))
    assert active["max"] == 2
    assert [step.skill for step in result.steps] == [
        "web_fetch", "read_document"]


def test_mixed_read_write_is_serialised(monkeypatch):
    monkeypatch.setenv("WINBROW_PARALLEL_READS", "1")
    brain = _brain([
        _action(
            {"skill": "web_fetch", "args": {"url": "https://a"}},
            {"skill": "create_text_file", "args": {"content": "x"}},
        ),
        {"kind": "done", "summary": "ok"},
    ])
    active = {"now": 0, "max": 0}

    async def fake_run(skill, args, why="", ctx=None):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.03)
        active["now"] -= 1
        return BrainStepResult(skill, args, True, f"ok {skill}")

    brain._run_step = fake_run
    asyncio.run(brain.handle("read then write", _Ctx(), _Session()))
    assert active["max"] == 1


def test_failed_batch_still_records_remaining_reads(monkeypatch):
    monkeypatch.setenv("WINBROW_PARALLEL_READS", "1")
    brain = _brain([
        _action(
            {"skill": "web_fetch", "args": {"url": "https://a"}},
            {"skill": "read_document", "args": {"path": "a.txt"}},
        ),
        {"kind": "done", "summary": "ok"},
    ])

    async def fake_run(skill, args, why="", ctx=None):
        if skill == "web_fetch":
            return BrainStepResult(skill, args, False, "fetch failed")
        return BrainStepResult(skill, args, True, "document text")

    brain._run_step = fake_run
    result = asyncio.run(brain.handle("read both", _Ctx(), _Session()))
    assert [step.skill for step in result.steps] == [
        "web_fetch", "read_document"]
    assert result.steps[0].success is False
    assert result.steps[1].success is True


def test_duplicate_reads_are_not_batched(monkeypatch):
    monkeypatch.setenv("WINBROW_PARALLEL_READS", "1")
    brain = _brain([
        _action(
            {"skill": "web_fetch", "args": {"url": "https://a"}},
            {"skill": "web_fetch", "args": {"url": "https://a"}},
        ),
        {"kind": "done", "summary": "ok"},
    ])
    calls = []

    async def fake_run(skill, args, why="", ctx=None):
        calls.append((skill, args))
        return BrainStepResult(skill, args, True, "ok")

    brain._run_step = fake_run
    asyncio.run(brain.handle("read twice", _Ctx(), _Session()))
    assert len(calls) == 1
