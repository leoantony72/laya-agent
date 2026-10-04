import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from winbrow import trace as trace_mod
from winbrow.agent import WinBrowAgent, _is_browser_intent
from winbrow.cli import main as cli_main
from winbrow.last_opened import ContextMemory
from winbrow.pshost import get_host
from winbrow.registry import ToolRegistry
from winbrow.router import CHAT, NEW_ACTION, STOP, WinBrowRouter
from winbrow.windows import WindowsContext, get_current_windows_context, _parse_volume_level


def laya_answer(choice, confidence=0.9, arg_choices=None):
    """Canned Laya predict() payload for mocked tool-choice tests."""
    answers = {
        "selected_tool": {
            "choice": choice,
            "confidence": confidence,
            "probabilities": {choice: confidence},
        }
    }
    for qid, val in (arg_choices or {}).items():
        answers[qid] = {"choice": val, "confidence": confidence}
    return {"answers": answers}


class FakeWorker:
    """Stand-in for LayaWorker: returns one canned answer (or raises)."""

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    async def apredict(self, text, questions, timeout):
        self.calls.append((text, questions, timeout))
        if self.error:
            raise self.error
        return self.payload


class TestWinBrowRegistryAndRouter(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.router = WinBrowRouter(self.registry)
        self.ctx = WindowsContext(
            active_app="Google Chrome",
            active_title="Google Chrome",
            running_apps=["chrome"],
            volume_level=50
        )

    def _mock_laya(self, payload=None, error=None):
        self.router._worker = FakeWorker(payload, error)

    # -- registry ----------------------------------------------------
    def test_registered_tools_count(self):
        tools = self.registry.all_tools()
        print(f"Total registered tools: {len(tools)}")
        self.assertEqual(len(tools), 41)

    def test_open_file_tool_exists(self):
        tool = self.registry.get("open_file")
        self.assertIsNotNone(tool)
        self.assertTrue(tool.description.strip())
        self.assertGreaterEqual(len(tool.examples), 3)

    # -- Laya-first tool choice (mocked model) ------------------------
    def test_laya_chooses_open_file_for_images_jpg(self):
        self._mock_laya(laya_answer("open_file", 0.92))
        route = asyncio.run(self.router.route("open images.jpg", self.ctx))
        self.assertEqual(route.tool_name, "open_file")
        self.assertEqual(route.args.get("target"), "images.jpg")
        self.assertEqual(route.tier, "laya")

    def test_laya_chooses_open_file_for_images_png(self):
        self._mock_laya(laya_answer("open_file", 0.9))
        route = asyncio.run(self.router.route("open the images.png file", self.ctx))
        self.assertEqual(route.tool_name, "open_file")
        self.assertEqual(route.args.get("target"), "images.png")

    def test_laya_chooses_open_file_from_downloads(self):
        self._mock_laya(laya_answer("open_file", 0.9))
        route = asyncio.run(self.router.route(
            "open the leoantony.pdf file from the downloads folder", self.ctx))
        self.assertEqual(route.tool_name, "open_file")
        self.assertEqual(route.args.get("target"), "leoantony.pdf")

    def test_laya_choice_beats_fallback(self):
        # "open downloads folder" would also match the fallback; Laya must win.
        self._mock_laya(laya_answer("open_folder", 0.93))
        route = asyncio.run(self.router.route("open downloads folder", self.ctx))
        self.assertEqual(route.tool_name, "open_folder")
        self.assertEqual(route.tier, "laya")

    def test_laya_unknown_tool_becomes_new_action(self):
        self._mock_laya(laya_answer("no_such_tool_xyz", 0.9))
        route = asyncio.run(self.router.route("do something weird", self.ctx))
        self.assertEqual(route.kind, NEW_ACTION)

    def test_laya_low_confidence_becomes_new_action(self):
        self._mock_laya(laya_answer("open_folder", 0.1))
        route = asyncio.run(self.router.route("maybe open something", self.ctx))
        self.assertEqual(route.kind, NEW_ACTION)

    def test_laya_chat_and_stop(self):
        self._mock_laya(laya_answer(CHAT, 0.95))
        route = asyncio.run(self.router.route("hello there", self.ctx))
        self.assertEqual(route.kind, CHAT)
        self._mock_laya(laya_answer(STOP, 0.95))
        route = asyncio.run(self.router.route("stop listening", self.ctx))
        self.assertEqual(route.kind, STOP)

    def test_laya_enum_args_answered(self):
        self._mock_laya(laya_answer(
            "toggle_dark_mode", 0.9,
            {"arg__toggle_dark_mode__mode": "dark"},
        ))
        route = asyncio.run(self.router.route("turn on dark mode", self.ctx))
        self.assertEqual(route.tool_name, "toggle_dark_mode")
        self.assertEqual(route.args.get("mode"), "dark")

    # -- fallback: only exact intents, never phrase-mapped tools -------
    def _fallback(self, text):
        tools = self.router.registry.available(self.ctx)
        return self.router._fallback_route(text, tools, time.perf_counter(), self.ctx)

    def test_fallback_volume_max(self):
        for cmd in ["set volume to max", "set the volume to max"]:
            route = self._fallback(cmd)
            self.assertEqual(route.tool_name, "volume_set_level")
            self.assertEqual(route.args.get("level"), "100")
            self.assertEqual(route.tier, "heuristic")

    def test_fallback_volume_digits(self):
        route = self._fallback("set volume to 40%")
        self.assertEqual(route.tool_name, "volume_set_level")
        self.assertEqual(route.args.get("level"), "40")

    def test_fallback_mute(self):
        route = self._fallback("mute the sound")
        self.assertEqual(route.tool_name, "system_volume")
        self.assertEqual(route.args.get("action"), "mute")

    def test_fallback_lock_screen(self):
        route = self._fallback("lock screen")
        self.assertEqual(route.tool_name, "lock_screen")

    def test_fallback_open_real_folder(self):
        route = self._fallback("open downloads folder")
        self.assertEqual(route.tool_name, "open_folder")

    def test_fallback_open_unresolvable_is_new_action(self):
        # The reported bug: no "open_folder anyway" fallback.
        route = self._fallback("open winbrow_no_such_file_xyz.jpg")
        self.assertEqual(route.kind, NEW_ACTION)

    def test_fallback_unknown_is_new_action(self):
        route = self._fallback("convert pdf to docx and extract tables")
        self.assertEqual(route.kind, NEW_ACTION)

    def test_timeout_falls_back_to_new_action(self):
        # Laya hanging must not route to open_folder; regression test.
        self._mock_laya(error=asyncio.TimeoutError("slow model"))
        route = asyncio.run(self.router.route("open winbrow_no_such_file_xyz.jpg", self.ctx))
        self.assertEqual(route.kind, NEW_ACTION)
        self.assertEqual(route.tier, "heuristic")

    def test_worker_error_falls_back(self):
        self._mock_laya(error=RuntimeError("Laya unavailable"))
        route = asyncio.run(self.router.route("set volume to max", self.ctx))
        self.assertEqual(route.tool_name, "volume_set_level")
        self.assertEqual(route.args.get("level"), "100")

    def test_timeout_configurable_via_env(self):
        with patch.dict(os.environ, {"LAYA_TIMEOUT_S": "7.5"}):
            r = WinBrowRouter(self.registry)
            self.assertEqual(r.timeout, 7.5)

    # -- unchanged behavior -------------------------------------------
    def test_parse_volume_level_free_text(self):
        self.assertEqual(_parse_volume_level("set the volume to max"), 100.0)
        self.assertEqual(_parse_volume_level("100"), 100.0)
        self.assertEqual(_parse_volume_level("set volume to 20"), 20.0)
        self.assertIsNone(_parse_volume_level("garbage with no level"))
        # bare "0" must not swallow "20"
        self.assertEqual(_parse_volume_level("set volume to 20"), 20.0)

    def test_browser_intent_ignores_tables(self):
        self.assertFalse(_is_browser_intent("convert pdf to docx and extract tables"))
        self.assertTrue(_is_browser_intent("open a new tab"))
        self.assertTrue(_is_browser_intent("close this tab"))

    # -- M0 instrumentation -------------------------------------------
    def test_trace_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            orig = trace_mod.TRACE_PATH
            trace_mod.TRACE_PATH = Path(tmp) / "t.jsonl"
            try:
                trace_mod.command_event(
                    utterance="probe", tier="laya", tool_name="current_time",
                    confidence=0.95, route_latency_ms=1.0, success=True,
                    total_elapsed_ms=12.0,
                )
                lines = (Path(tmp) / "t.jsonl").read_text(encoding="utf-8").strip().splitlines()
                self.assertEqual(len(lines), 1)
                rec = json.loads(lines[0])
                self.assertEqual(rec["utterance"], "probe")
                self.assertEqual(rec["tier"], "laya")
                self.assertIn("ts", rec)
            finally:
                trace_mod.TRACE_PATH = orig

    def test_persistent_ps_runs_script(self):
        res = asyncio.run(get_host().run('Write-Output "pshost-probe-ok"', timeout=30))
        self.assertTrue(res["success"])
        self.assertIn("pshost-probe-ok", res["stdout"])
        self.assertIn("elapsed_ms", res)

    def test_persistent_ps_reports_errors(self):
        res = asyncio.run(get_host().run("Get-Item Z:\\Nope\\Missing -ErrorAction Stop", timeout=30))
        self.assertFalse(res["success"])
        # host must survive a failed script
        res2 = asyncio.run(get_host().run('Write-Output "alive"', timeout=30))
        self.assertTrue(res2["success"])
        self.assertIn("alive", res2["stdout"])

    def test_cli_observe_runs(self):
        rc = cli_main(["observe", "--max-nodes", "5", "--depth", "1"])
        self.assertEqual(rc, 0)

    # -- fallback file-open (validated against disk, Downloads first) --
    def _fallback_mem(self, text, memory=None):
        tools = self.router.registry.available(self.ctx)
        return self.router._fallback_route(
            text, tools, time.perf_counter(), self.ctx, memory=memory)

    def test_fallback_open_downloads_folder_variants(self):
        for cmd in ["hey open the downloads folder",
                    "open downloads folder please",
                    "please open the downloads folder"]:
            route = self._fallback_mem(cmd)
            self.assertEqual(route.tool_name, "open_folder", cmd)
            self.assertEqual(route.args.get("folder"), "downloads", cmd)

    def test_fallback_open_file_resolves_on_disk(self):
        with patch("winbrow.router._resolve_file",
                   return_value="C:\\Users\\USER\\Downloads\\leoantony.png"):
            route = self._fallback_mem("open leoantony.png file")
        self.assertEqual(route.tool_name, "open_file")
        self.assertEqual(route.args.get("target"), "leoantony.png")
        self.assertEqual(route.args.get("app"), "")

    def test_fallback_open_file_with_app(self):
        with patch("winbrow.router._resolve_file",
                   return_value="C:\\Users\\USER\\Downloads\\leoantony.pdf"):
            route = self._fallback_mem("open leoantony.pdf in chrome")
        self.assertEqual(route.tool_name, "open_file")
        self.assertEqual(route.args.get("target"), "leoantony.pdf")
        self.assertEqual(route.args.get("app"), "chrome")

    def test_fallback_open_file_extension_must_match(self):
        # "leoantony.png" must not open "Leoantony.pdf".
        with patch("winbrow.router._resolve_file",
                   return_value="C:\\Users\\USER\\Desktop\\Resume\\Leoantony.pdf"):
            route = self._fallback_mem("open leoantony.png file")
        self.assertEqual(route.kind, NEW_ACTION)
        self.assertIsNone(route.tool_name)

    def test_fallback_open_missing_file_is_new_action(self):
        with patch("winbrow.router._resolve_file", return_value=None):
            route = self._fallback_mem("open images.jpg")
        self.assertEqual(route.kind, NEW_ACTION)
        self.assertIsNone(route.tool_name)

    def test_fallback_that_file_uses_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(tmp, "remember_me.pdf")
            Path(real).write_bytes(b"%PDF-1.4 probe")
            mem = ContextMemory(path=Path(tmp) / "mem.json")
            mem.record_open("file", real, "remember_me.pdf")
            route = self._fallback_mem("open that file", memory=mem)
        self.assertEqual(route.tool_name, "open_file")
        self.assertEqual(route.args.get("target"), "remember_me.pdf")

    def test_fallback_that_file_without_memory_is_new_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = ContextMemory(path=Path(tmp) / "mem.json")
            route = self._fallback_mem("open that file", memory=mem)
        self.assertEqual(route.kind, NEW_ACTION)

    # -- context memory recording --------------------------------------
    def test_memory_record_open_result(self):
        agent = WinBrowAgent()
        with tempfile.TemporaryDirectory() as tmp:
            agent.memory = ContextMemory(path=Path(tmp) / "mem.json")
            with patch("winbrow.agent.resolve_file",
                       return_value="C:\\Users\\USER\\Downloads\\x.png"):
                agent._record_open_result("open_file", {"target": "x.png"}, True)
            self.assertEqual(
                agent.memory.last_file, "C:\\Users\\USER\\Downloads\\x.png")
            # failures are never recorded
            with patch("winbrow.agent.resolve_file",
                       return_value="C:\\Users\\USER\\Downloads\\y.png"):
                agent._record_open_result("open_file", {"target": "y.png"}, False)
            self.assertEqual(
                agent.memory.last_file, "C:\\Users\\USER\\Downloads\\x.png")

    def test_memory_record_open_folder(self):
        agent = WinBrowAgent()
        with tempfile.TemporaryDirectory() as tmp:
            agent.memory = ContextMemory(path=Path(tmp) / "mem.json")
            with patch("winbrow.agent.resolve_folder",
                       return_value="C:\\Users\\USER\\Downloads"):
                agent._record_open_result(
                    "open_folder", {"folder": "downloads"}, True)
            self.assertEqual(
                agent.memory.last_folder, "C:\\Users\\USER\\Downloads")

if __name__ == "__main__":
    unittest.main()
