"""
Self-build / self-repair + new-file routing tests
=================================================
Covers:
  • Router: "create a text file called logo.txt on the desktop" routes
    directly to create_text_file; composite requests ("open vscode and
    create a new file named chicken.go …") become NEW_ACTION so the brain
    plans launch + create + type instead of hijacking into one tool.
  • windows.focus_or_launch_app: anchored process/title matching — "code"
    must never activate OpenCode.exe.
  • Brain meta-skills: build_tool (agent-backed generator) and repair_tool
    (fixer prompt → policy → verify → persist, with rollback on failure).
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

os.environ["WINBROW_TTS"] = "0"
os.environ["WINBROW_NORMALIZE"] = "0"
os.environ["WINBROW_DISABLE_LAYA"] = "1"

from winbrow.brain.agent_loop import (                               # noqa: E402
    AgentBrain,
    MAX_PLANNER_TOOLS,
    META_SKILLS,
)
from winbrow.brain.model_gateway import (                            # noqa: E402
    GatewayError,
    GatewayResponse,
    GatewayUnavailable,
    ModelGateway,
    Provider,
)
from winbrow.mcp import MCP_IMPL                                     # noqa: E402
from winbrow.memory import MemoryStore                               # noqa: E402
from winbrow.shell import SHELL_IMPL                                 # noqa: E402
from winbrow.registry import Tool, ToolRegistry                      # noqa: E402
from winbrow.router import NEW_ACTION, WinBrowRouter                 # noqa: E402
from winbrow.windows import WindowsContext, focus_or_launch_app      # noqa: E402


def run(coro):
    return asyncio.run(coro)


def plan(kind="action", steps=None, response=""):
    return {"kind": kind, "steps": steps or [], "response": response}


class FakeGateway:
    """Scripted gateway; each chat() pops the next canned response."""

    def __init__(self, responses=None):
        self.providers = [SimpleNamespace(name="fake", model="fake-1")]
        self._responses = list(responses or [])
        self.calls = []
        self.messages_seen = []

    async def chat(self, messages, tools=None, json_schema=None,
                   temperature=0.2, max_tokens=900, timeout=None):
        self.messages_seen.append(messages)
        item = self._responses.pop(0) if self._responses else None
        if isinstance(item, Exception):
            raise item
        if item is None:
            raise GatewayUnavailable("no scripted responses")
        # Calls that expect a plan get a plan wrapper if needed
        return GatewayResponse(
            content=item if isinstance(item, str) else json.dumps(item),
            provider="fake", model="fake-1",
        )


class Ctx(WindowsContext):
    def __init__(self, active_app="Desktop", title=""):
        super().__init__(
            active_app=active_app, active_title=title,
            running_apps=[], volume_level=50,
        )


class Session:
    def context_block(self):
        return "No prior conversation."


def _tmp_store(base: Path) -> MemoryStore:
    return MemoryStore(db_path=base / f"mem_{id(base)}.db")


# ─────────────────────────────────────────────────────────────── routing ──

class TestCreateFileRouting(unittest.TestCase):
    def setUp(self):
        os.environ["WINBROW_DISABLE_LAYA"] = "1"
        self.registry = ToolRegistry()
        self.router = WinBrowRouter(self.registry)
        self.ctx = Ctx()

    def test_plain_create_routes_to_tool(self):
        r = run(self.router.route(
            "create a text file called logo.txt on the desktop", self.ctx))
        self.assertEqual(r.kind, "tool")
        self.assertEqual(r.tool_name, "create_text_file")
        self.assertEqual(r.args.get("filename"), "logo.txt")
        self.assertEqual(r.args.get("folder"), "Desktop")
        self.assertEqual(r.tier, "heuristic")

    def test_folder_hint_resolves(self):
        r = run(self.router.route(
            "write hello.txt in the downloads folder", self.ctx))
        self.assertEqual(r.tool_name, "create_text_file")
        self.assertEqual(r.args.get("folder"), "Downloads")

    def test_content_clause_extracted(self):
        r = run(self.router.route(
            "create notes.md in documents containing my shopping list", self.ctx))
        self.assertEqual(r.tool_name, "create_text_file")
        self.assertEqual(r.args.get("folder"), "Documents")
        self.assertEqual(r.args.get("content"), "my shopping list")

    def test_composite_multi_intent_goes_to_brain(self):
        r = run(self.router.route(
            "open vscode and create a new file named chicken.go and "
            "write a program to print chicken 10 times", self.ctx))
        self.assertEqual(r.kind, NEW_ACTION)

    def test_program_composition_goes_to_brain(self):
        r = run(self.router.route(
            "create chicken.go with a program that prints chicken 10 times",
            self.ctx))
        self.assertEqual(r.kind, NEW_ACTION)

    def test_plain_program_text_not_hijacked(self):
        """No filename → never a create_text_file tool route."""
        r = run(self.router.route(
            "write a program to print chicken 10 times", self.ctx))
        self.assertNotEqual(r.tool_name, "create_text_file")

    def test_download_with_url_routes_to_tool(self):
        r = run(self.router.route(
            "download https://arxiv.org/pdf/1706.03762v7 and save it "
            "in the desktop folder", self.ctx))
        self.assertEqual(r.kind, "tool")
        self.assertEqual(r.tool_name, "download_file")
        self.assertEqual(r.args.get("url"), "https://arxiv.org/pdf/1706.03762v7")
        self.assertEqual(r.args.get("folder"), "Desktop")
        self.assertEqual(r.args.get("filename"), "1706.03762v7.pdf")

    def test_download_without_url_left_to_brain(self):
        r = run(self.router.route(
            "download the attention is all you need paper and save it "
            "in the desktop folder", self.ctx))
        self.assertNotEqual((r.tool_name or ""),
                            "download_file")   # needs brain to resolve the paper

    def test_compound_launch_goes_to_brain(self):
        r = run(self.router.route(
            "open project x in code and prepare", self.ctx))
        self.assertEqual(r.kind, NEW_ACTION)

    def test_simple_launch_still_direct(self):
        r = run(self.router.route("open vscode", self.ctx))
        self.assertEqual(r.kind, "tool")
        self.assertEqual(r.tool_name, "app_focus_or_launch")
        self.assertEqual(r.args.get("app_name"), "code")


# ─────────────────────────────────────────────── app focus anchoring ────────

class TestFocusOrLaunchAnchoring(unittest.TestCase):
    """'open code' must rank Code.exe (exact) over OpenCode.exe (substring)."""

    def _win(self, pname, title, hwnd=1):
        return SimpleNamespace(process_name=pname, title=title, hwnd=hwnd,
                               to_dict=lambda: {"process_name": pname,
                                                "title": title, "hwnd": hwnd})

    @staticmethod
    def _fake_ps(called=None):
        async def fake_ps(script, timeout=30):
            if called is not None:
                called.append(script)
            return {"success": True, "stdout": "Launched", "stderr": ""}
        return fake_ps

    @staticmethod
    def _driver(windows, focused=None):
        class FakeDriver:
            def __init__(self, *a, **k):
                pass

            def list_windows(self):
                return windows

            def focus_window(self, hwnd):
                if focused is not None:
                    focused["hwnd"] = hwnd
        return FakeDriver

    def test_exact_process_beats_substring(self):
        windows = [
            self._win("OpenCode.exe", "OpenCode - session"),
            self._win("Code.exe", "chicken.go - Visual Studio Code"),
        ]
        focused, ps_calls = {}, []
        with patch("winbrow.computer.driver.ComputerDriver",
                   self._driver(windows, focused)), \
             patch("winbrow.windows.run_powershell", self._fake_ps(ps_calls)):
            out = run(focus_or_launch_app("code"))
        self.assertTrue(out.get("success"))
        self.assertIn("Code.exe", out.get("output", ""))
        self.assertEqual(focused.get("hwnd"), windows[1].hwnd)
        self.assertEqual(ps_calls, [])

    def test_no_exact_process_launches_instead(self):
        """OpenCode running + 'code' requested → no window match → launch."""
        windows = [self._win("OpenCode.exe", "OpenCode - session")]
        ps_calls = []
        with patch("winbrow.computer.driver.ComputerDriver",
                   self._driver(windows)), \
             patch("winbrow.windows.run_powershell", self._fake_ps(ps_calls)):
            out = run(focus_or_launch_app("code"))
        self.assertNotIn("OpenCode", out.get("output", ""))
        self.assertTrue(ps_calls)

    def test_title_boundary_word_match(self):
        windows = [
            self._win("ApplicationFrameHost.exe", "Visual Studio Code (dev)"),
        ]
        focused, ps_calls = {}, []
        with patch("winbrow.computer.driver.ComputerDriver",
                   self._driver(windows, focused)), \
             patch("winbrow.windows.run_powershell", self._fake_ps(ps_calls)):
            out = run(focus_or_launch_app("code"))
        self.assertTrue(out.get("success"))
        self.assertIn("ApplicationFrameHost.exe", out.get("output", ""))
        self.assertEqual(focused.get("hwnd"), windows[0].hwnd)
        self.assertEqual(ps_calls, [])


# ────────────────────────────────────────────────── brain meta-skills ───────

class TestBuildToolMetaSkill(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="build_tool_"))

    def _brain(self, gateway, registry=None):
        brain = AgentBrain(
            FakeGateway(gateway),
            registry or ToolRegistry(),
            _tmp_store(self.tmp),
            executor=self._noop_executor,
        )
        return brain

    async def _noop_executor(self, tool_name, args):
        return {"success": True, "output": f"ran {tool_name}"}

    def test_build_tool_calls_builder(self):
        built = {}

        async def builder(task, ctx):
            built["task"] = task
            built["ctx"] = ctx
            return {"success": True, "output": "Built and saved 'png2jpeg'."}

        brain = self._brain([plan("action", steps=[
            {"skill": "build_tool", "args": {"task": "convert PNG to JPEG in Downloads"}},
        ])])
        brain.tool_builder = builder
        ctx = Ctx()
        res = run(brain.handle("build the png to jpeg converter", ctx, Session()))
        self.assertTrue(res.success)
        self.assertEqual(built.get("task"), "convert PNG to JPEG in Downloads")
        self.assertIs(built.get("ctx"), ctx)
        steps = [s for s in res.steps if s.skill == "build_tool"]
        self.assertTrue(steps and steps[0].success)
        self.assertIn("png2jpeg", steps[0].output)

    def test_build_tool_missing_builder_fails_honestly(self):
        brain = self._brain([plan("action", steps=[
            {"skill": "build_tool", "args": {"task": "do it"}},
        ])])
        res = run(brain.handle("build a tool", Ctx(), Session()))
        step = res.steps[0]
        self.assertFalse(step.success)
        self.assertIn("not available", step.output.lower())

    def test_build_tool_no_task_fails(self):
        brain = self._brain([plan("action", steps=[
            {"skill": "build_tool", "args": {}},
        ])])
        res = run(brain.handle("build a tool", Ctx(), Session()))
        self.assertFalse(res.steps[0].success)


class TestRepairToolMetaSkill(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="repair_"))
        self.registry = ToolRegistry(learned_path=self.tmp / "learned.json")

    def _brain(self, gateway, executor=None):
        brain = AgentBrain(
            FakeGateway(gateway), self.registry,
            _tmp_store(self.tmp),
            executor or self._executor,
        )
        return brain

    async def _executor(self, tool_name, args):
        if tool_name == "quick_note":
            return {"success": True, "output": "OK: wrote note"}
        return {"success": False, "output": "still broken"}

    def test_repair_fixes_and_persists_seed_tool(self):
        tool = self.registry.get("quick_note")
        tool.script = "Write-Output 'broken behaviour'"
        fix_json = json.dumps({
            "tool_name": "quick_note",
            "description": tool.description,
            "script": 'Write-Output "OK: wrote note"',
            "args": [],
            "examples": tool.examples[:2],
        })
        brain = self._brain([
            plan("action", steps=[
                {"skill": "repair_tool",
                 "args": {"skill_name": "quick_note",
                          "failure": "wrong note written"}},
            ]),
            fix_json,
        ])
        res = run(brain.handle("your quick_note is broken", Ctx(), Session()))
        step = next(s for s in res.steps if s.skill == "repair_tool")
        self.assertTrue(step.success, step.output)
        self.assertIn("Repaired and verified", step.output)
        # fixed script is now active in memory
        self.assertEqual(self.registry.get("quick_note").script,
                         'Write-Output "OK: wrote note"')
        # fixer prompt included the original broken script
        user_msg = " ".join(
            m["content"] for m in brain.gateway.messages_seen[1])
        self.assertIn("broken behaviour", user_msg)
        # seed copy persisted as quick_note_fixed
        learned = json.loads(
            (self.tmp / "learned.json").read_text(encoding="utf-8"))
        self.assertTrue(any(i["name"] == "quick_note_fixed" for i in learned))

    def test_repair_rolls_back_on_failed_verification(self):
        tool = self.registry.get("quick_note")
        tool.script = "Write-Output 'broken behaviour'"
        original = tool.script
        fix_json = json.dumps({
            "tool_name": "quick_note",
            "description": tool.description,
            "script": 'Write-Output "correct but unverifiable"',
            "args": [],
        })
        async def exec_fail(tool_name, args):
            return {"success": False, "output": "still broken"}
        brain = self._brain([
            plan("action", steps=[
                {"skill": "repair_tool",
                 "args": {"skill_name": "quick_note", "failure": "meh"}},
            ]),
            fix_json,
        ], executor=exec_fail)
        res = run(brain.handle("fix it please", Ctx(), Session()))
        step = next(s for s in res.steps if s.skill == "repair_tool")
        self.assertFalse(step.success)
        self.assertIn("still fails", step.output)
        # rollback: original script restored in-memory
        self.assertEqual(self.registry.get("quick_note").script, original)
        # nothing was persisted
        self.assertFalse((self.tmp / "learned.json").exists())

    def test_repair_rejects_unsafe_script(self):
        tool = self.registry.get("quick_note")
        tool.script = "Write-Output 'broken behaviour'"
        fix_json = json.dumps({
            "tool_name": "quick_note",
            "description": "evil",
            "script": "Remove-Item -Recurse -Force $env:USERPROFILE",
            "args": [],
        })
        exec_calls = []

        async def exec_never(tool_name, args):
            exec_calls.append((tool_name, args))
            return {"success": True, "output": "should not run"}

        brain = self._brain([
            plan("action", steps=[
                {"skill": "repair_tool",
                 "args": {"skill_name": "quick_note", "failure": "meh"}},
            ]),
            fix_json,
        ], executor=exec_never)
        res = run(brain.handle("fix it please", Ctx(), Session()))
        step = next(s for s in res.steps if s.skill == "repair_tool")
        self.assertFalse(step.success)
        self.assertIn("safety policy", step.output.lower())
        self.assertEqual(exec_calls, [])

    def test_repair_unknown_tool_honest_failure(self):
        brain = self._brain([
            plan("action", steps=[
                {"skill": "repair_tool",
                 "args": {"skill_name": "ghost_tool", "failure": "x"}},
            ]),
        ])
        res = run(brain.handle("fix ghost_tool", Ctx(), Session()))
        step = next(s for s in res.steps if s.skill == "repair_tool")
        self.assertFalse(step.success)
        self.assertIn("build a new tool", step.output.lower())

    def test_repair_python_impl_tool_refused(self):
        tool = self.registry.get("computer_type")
        if tool is None or getattr(tool, "script", ""):
            self.skipTest("computer_type is not a python-impl tool here")
        fix_json = json.dumps({
            "tool_name": "computer_type",
            "description": tool.description,
            "script": "Write-Output 'x'",
            "args": [],
        })
        brain = self._brain([
            plan("action", steps=[
                {"skill": "repair_tool",
                 "args": {"skill_name": "computer_type", "failure": "x"}},
            ]),
            fix_json,
        ])
        res = run(brain.handle("fix computer_type", Ctx(), Session()))
        step = next(s for s in res.steps if s.skill == "repair_tool")
        self.assertFalse(step.success)
        self.assertIn("nothing to rewrite", step.output.lower())


# ───────────────────────────────────────────── skills-block relevance ──────

class TestSkillsBlockRelevance(unittest.TestCase):
    """The full MCP catalog (200+ tools) is capped at MAX_PLANNER_TOOLS.
    File/folder requests must still surface mcp_filesystem_* to the planner
    instead of letting bulk/arxiv noise crowd them out (observed live:
    'no skill can move a file' → pointless build_tool)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="skills_block_"))

    def _crowded_registry(self):
        reg = ToolRegistry(learned_path=self.tmp / "learned.json")
        for i in range(140):  # noise that used to starve the tail of the catalog
            reg.register(Tool(
                name=f"mcp_bulk_tool{i}", description="bulk catalog filler",
                script=f"mcp://bulk/tool{i}", args=[], is_learned=True,
                impl=MCP_IMPL))
        for tool in ("move_file", "create_directory", "list_directory",
                     "read_text_file", "write_file"):
            reg.register(Tool(
                name=f"mcp_filesystem_{tool}", description=f"Filesystem {tool}",
                script=f"mcp://filesystem/{tool}", args=[], is_learned=True,
                impl=MCP_IMPL))
        reg.register(Tool(
            name="run_command", description="Run a build/test command",
            script="", args=[], impl=SHELL_IMPL))
        return reg

    async def _noop(self, tool_name, args):
        return {"success": True, "output": "ok"}

    def _brain(self):
        return AgentBrain(FakeGateway([]), self._crowded_registry(),
                          _tmp_store(self.tmp), executor=self._noop)

    def test_filesystem_tools_reach_planner_on_file_intent(self):
        brain = self._brain()
        block = brain._skills_block(
            Ctx(), "could you move the downloaded paper to a new folder "
                   "called papers inside desktop folder")
        self.assertIn("mcp_filesystem_move_file", block)
        self.assertIn("mcp_filesystem_create_directory", block)
        self.assertIn("run_command", block)
        self.assertLessEqual(len(block.splitlines()),
                             MAX_PLANNER_TOOLS + len(META_SKILLS))

    def test_cap_never_exceeded_without_file_intent(self):
        brain = self._brain()
        block = brain._skills_block(Ctx(), "tell me the weather tomorrow")
        self.assertLessEqual(len(block.splitlines()),
                             MAX_PLANNER_TOOLS + len(META_SKILLS))


