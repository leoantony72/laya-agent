"""Repeat-guard recovery in the closed loop.

Regression for the Recovery_Test run: the planner saved a file, then re-read
the original to verify the save. ``mcp_filesystem_read_file`` was treated as a
completed step, skipped three times, and the whole run hard-failed with
"Stopped: the planner kept repeating ...". Re-reading after a mutation is
required verification, not a loop.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.brain.agent_loop import (
    READ_REPEAT_LIMIT,
    REPEAT_HARD_LIMIT,
    AgentBrain,
    BrainStepResult,
)
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
        db_path=Path(tempfile.gettempdir()) / "wb_recovery.db",
        chroma_path=Path(tempfile.gettempdir()) / "wb_recovery_chroma",
    )
    return AgentBrain(_Gateway(plans), _Registry(), store, executor=None)


def _action(skill, **args):
    return {"kind": "action", "step": {"skill": skill, "args": args}}


def _run(plans, calls, output="contents"):
    brain = _brain(plans)

    async def fake_run(skill, args, why="", ctx=None):
        calls.append((skill, dict(args)))
        return BrainStepResult(skill, args, True, output)

    brain._run_step = fake_run
    return asyncio.run(brain.handle("read the file", _Ctx(), _Session()))


def test_rereading_a_file_after_success_is_executed_not_blocked():
    calls = []
    result = _run(
        [
            _action("mcp_filesystem_read_file", path="C:\\x\\records.txt"),
            _action("mcp_filesystem_read_file", path="C:\\x\\records.txt"),
            _action("mcp_filesystem_read_file", path="C:\\x\\records.txt"),
            {"kind": "done", "summary": "verified"},
        ],
        calls,
    )

    assert [skill for skill, _ in calls] == ["mcp_filesystem_read_file"] * 3
    assert len(result.steps) == 3
    assert result.success is True
    assert "kept repeating" not in result.response


def test_repeated_reads_are_capped_then_skipped_without_stopping():
    calls = []
    read = _action("mcp_filesystem_read_file", path="C:\\x\\records.txt")
    plans = [read] * (1 + READ_REPEAT_LIMIT + 2)
    plans.append({"kind": "done", "summary": "verified"})
    result = _run(plans, calls)

    # One initial read plus the bounded verification re-reads.
    assert len(calls) == 1 + READ_REPEAT_LIMIT
    assert result.success is True


def test_mutating_repeat_is_skipped_then_planner_is_forced_forward():
    calls = []
    result = _run(
        [
            _action("type_text", text="records 11-15"),
            _action("type_text", text="records 11-15"),
            _action("type_text", text="records 11-15"),
            _action("focus_window", title="Notepad"),
            {"kind": "done", "summary": "done"},
        ],
        calls,
    )

    # The mutating step runs once; the two repeats are skipped, then the
    # planner picks a different action instead of hard-failing the run.
    assert [skill for skill, _ in calls] == ["type_text", "focus_window"]
    assert result.success is True


def test_repeat_limit_finalizes_honestly_without_hard_stop():
    calls = []
    plans = [_action("type_text", text="x")]
    plans += [_action("type_text", text="x")] * (REPEAT_HARD_LIMIT + 2)
    result = _run(plans, calls)

    assert [skill for skill, _ in calls] == ["type_text"]
    assert result.complete is False
    assert "kept choosing" in result.response
    assert "Stopped: the planner kept repeating" not in result.response


def test_repeated_failure_gets_a_recovery_turn_instead_of_stopping():
    calls = []

    async def fake_run(skill, args, why="", ctx=None):
        calls.append((skill, dict(args)))
        if skill == "open_file":
            return BrainStepResult(
                skill, args, False, "File not found: the path no longer exists")
        return BrainStepResult(skill, args, True, "found the moved file")

    brain = _brain([
        _action("open_file", target="C:\\x\\old\\copy.txt"),
        _action("open_file", target="C:\\x\\old\\copy.txt"),
        # The recovery turn ignores the disable list and retries open_file
        # with different args: it must still be enforced, not executed.
        _action("open_file", target="C:\\x\\old\\renamed.txt"),
        _action("mcp_filesystem_list_directory", path="C:\\x"),
        _action("open_file", target="C:\\x\\new\\copy.txt"),
        {"kind": "done", "summary": "recovered"},
    ])
    brain._run_step = fake_run
    result = asyncio.run(brain.handle("reopen the file", _Ctx(), _Session()))

    assert calls[2][0] == "mcp_filesystem_list_directory"
    assert result.success is True
    assert "failed twice with the same error" not in result.response
