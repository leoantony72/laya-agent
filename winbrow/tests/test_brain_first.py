"""Every user request must reach the brain before any MCP/router fast path."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.agent import WinBrowAgent, _brain_first_enabled
from winbrow.brain.agent_loop import BrainResult, BrainStepResult


def _agent_with_fake_brain(handler):
    agent = WinBrowAgent()
    agent.gateway = SimpleNamespace(available=True)
    agent.get_context = lambda: SimpleNamespace(to_dict=lambda: {})
    agent.brain.handle = handler
    return agent


def test_brain_first_defaults_on(monkeypatch):
    monkeypatch.delenv("WINBROW_BRAIN_FIRST", raising=False)
    assert _brain_first_enabled() is True
    monkeypatch.setenv("WINBROW_BRAIN_FIRST", "0")
    assert _brain_first_enabled() is False


def test_mcp_shortcut_is_not_used_when_brain_is_available():
    calls = []

    async def fake_handle(utterance, ctx, session, emit=None, max_steps=None):
        calls.append(utterance)
        return BrainResult(
            kind="action",
            success=True,
            response="brain handled the request",
            steps=[BrainStepResult("mcp_wikipedia_search_wikipedia", {}, True, "ok")],
        )

    agent = _agent_with_fake_brain(fake_handle)
    out = asyncio.run(agent.execute(
        "search wikipedia for alan turing", session_id="brain-first-mcp",
    ))

    assert out["route"]["tier"] == "brain_first"
    assert out["execution"]["output"] == "brain handled the request"
    assert calls == ["search wikipedia for alan turing"]
    assert out["route"]["tool_name"] == "brain"


def test_brain_failure_does_not_fall_through_to_mcp():
    async def broken_handle(utterance, ctx, session, emit=None, max_steps=None):
        raise RuntimeError("planner exploded")

    agent = _agent_with_fake_brain(broken_handle)
    out = asyncio.run(agent.execute(
        "show git status", session_id="brain-first-error",
    ))

    assert out["route"]["tier"] == "brain_first_error"
    assert out["execution"]["success"] is False
    assert "Brain failed before planning" in out["execution"]["output"]
    assert out["route"]["tool_name"] == "brain"