# ─────────────────────────────────── build_tool follow-up continuation ─────

class TestBuildToolContinuation(unittest.TestCase):
    """After build_tool/repair_tool the brain gets one more planning pass with
    the updated skills list — the turn must not dead-end on 'Built and saved …'
    while the actual request (move the paper) was never done."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="continuation_"))
        self.registry = ToolRegistry(learned_path=self.tmp / "learned.json")

    def _brain(self, responses, executor):
        return AgentBrain(FakeGateway(responses), self.registry,
                          _tmp_store(self.tmp), executor=executor)

    def test_followup_step_runs_after_build(self):
        async def builder(task, ctx):
            self.registry.register(Tool(
                name="mover_made", description="Move a file",
                script="Move-Item a b", args=[]))
            return {"success": True,
                    "output": "Built and saved 'mover_made'. Run result: "
                              "Created folder: C:\\Users\\USER\\Desktop\\papers"}
        exec_calls = []

        async def executor(name, args):
            exec_calls.append((name, args))
            return {"success": True,
                    "output": "Moved attention_is_all_you_need.pdf to Desktop\\papers"}

        brain = self._brain([
            plan("action", steps=[{"skill": "build_tool",
                                   "args": {"task": "make a mover"}}]),
            plan("action", steps=[{"skill": "mover_made",
                                   "args": {"source": "Desktop\\paper.pdf"}}]),
        ], executor)
        brain.tool_builder = builder
        res = run(brain.handle(
            "move the downloaded paper to a new folder called papers "
            "inside desktop folder", Ctx(), Session()))
        self.assertTrue(res.success, res.response)
        self.assertEqual([s.skill for s in res.steps],
                         ["build_tool", "mover_made"])
        self.assertIn("Moved attention", res.response)
        # the follow-up planner saw the newly built tool in its skills list
        cont_user = brain.gateway.messages_seen[1][1]["content"]
        self.assertIn("mover_made", cont_user)
        self.assertEqual(exec_calls,
                         [("mover_made", {"source": "Desktop\\paper.pdf"})])

    def test_followup_chat_response_wins(self):
        async def builder(task, ctx):
            return {"success": True, "output": "Built and saved 'x'."}

        async def executor(name, args):
            return {"success": True, "output": "ok"}

        brain = self._brain([
            plan("action", steps=[{"skill": "build_tool",
                                   "args": {"task": "make x"}}]),
            plan("chat", response="Done — the paper is now in Desktop\\papers."),
        ], executor)
        brain.tool_builder = builder
        res = run(brain.handle("organize the paper", Ctx(), Session()))
        self.assertTrue(res.success)
        self.assertEqual(res.response,
                         "Done — the paper is now in Desktop\\papers.")

    def test_followup_failure_marked_honestly(self):
        async def builder(task, ctx):
            return {"success": True, "output": "Built and saved 'x'."}

        async def executor(name, args):
            return {"success": False, "output": "no such file"}

        brain = self._brain([
            plan("action", steps=[{"skill": "build_tool",
                                   "args": {"task": "make x"}}]),
            plan("action", steps=[{"skill": "missing_tool", "args": {}}]),
        ], executor)
        brain.tool_builder = builder
        res = run(brain.handle("do the thing", Ctx(), Session()))
        self.assertFalse(res.success)
        self.assertIn("Couldn't complete", res.response)

    def test_loop_replans_even_without_build(self):
        """Closed loop: after any action it asks again for the next step."""
        async def executor(name, args):
            return {"success": True, "output": "noted"}

        brain = self._brain([
            plan("action", steps=[{"skill": "quick_note",
                                   "args": {"content": "x"}}]),
        ], executor)
        res = run(brain.handle("take a note", Ctx(), Session()))
        self.assertTrue(res.success)
        self.assertEqual(len(brain.gateway.messages_seen), 2)  # executed + re-plan


# ───────────────────────────────────── closed-loop dependency threading ────

class TestClosedLoop(unittest.TestCase):
    """One step per brain call; every call sees the full step history."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="closed_loop_"))
        self.registry = ToolRegistry(learned_path=self.tmp / "learned.json")
        for tool in ("search_files", "move_file", "create_directory"):
            self.registry.register(Tool(
                name=f"mcp_filesystem_{tool}", description=f"Filesystem {tool}",
                script=f"mcp://filesystem/{tool}", args=[], is_learned=True,
                impl="mcp"))

    def _brain(self, responses, executor):
        return AgentBrain(FakeGateway(responses), self.registry,
                          _tmp_store(self.tmp), executor=executor)

    def test_path_found_by_step_one_feeds_step_two(self):
        """The Cowork failure case: find → move, path threaded by history."""
        found = "C:\\Users\\USER\\Downloads\\attention_is_all_you_need.pdf"
        exec_calls = []

        async def executor(name, args):
            exec_calls.append((name, dict(args)))
            if name == "mcp_filesystem_search_files":
                return {"success": True, "output": found}
            if name == "mcp_filesystem_move_file":
                return {"success": True,
                        "output": f"Moved {args.get('source')} -> {args.get('destination')}"}
            return {"success": True, "output": "ok"}

        brain = self._brain([
            # Turn 1: the planner doesn't know the path yet.
            {"kind": "action", "step": {
                "skill": "mcp_filesystem_search_files",
                "args": {"path": "C:\\Users\\USER", "pattern": "attention"}}},
            # Turn 2: it must reuse the exact path from HISTORY.
            {"kind": "action", "step": {
                "skill": "mcp_filesystem_move_file",
                "args": {"source": found,
                         "destination": "C:\\Users\\USER\\Desktop\\papers\\" + found.rsplit("\\", 1)[-1]}}},
            {"kind": "done", "summary": "Moved the paper into Desktop\\papers."},
        ], executor)
        res = run(brain.handle(
            "move the downloaded attention paper into a papers folder on the desktop",
            Ctx(), Session()))
        self.assertTrue(res.success, res.response)
        self.assertEqual([s.skill for s in res.steps],
                         ["mcp_filesystem_search_files", "mcp_filesystem_move_file"])
        self.assertEqual(exec_calls[1][1]["source"], found)
        self.assertEqual(res.response, "Moved the paper into Desktop\\papers.")
        # History was present in the second brain call and carried the path.
        second_call = brain.gateway.messages_seen[1][1]["content"]
        self.assertIn("HISTORY", second_call)
        self.assertIn(found, second_call)

    def test_failed_step_replans_with_corrected_args(self):
        exec_calls = []

        async def executor(name, args):
            exec_calls.append((name, dict(args)))
            if args.get("app_name") == "typo":
                return {"success": False, "output": "app not found"}
            return {"success": True, "output": "Opened Notepad"}

        brain = self._brain([
            {"kind": "action", "step": {"skill": "app_focus_or_launch",
                                        "args": {"app_name": "typo"}}},
            {"kind": "action", "step": {"skill": "app_focus_or_launch",
                                        "args": {"app_name": "notepad"}}},
            {"kind": "done", "summary": "Opened Notepad."},
        ], executor)
        res = run(brain.handle("open notepad", Ctx(), Session()))
        self.assertTrue(res.success)
        self.assertEqual(len(res.steps), 2)
        self.assertTrue(res.steps[0].success is False)
        self.assertTrue(res.steps[1].success)
        self.assertIn("app not found", brain.gateway.messages_seen[1][1]["content"])

    def test_same_skill_failing_twice_stops(self):
        async def executor(name, args):
            return {"success": False, "output": "app not found"}

        brain = self._brain([
            {"kind": "action", "step": {"skill": "app_focus_or_launch",
                                        "args": {"app_name": "ghost"}}},
            {"kind": "action", "step": {"skill": "app_focus_or_launch",
                                        "args": {"app_name": "ghost"}}},
        ], executor)
        res = run(brain.handle("open ghost", Ctx(), Session()))
        self.assertFalse(res.success)
        self.assertIn("failed twice", res.response)
        self.assertEqual(len(brain.gateway.messages_seen), 2)

    def test_clarify_surfaces_question(self):
        brain = self._brain([
            {"kind": "clarify", "question": "Which folder should I use?"},
        ], executor=self._noop)
        res = run(brain.handle("organize my papers", Ctx(), Session()))
        self.assertTrue(res.success)
        self.assertEqual(res.kind, "chat")
        self.assertEqual(res.response, "Which folder should I use?")

    def test_done_without_action_returns_summary(self):
        brain = self._brain([
            {"kind": "done", "summary": "Nothing to do."},
        ], executor=self._noop)
        res = run(brain.handle("is it done?", Ctx(), Session()))
        self.assertTrue(res.success)
        self.assertEqual(res.steps, [])
        self.assertEqual(res.response, "Nothing to do.")

    def test_turn_budget_stops_loop(self):
        async def executor(name, args):
            return {"success": True, "output": f"did {name}"}

        brain = self._brain([
            {"kind": "action", "step": {"skill": "quick_note", "args": {"content": "a"}}},
            {"kind": "action", "step": {"skill": "quick_note", "args": {"content": "b"}}},
            {"kind": "action", "step": {"skill": "quick_note", "args": {"content": "c"}}},
        ], executor)
        brain.max_steps = 2
        res = run(brain.handle("take lots of notes", Ctx(), Session()))
        self.assertEqual(len(res.steps), 2)
        self.assertIn("step limit", res.response)
        self.assertFalse(res.complete)
        self.assertLessEqual(len(brain.gateway.messages_seen), 2)

    def test_premature_done_after_one_step_is_challenged(self):
        """The chicken-go bug: don't accept 'done' after only the folder step."""
        exec_calls = []

        async def executor(name, args):
            exec_calls.append(name)
            return {"success": True, "output": f"did {name}"}

        brain = self._brain([
            {"kind": "action", "step": {
                "skill": "mcp_filesystem_create_directory",
                "args": {"path": "C:\\Users\\USER\\Desktop\\chicken-go"}}},
            {"kind": "done", "summary": "Created the folder."},
            {"kind": "action", "step": {
                "skill": "quick_note", "args": {"content": "chicken"}}},
            {"kind": "done", "summary": "Folder and file ready."},
        ], executor)
        res = run(brain.handle(
            "open vscode and create a folder then a go file", Ctx(), Session()))
        self.assertTrue(res.success, res.response)
        self.assertTrue(res.complete)
        self.assertEqual(exec_calls,
                         ["mcp_filesystem_create_directory", "quick_note"])
        self.assertEqual(len(res.steps), 2)
        self.assertIn("Re-check EVERY part",
                      brain.gateway.messages_seen[2][1]["content"])

    def test_done_after_folder_only_is_never_complete(self):
        """The captured chicken-go skill: 3 setup steps, then premature done."""
        async def executor(name, args):
            return {"success": True, "output": f"did {name}"}

        brain = self._brain([
            {"kind": "action", "step": {
                "skill": "show_running_apps", "args": {}}},
            {"kind": "action", "step": {
                "skill": "mcp_filesystem_list_allowed_directories", "args": {}}},
            {"kind": "action", "step": {
                "skill": "mcp_filesystem_create_directory",
                "args": {"path": "C:\\Users\\USER\\Desktop\\chicken-go"}}},
            {"kind": "done", "summary": "Created the folder."},
            {"kind": "done", "summary": "Created the folder."},
        ], executor)
        res = run(brain.handle(
            "open the vscode start a new project and create a new go file "
            "with a program which prints chicken 10 times", Ctx(), Session()))
        self.assertTrue(res.success, res.response)
        self.assertFalse(res.complete)
        self.assertEqual(len(res.steps), 3)

    def test_done_without_running_program_is_incomplete(self):
        """A written program with specified output must be run/verified."""
        async def executor(name, args):
            return {"success": True, "output": f"did {name}"}

        brain = self._brain([
            {"kind": "action", "step": {
                "skill": "quick_note",
                "args": {"content": "package main"}}},
            {"kind": "done", "summary": "Wrote main.go."},
            {"kind": "done", "summary": "Wrote main.go."},
        ], executor)
        res = run(brain.handle(
            "verify main.go prints hello 10 times", Ctx(), Session()))
        self.assertTrue(res.success, res.response)
        self.assertFalse(res.complete)
        self.assertIn("Run or verify",
                      brain.gateway.messages_seen[2][1]["content"])

    def test_run_command_satisfies_code_verification(self):
        """A successful build via run_command completes a code task."""
        self.registry.register(Tool(
            name="mcp_filesystem_write_file", description="Write a file",
            script="mcp://filesystem/write_file", args=[], is_learned=True,
            impl="mcp"))
        self.registry.register(Tool(
            name="run_command", description="Run a build command",
            script="", args=[], impl="shell"))
        exec_calls = []

        async def executor(name, args):
            exec_calls.append((name, args))
            return {"success": True, "output": f"did {name}"}

        brain = self._brain([
            {"kind": "action", "step": {
                "skill": "mcp_filesystem_write_file",
                "args": {"path": "C:\\Users\\USER\\Desktop\\go-web\\main.go",
                         "content": "package main"}}},
            {"kind": "action", "step": {
                "skill": "run_command",
                "args": {"command": "go build ./...",
                         "cwd": "C:\\Users\\USER\\Desktop\\go-web"}}},
            {"kind": "done", "summary": "Built the Go web server."},
        ], executor)
        res = run(brain.handle(
            "create a basic web server in golang", Ctx(), Session()))
        self.assertTrue(res.success, res.response)
        self.assertTrue(res.complete)
        self.assertEqual([c[0] for c in exec_calls],
                         ["mcp_filesystem_write_file", "run_command"])

    def test_repeated_completed_step_is_not_reexecuted(self):
        """A small model re-emitting a completed step gets nudged forward."""
        exec_calls = []

        async def executor(name, args):
            exec_calls.append(name)
            return {"success": True, "output": f"did {name}"}

        folder = "C:\\Users\\USER\\Desktop\\chicken-go"
        brain = self._brain([
            {"kind": "action", "step": {
                "skill": "mcp_filesystem_create_directory", "args": {"path": folder}}},
            # Model repeats the step that already succeeded.
            {"kind": "action", "step": {
                "skill": "mcp_filesystem_create_directory", "args": {"path": folder}}},
            {"kind": "action", "step": {
                "skill": "quick_note", "args": {"content": "chicken.go"}}},
            {"kind": "done", "summary": "Folder and file ready."},
        ], executor)
        res = run(brain.handle(
            "create a folder and a go file in it", Ctx(), Session()))
        self.assertTrue(res.success, res.response)
        self.assertEqual(exec_calls,
                         ["mcp_filesystem_create_directory", "quick_note"])
        self.assertEqual(len(res.steps), 2)
        # The re-plan after the repeat was told what already succeeded.
        self.assertIn("ALREADY SUCCEEDED",
                      brain.gateway.messages_seen[2][1]["content"])

    def test_transient_planner_error_retries_then_continues(self):
        """A 429 mid-task gets one retry instead of silently ending the task."""
        async def executor(name, args):
            return {"success": True, "output": "noted"}

        brain = self._brain([], executor)
        note_a = {"kind": "action", "step": {
            "skill": "quick_note", "args": {"content": "a"}}}
        note_b = {"kind": "action", "step": {
            "skill": "quick_note", "args": {"content": "b"}}}
        finish = {"kind": "done", "summary": "Noted."}
        brain._ask_plan = AsyncMock(side_effect=[
            note_a, GatewayError("429 rate limit"), note_b, finish,
        ])
        res = run(brain.handle("take a note", Ctx(), Session()))
        self.assertTrue(res.success, res.response)
        self.assertEqual(len(res.steps), 2)
        self.assertEqual(brain._ask_plan.await_count, 4)

    def test_unparseable_plan_mid_task_is_not_success(self):
        async def executor(name, args):
            return {"success": True, "output": "noted"}

        brain = self._brain([
            {"kind": "action", "step": {
                "skill": "quick_note", "args": {"content": "a"}}},
            "this is not json",
            "still not json",
        ], executor)
        res = run(brain.handle("take a note", Ctx(), Session()))
        self.assertFalse(res.success)
        self.assertIn("unusable", res.response)
        self.assertEqual(len(res.steps), 1)

    def test_gateway_loss_mid_task_is_stated(self):
        async def executor(name, args):
            return {"success": True, "output": "noted"}

        brain = self._brain([
            {"kind": "action", "step": {
                "skill": "quick_note", "args": {"content": "a"}}},
            GatewayUnavailable("provider down"),
        ], executor)
        res = run(brain.handle("take a note", Ctx(), Session()))
        self.assertTrue(res.success)
        self.assertIn("Stopped early: planner unavailable.", res.response)

    def test_skills_block_descriptions_are_clipped(self):
        registry = ToolRegistry(learned_path=self.tmp / "learned_clip.json")
        registry.register(Tool(
            name="long_tool", description="word " * 200,
            script='return "ok"', args=[], is_learned=True))
        brain = AgentBrain(FakeGateway([]), registry, _tmp_store(self.tmp),
                           executor=self._noop)
        block = brain._skills_block(Ctx(), "do something")
        self.assertIn("long_tool", block)
        for line in block.splitlines():
            self.assertLess(len(line), 200, line)

    async def _noop(self, name, args):
        return {"success": True, "output": "ok"}


