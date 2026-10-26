"""Scoped subagent profiles, delegation, history, and API surface."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.brain.agent_loop import (
    AgentBrain,
    BrainResult,
    BrainStepResult,
    delegation_allowed_for,
    is_explicit_subagent_request,
    is_subagent_candidate,
)
from winbrow.brain.model_gateway import GatewayResponse, ModelGateway
from winbrow.registry import ArgSpec, Tool, ToolRegistry
from winbrow.subagents import (
    SUBAGENT_PROFILES,
    ScopedToolRegistry,
    SubagentManager,
    get_subagent_tools,
    list_profiles,
)
import server  # noqa: E402


class FakeBaseRegistry:
    def __init__(self):
        self._tools = {
            "read_document": SimpleNamespace(name="read_document"),
            "create_text_file": SimpleNamespace(name="create_text_file"),
            "browser_navigate": SimpleNamespace(name="browser_navigate"),
            "mcp_fetch_get": SimpleNamespace(name="mcp_fetch_get"),
        }

    def get(self, name):
        return self._tools.get(name)

    def all_tools(self):
        return list(self._tools.values())

    def available(self, ctx):
        return list(self._tools.values())


def test_profiles_and_tool_registration():
    from winbrow.permissions import tool_tier
    profiles = list_profiles()
    assert {item["id"] for item in profiles} == set(SUBAGENT_PROFILES)
    assert len(profiles) == 6
    tools = get_subagent_tools()
    assert [tool.name for tool in tools] == ["delegate_task"]
    assert tools[0].impl == "subagent"
    assert any(arg.name == "tasks" for arg in tools[0].args)
    assert tool_tier("delegate_task") == "t1"


def test_explicit_subagent_intent_detection():
    positives = [
        "use a subagent to compare these files",
        "use subagents to research these competitors",
        "delegate this research to a researcher",
        "spawn three agents to check these pages in parallel",
        "run separate agents for each source",
        "ask a verifier to check the report",
        "use an agent team for this",
    ]
    negatives = [
        "compare these two files",
        "research these competitors in parallel",
        "open the report",
        "read a.txt and summarize it",
        "delegate access is handled by the admin",
    ]
    for text in positives:
        assert is_explicit_subagent_request(text), text
    for text in negatives:
        assert not is_explicit_subagent_request(text), text


def test_delegation_defaults_to_auto(monkeypatch):
    monkeypatch.delenv("WINBROW_SUBAGENTS", raising=False)
    monkeypatch.delenv("WINBROW_SUBAGENTS_EXPLICIT", raising=False)
    assert delegation_allowed_for("compare these two files") is True
    assert is_subagent_candidate("research these three competitors in parallel")
    assert is_subagent_candidate("review the diff for security issues")
    assert is_subagent_candidate("verify the report exists")
    assert not is_subagent_candidate("open the report")
    assert not is_subagent_candidate("what time is it")
    assert not is_subagent_candidate("compare prices on amazon")


def test_explicit_only_mode(monkeypatch):
    monkeypatch.setenv("WINBROW_SUBAGENTS_EXPLICIT", "1")
    assert delegation_allowed_for("compare these two files") is False
    assert delegation_allowed_for("use subagents to compare these files") is True


def test_subagents_can_be_disabled(monkeypatch):
    monkeypatch.setenv("WINBROW_SUBAGENTS", "0")
    assert delegation_allowed_for("use subagents to compare these files") is False


def test_delegate_gate_respects_turn_permission():
    registry = ToolRegistry()
    registry.register(get_subagent_tools()[0])

    async def fake_executor(name, args):
        return {"success": True, "output": "delegated"}

    brain = AgentBrain(
        gateway=ModelGateway(),
        registry=registry,
        store=None,
        executor=fake_executor,
        enable_skill_writer=False,
    )
    brain.subagents_allowed = False
    blocked = asyncio.run(brain._run_step("delegate_task", {
        "agent": "file-analyst", "task": "Read a.txt",
    }))
    assert blocked.success is False
    assert "disabled or explicit-only" in blocked.output

    brain.subagents_allowed = True
    allowed = asyncio.run(brain._run_step("delegate_task", {
        "agent": "file-analyst", "task": "Read a.txt",
    }))
    assert allowed.success is True



    base = FakeBaseRegistry()
    scoped = ScopedToolRegistry(base, lambda name: name == "read_document" or name.startswith("mcp_fetch_"))
    assert scoped.get("read_document") is not None
    assert scoped.get("create_text_file") is None
    assert scoped.get("mcp_fetch_get") is not None
    assert {tool.name for tool in scoped.all_tools()} == {"read_document", "mcp_fetch_get"}


def test_subagent_brain_blocks_disallowed_skill():
    brain = AgentBrain(
        gateway=ModelGateway(),
        registry=ToolRegistry(),
        store=None,
        executor=lambda name, args: None,
        allowed_skills=lambda name: name == "read_document",
        system_prompt="subagent",
        enable_skill_writer=False,
    )
    result = asyncio.run(brain._run_step("remember_fact", {"fact": "x"}))
    assert result.success is False
    assert "not available to this subagent" in result.output
    assert "read_document" in brain.skill_registry
    assert "delegate_task" not in brain.skill_registry


class FakeAgent:
    gateway = None
    registry = FakeBaseRegistry()
    store = None

    def __init__(self):
        self.subagents = None

    def get_context(self):
        return SimpleNamespace()

    async def run_skill(self, name, args, **kwargs):
        return {"success": True, "output": "ok"}


class FakeBrain:
    def __init__(self, result=None):
        self.result = result or BrainResult(
            kind="action",
            success=True,
            response="Found the answer.",
            steps=[
                BrainStepResult("read_document", {"path": "a.txt"}, True, "read ok"),
                BrainStepResult("find_files", {"search_term": "a"}, True, "found"),
            ],
        )

    async def handle(self, task, ctx, session, max_steps=None):
        assert task
        assert max_steps
        return self.result


def test_manager_runs_and_records_history(tmp_path):
    manager = SubagentManager(
        FakeAgent(),
        brain_factory=lambda profile: FakeBrain(),
        history_path=tmp_path / "subagents.json",
    )
    out = asyncio.run(manager.execute("delegate_task", {
        "agent": "file-analyst",
        "task": "Read a.txt and report the first line.",
    }))
    assert out["success"] is True
    assert out["count"] == 1
    run = out["subagents"][0]
    assert run["status"] == "done"
    assert run["step_count"] == 2
    assert "Found the answer." in run["summary"]
    assert manager.recent(1)[0]["id"] == run["id"]
    assert (tmp_path / "subagents.json").is_file()


def test_manager_reports_blocked_child(tmp_path):
    blocked = BrainResult(
        kind="chat", success=True, needs_input=True,
        question="Which file did you mean?", response="Which file did you mean?",
    )
    manager = SubagentManager(
        FakeAgent(),
        brain_factory=lambda profile: FakeBrain(blocked),
        history_path=tmp_path / "subagents.json",
    )
    out = asyncio.run(manager.execute("delegate_task", {
        "agent": "file-analyst", "task": "Read the report.",
    }))
    assert out["success"] is False
    assert out["subagents"][0]["status"] == "blocked"


def test_manager_parallel_only_for_safe_profiles(tmp_path):
    manager = SubagentManager(
        FakeAgent(),
        brain_factory=lambda profile: FakeBrain(),
        history_path=tmp_path / "subagents.json",
    )
    # file-analyst is parallel-safe.
    out = asyncio.run(manager.execute("delegate_task", {
        "parallel": "true",
        "tasks": [
            {"agent": "file-analyst", "task": "Read a.txt"},
            {"agent": "file-analyst", "task": "Read b.txt"},
        ],
    }))
    assert out["parallel_used"] is True

    # researcher uses the shared browser, so the manager falls back to serial.
    out = asyncio.run(manager.execute("delegate_task", {
        "parallel": "true",
        "tasks": [
            {"agent": "researcher", "task": "Research a"},
            {"agent": "file-analyst", "task": "Read b.txt"},
        ],
    }))
    assert out["parallel_used"] is False
    assert "sequential" in out["output"]


def test_unknown_profile_and_bad_tasks(tmp_path):
    manager = SubagentManager(
        FakeAgent(),
        brain_factory=lambda profile: FakeBrain(),
        history_path=tmp_path / "subagents.json",
    )
    out = asyncio.run(manager.execute("delegate_task", {
        "agent": "nope", "task": "x",
    }))
    assert out["success"] is False
    assert "Unknown subagent profile" in out["output"]

    out = asyncio.run(manager.execute("delegate_task", {
        "agent": "file-analyst", "task": "",
    }))
    assert out["success"] is False
    assert "non-empty" in out["output"]


def test_api_and_ui_expose_subagents():
    client = TestClient(server.app)
    resp = client.get("/api/subagents")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["profiles"]) == 6
    assert "runs" in data

    page = client.get("/")
    assert page.status_code == 200
    assert 'id="subagentsBtn"' in page.text
    assert 'id="subagentsModal"' in page.text
    assert "openSubagents" in page.text


class ScriptedGateway:
    """Returns one read action, then done — enough for a real child brain."""

    def __init__(self):
        self.calls = 0
        self.providers = [SimpleNamespace(name="fake", model="fake-1")]

    @property
    def available(self) -> bool:
        return True

    async def chat(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            payload = {
                "kind": "action",
                "step": {
                    "skill": "read_document",
                    "args": {"path": "a.txt"},
                    "why": "read the file",
                },
            }
        else:
            payload = {
                "kind": "done",
                "summary": "The first line is hello.",
                "evidence": {"confirmation": "read result in history"},
            }
        return GatewayResponse(
            content=json.dumps(payload), provider="fake", model="fake-1",
        )


class RealChildAgent:
    def __init__(self):
        self.gateway = ScriptedGateway()
        self.registry = FakeBaseRegistry()
        self.store = None
        self.subagents = None
        self.skills = []

    def get_context(self):
        return SimpleNamespace()

    async def run_skill(self, name, args, **kwargs):
        assert name == "read_document"
        return {"success": True, "output": "hello world"}


def test_real_child_brain_runs_to_done(tmp_path):
    agent = RealChildAgent()
    manager = SubagentManager(
        agent,
        history_path=tmp_path / "subagents.json",
    )
    out = asyncio.run(manager.execute("delegate_task", {
        "agent": "file-analyst",
        "task": "Read a.txt and report the first line.",
        "context": "The file is expected to contain a greeting.",
    }))
    assert out["success"] is True
    run = out["subagents"][0]
    assert run["status"] == "done"
    assert "hello" in run["summary"].lower()
    assert agent.gateway.calls >= 2
    assert run["steps"][0]["skill"] == "read_document"

