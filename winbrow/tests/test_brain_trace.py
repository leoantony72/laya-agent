"""Brain trace events carry run ids, step indexes, and usage fields."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.brain import agent_loop
from winbrow.brain.agent_loop import AgentBrain
from winbrow.brain.model_gateway import GatewayResponse
from winbrow.memory.store import MemoryStore


class _Gateway:
    def __init__(self, responses):
        from types import SimpleNamespace
        self.providers = [SimpleNamespace(name="fake", model="fake-1")]
        self._responses = list(responses)

    async def chat(self, messages, **kwargs):
        item = self._responses.pop(0) if self._responses else {"kind": "done"}
        return GatewayResponse(
            content=json.dumps(item),
            provider="fake", model="fake-1",
            input_tokens=1000, output_tokens=50,
            cache_read_input_tokens=400,
            finish_reason="stop",
        )


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


class _ZeroUsageGateway(_Gateway):
    async def chat(self, messages, **kwargs):
        return GatewayResponse(
            content=json.dumps({"kind": "done", "summary": "ok"}),
            provider="fake", model="fake-1",
            input_tokens=0, output_tokens=0,
        )


def test_missing_usage_is_estimated_from_prompt_chars(monkeypatch):
    events = []
    monkeypatch.setattr(agent_loop, "log_event", events.append)
    brain = AgentBrain(_ZeroUsageGateway([]), _Registry(), MemoryStore(
        db_path=Path(tempfile.gettempdir()) / "wb_trace_zero.db",
        chroma_path=Path(tempfile.gettempdir()) / "wb_trace_zero_chroma",
    ), executor=None)
    asyncio.run(brain._ask_plan(
        [{"role": "user", "content": "x" * 400}], None, history=[]))
    plan = next(e for e in events if e["type"] == "brain_plan")
    assert plan["estimated"] is True
    assert plan["input_tokens"] == plan["prompt_chars"] // 4


def test_loop_emits_plan_and_step_events(monkeypatch):
    events = []
    monkeypatch.setattr(agent_loop, "log_event", events.append)
    store = MemoryStore(
        db_path=Path(tempfile.gettempdir()) / "wb_trace.db",
        chroma_path=Path(tempfile.gettempdir()) / "wb_trace_chroma",
    )
    brain = AgentBrain(
        _Gateway([
            {"kind": "action", "step": {
                "skill": "web_fetch", "args": {"url": "https://a"}}},
            {"kind": "done", "summary": "ok"},
        ]),
        _Registry(), store,
        executor=None,
    )

    async def fake_run(skill, args, why="", ctx=None):
        from winbrow.brain.agent_loop import BrainStepResult
        return BrainStepResult(skill, args, True, "page text")

    brain._run_step = fake_run
    result = asyncio.run(brain.handle("fetch it", _Ctx(), _Session()))

    plan_events = [e for e in events if e["type"] == "brain_plan"]
    step_events = [e for e in events if e["type"] == "brain_step"]
    assert plan_events and step_events
    assert plan_events[0]["run_id"] == result.run_id
    assert plan_events[0]["input_tokens"] == 1000
    assert plan_events[0]["cache_read"] == 400
    assert plan_events[0]["prompt_chars"] > 0
    assert step_events[0]["run_id"] == result.run_id
    assert step_events[0]["step_index"] == 1
    assert step_events[0]["output_chars"] == len("page text")
    assert "page text" not in json.dumps(events)