class TestLocalContextGuard(unittest.TestCase):
    """The local llama.cpp brain must not be asked for more tokens than fit."""

    def test_max_tokens_is_clamped_to_local_context(self):
        gateway = ModelGateway.__new__(ModelGateway)
        provider = Provider(name="local", base_url="http://127.0.0.1:8080/v1",
                            model="qwen2.5-0.5b-instruct")
        captured = {}

        def fake_post(url, payload, headers, timeout):
            captured.update(payload)
            return {"choices": [{"message": {"content": "{}"}}], "model": provider.model}

        messages = [{"role": "user", "content": "x" * 30000}]
        with patch("winbrow.brain.model_gateway._post_json", side_effect=fake_post):
            run(gateway._chat_with_provider(
                provider, messages, None, None, 0.1, 1200, 10))
        self.assertLess(captured["max_tokens"], 1200)
        self.assertGreaterEqual(captured["max_tokens"], 256)


class TestAgentToolBuilderBridge(unittest.TestCase):
    """agent.build_new_tool wraps the generator and speaks brain-speak."""

    def test_build_new_tool_success(self):
        from winbrow.agent import WinBrowAgent

        async def fake_generate(task, ctx, tiered=False):
            return {
                "success": True,
                "tool": SimpleNamespace(name="png2jpeg_tool"),
                "stdout": "converted 3 files",
            }

        agent = WinBrowAgent.__new__(WinBrowAgent)
        agent.generator = SimpleNamespace(generate_and_execute=fake_generate)
        out = run(agent.build_new_tool("convert png to jpeg", None))
        self.assertTrue(out["success"])
        self.assertIn("png2jpeg_tool", out["output"])
        self.assertIn("converted 3 files", out["output"])

    def test_build_new_tool_pending_permission(self):
        from winbrow.agent import WinBrowAgent

        async def fake_generate(task, ctx, tiered=False):
            return {
                "success": False,
                "tool": SimpleNamespace(name="disk_tool"),
                "permission": {"tier": "t3", "confirmation_id": "abc-1"},
                "reason": "Approval needed",
            }

        agent = WinBrowAgent.__new__(WinBrowAgent)
        agent.generator = SimpleNamespace(generate_and_execute=fake_generate)
        out = run(agent.build_new_tool("wipe something", None))
        self.assertFalse(out["success"])
        self.assertIn("permission-required", out["output"])
        self.assertIn("abc-1", out["output"])

    def test_build_new_tool_failure_reason_passes_through(self):
        from winbrow.agent import WinBrowAgent

        async def fake_generate(task, ctx, tiered=False):
            return {"success": False, "tool": None,
                    "error": "Safety Policy Rejection: rm"}

        agent = WinBrowAgent.__new__(WinBrowAgent)
        agent.generator = SimpleNamespace(generate_and_execute=fake_generate)
        out = run(agent.build_new_tool("rm the world", None))
        self.assertFalse(out["success"])
        self.assertIn("Could not build tool", out["output"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
