"""
M2 Tests — App Adapters + Procedural Memory (capture/replay)
=============================================================
Logic-level tests only:
  • SkillMemory: save/match/replay-support/delete/persistence/caps
  • Adapter ops registered uniformly, dispatcher arg validation, app grouping
  • Agent fast-path replay (<2s, no LLM) and post-brain capture
  • Server /api/skills endpoints

Real-world COM/CLI execution is gated behind WINBROW_M2_E2E=1.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))

# Isolate procedural memory BEFORE winbrow import
_SKILLS_DIR = tempfile.mkdtemp(prefix="winbrow_skills_m2_")
os.environ["WINBROW_SKILLS_DIR"] = _SKILLS_DIR

from winbrow.brain.model_gateway import (GatewayResponse, GatewayUnavailable,
                                         ModelGateway)
from winbrow.memory.skills import (MAX_STEPS, MATCH_THRESHOLD, SkillMemory,
                                   SkillRecord, SkillStep,
                                   MissingSkillParameters, match_score,
                                   parameterize_steps, suggest_skill_name)
from winbrow.adapters.base import AdapterOp
from winbrow.adapters.manager import (ADAPTER_IMPL, ADAPTER_TOOLS,
                                      adapter_for_app, execute_adapter_tool,
                                      get_adapter_tools)

def run(coro):
    return asyncio.run(coro)


class FakeGateway(ModelGateway):
    def __init__(self, responses=None):
        self.providers = [SimpleNamespace(name="fake", model="fake-1")]
        self._responses = list(responses or [])
        self.calls = 0

    async def chat(self, messages, tools=None, json_schema=None,
                   temperature=0.2, max_tokens=900, timeout=None):
        import json
        self.calls += 1
        if not self._responses:
            return GatewayResponse(content=json.dumps(
                {"kind": "chat", "steps": [], "response": "()"}),
                provider="fake", model="fake-1")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return GatewayResponse(
            content=item if isinstance(item, str) else json.dumps(item),
            provider="fake", model="fake-1",
        )


class TestSkillMemory(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="skills_case_")
        self.store = SkillMemory(Path(self.dir) / "skills.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def steps(self, n=2):
        return [{"skill": f"tool_{i}", "args": {"k": str(i)}} for i in range(n)]

    def test_save_and_list(self):
        rec = self.store.save("Open Code", "open D:\\code in vscode",
                              self.steps(2), source="brain")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.id, "open-code")
        self.assertEqual(len(self.store), 1)
        self.assertEqual(len(rec.steps), 2)
        self.assertEqual(rec.steps[0].skill, "tool_0")
        self.assertEqual(rec.source, "brain")

    def test_dedup_same_slug_updates(self):
        self.store.save("x", "open code", self.steps(2))
        rec2 = self.store.save("x", "open code again", self.steps(3))
        self.assertEqual(len(self.store), 1)
        self.assertEqual(len(rec2.steps), 3)
        self.assertEqual(rec2.utterance, "open code again")

    def test_find_match_hit_and_threshold(self):
        self.store.save("ext install", "install the python extension in vscode", self.steps(1))
        hit = self.store.find_match("install the python extension in vscode")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0].id, "ext-install")
        self.assertGreaterEqual(hit[1], MATCH_THRESHOLD)
        # wordy-but-similar utterance still matches
        hit2 = self.store.find_match(
            "hey winbrow please install the python extension in vscode")
        self.assertIsNotNone(hit2)
        # unrelated utterance must NOT match
        self.assertIsNone(self.store.find_match("what time is it now"))

    def test_delete_and_mark_used(self):
        self.store.save("x", "open code", self.steps(1))
        self.store.mark_used("x")
        rec = self.store.get("x")
        self.assertEqual(rec.uses, 1)
        self.assertTrue(self.store.delete("x"))
        self.assertIsNone(self.store.get("x"))
        self.assertFalse(self.store.delete("x"))

    def test_persistence_across_instances(self):
        self.store.save("open code", "open code", self.steps(2))
        again = SkillMemory(Path(self.dir) / "skills.json")
        self.assertEqual(len(again), 1)
        self.assertEqual(again.find_match("open code")[0].id, "open-code")

    def test_max_steps_cap(self):
        big = [{"skill": f"t{i}", "args": {}} for i in range(MAX_STEPS + 5)]
        rec = self.store.save("big", "do the big flow", big)
        self.assertEqual(len(rec.steps), MAX_STEPS)

    def test_empty_steps_rejected(self):
        self.assertIsNone(self.store.save("n", "utterance", []))
        self.assertEqual(len(self.store), 0)

    def test_match_score(self):
        self.assertEqual(match_score("open code", "open code"), 1.0)
        self.assertLess(match_score("open code", "import os"), 1.0)
        self.assertEqual(match_score("", "open"), 0.0)

    def test_proposed_skills_need_approval_before_matching(self):
        rec = self.store.save("vscode flow", "create a go project in vscode",
                              self.steps(2), status="proposed")
        self.assertEqual(rec.status, "proposed")
        self.assertIsNone(self.store.find_match("create a go project in vscode"))
        self.assertTrue(self.store.approve(rec.id))
        hit = self.store.find_match("create a go project in vscode")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0].id, rec.id)
        self.assertEqual(self.store.proposed(), [])

    def test_trigger_phrases_broaden_matching(self):
        rec = self.store.save(
            "vscode-go-project", "set up chicken-go",
            self.steps(2), triggers=["set up a go workspace in vscode"])
        hit = self.store.find_match("open vscode and set up a go workspace")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0].id, rec.id)

    def test_parameterized_steps_render_from_new_values(self):
        rec = self.store.save(
            "vscode-go-project", "create go project at C:\\proj",
            [{"skill": "vscode_open", "args": {"target": "{{folder_path}}"}},
             {"skill": "mcp_filesystem_write_file",
              "args": {"path": "{{folder_path | basename}}\\main.go",
                       "content": "package main"}}],
            parameters=[{"name": "folder_path",
                         "description": "Project folder", "required": True}])
        steps = rec.render_steps({"folder_path": "D:\\work\\other-go"})
        self.assertEqual(steps[0]["args"]["target"], "D:\\work\\other-go")
        self.assertEqual(steps[1]["args"]["path"], "other-go\\main.go")
        with self.assertRaises(MissingSkillParameters):
            rec.render_steps({})

    def test_parameterize_steps_only_touches_request_values(self):
        steps = [
            {"skill": "vscode_open",
             "args": {"target": "C:\\Users\\USER\\Desktop\\chicken-go"}},
            {"skill": "mcp_filesystem_write_file",
             "args": {"path": "C:\\Users\\USER\\Desktop\\chicken-go\\main.go",
                      "content": "package main"}},
            {"skill": "app_focus_or_launch", "args": {"app_name": "code"}},
        ]
        out, params = parameterize_steps(
            steps,
            "open vscode and create C:\\Users\\USER\\Desktop\\chicken-go with main.go")
        self.assertEqual(out[0]["args"]["target"], "{{folder_path}}")
        self.assertEqual(out[1]["args"]["path"], "{{file_path}}")
        self.assertEqual(out[2]["args"]["app_name"], "code")
        self.assertEqual([p.name for p in params], ["folder_path", "file_path"])

    def test_parameterize_upload_path_matches_fuzzy_filename(self):
        steps = [{
            "skill": "browser_upload_file",
            "args": {"file_path": "C:\\Users\\USER\\Downloads\\LDM seminar.pptx"},
        }]
        out, params = parameterize_steps(
            steps, "can you upload the LDMseminar pptx file to Google Drive")
        self.assertEqual(out[0]["args"]["file_path"], "{{file_path}}")
        self.assertEqual([p.name for p in params], ["file_path"])

    def test_upload_skill_name_is_generic(self):
        steps = [
            {"skill": "find_files", "args": {"search_term": "LDMseminar"}},
            {"skill": "browser_navigate",
             "args": {"url": "https://drive.google.com"}},
            {"skill": "browser_upload_file",
             "args": {"file_path": "C:\\Users\\USER\\Downloads\\LDM seminar.pptx"}},
        ]
        name = suggest_skill_name(
            steps, "can you upload the LDMseminar pptx file in the google drive")
        self.assertEqual(name, "Upload a file to Google Drive")
        self.assertNotIn("LDMseminar", name)

    def test_non_upload_skill_names_are_intent_based(self):
        self.assertEqual(
            suggest_skill_name([
                {"skill": "find_files", "args": {"search_term": "x"}},
                {"skill": "mcp_filesystem_move_file", "args": {}},
            ], "move x to papers"),
            "Move a file to a folder",
        )
        self.assertEqual(
            suggest_skill_name([
                {"skill": "read_document", "args": {}},
                {"skill": "create_text_file", "args": {}},
            ], "summarize x"),
            "Create a document from a file",
        )


class TestAdapterOps(unittest.TestCase):
    def test_sixteen_ops_unique_names(self):
        names = [op.tool_name for op in ADAPTER_TOOLS]
        self.assertEqual(len(names), 16)
        self.assertEqual(len(names), len(set(names)))

    def test_ops_have_fns_and_args(self):
        for op in ADAPTER_TOOLS:
            self.assertIsNotNone(op.fn, op.tool_name)
            for spec in op.args.values():
                self.assertIn("instructions", spec)

    def test_get_adapter_tools_shape(self):
        tools = get_adapter_tools()
        self.assertEqual(len(tools), len(ADAPTER_TOOLS))
        for t in tools:
            self.assertEqual(t.impl, ADAPTER_IMPL)
            self.assertEqual(t.script, "")
            self.assertTrue(t.description)
            arg_names = {a.name for a in t.args}
            op = next(o for o in ADAPTER_TOOLS if o.tool_name == t.name)
            self.assertEqual(arg_names, set(op.args.keys()))

    def test_app_grouping(self):
        self.assertEqual(len(adapter_for_app("vscode")), 4)
        self.assertEqual(len(adapter_for_app("photoshop")), 3)
        self.assertEqual(len(adapter_for_app("notepad")), 2)
        self.assertEqual(len(adapter_for_app("paint")), 3)
        self.assertEqual(len(adapter_for_app("office")), 2)
        self.assertEqual(len(adapter_for_app("aftereffects")), 2)
        self.assertEqual(adapter_for_app("nosuchapp"), [])

    def test_dispatcher_unknown_op(self):
        res = run(execute_adapter_tool("vscode_nonexistent", {}))
        self.assertFalse(res["success"])

    def test_dispatcher_required_args(self):
        res = run(execute_adapter_tool("vscode_open", {}))
        self.assertFalse(res["success"])
        self.assertIn("target", res["output"])
        res2 = run(execute_adapter_tool("photoshop_save_as", {}))
        self.assertFalse(res2["success"])

    def test_dispatch_wrong_type_arg(self):
        res = run(execute_adapter_tool("vscode_open", {"target": "D:\\definitely\\not\\here\\x"}))
        self.assertFalse(res["success"])

    def test_vscode_open_uses_detached_start(self):
        """Never block on the `code` shim: GUI children hold its pipes open."""
        from unittest.mock import patch
        from winbrow.adapters import vscode as vscode_adapter

        captured = {}

        async def fake_run_powershell(script, timeout=30):
            captured["script"] = script
            return {"success": True, "stdout": "Launched VS Code", "stderr": ""}

        with patch("winbrow.windows.run_powershell", side_effect=fake_run_powershell):
            res = run(vscode_adapter._launch_detached(
                "C:\\Users\\USER\\Desktop\\proj"))
        self.assertTrue(res["success"])
        self.assertIn("Start-Process", captured["script"])
        self.assertIn("Code.exe", captured["script"])
        self.assertIn("Desktop\\proj", captured["script"])

    def test_sync_powershell_captures_output(self):
        from winbrow.windows import _run_powershell_sync
        import time as _time
        t0 = _time.perf_counter()
        res = run(_run_powershell_sync("Write-Output 'hello-sync'", 15, t0))
        self.assertTrue(res["success"], res)
        self.assertIn("hello-sync", res["stdout"])

    def test_sync_powershell_timeout_returns(self):
        from winbrow.windows import _run_powershell_sync
        import time as _time
        t0 = _time.perf_counter()
        res = run(_run_powershell_sync("Start-Sleep -Seconds 30", 1, t0))
        self.assertFalse(res["success"])
        self.assertIn("timed out", res["stderr"])
        self.assertLess(_time.perf_counter() - t0, 20)


class TestShellTool(unittest.TestCase):
    def test_execute_parses_output_and_exit_code(self):
        from unittest.mock import patch
        from winbrow.shell import execute_shell_tool

        captured = {}

        async def fake_run(script, timeout=30, t0=0.0):
            captured["script"] = script
            captured["timeout"] = timeout
            return {
                "success": True,
                "stdout": "main.go: ok\n__WINBROW_EXIT__:0\n",
                "stderr": "",
                "elapsed_ms": 12.5,
            }

        with patch("winbrow.shell._run_powershell_sync", side_effect=fake_run):
            out = run(execute_shell_tool(
                "run_command",
                {"command": "go build ./...", "cwd": str(Path.cwd()), "timeout": "30"},
            ))
        self.assertTrue(out["success"], out)
        self.assertEqual(out["exit_code"], 0)
        self.assertIn("main.go: ok", out["output"])
        self.assertIn("go build ./...", captured["script"])
        self.assertIn("Push-Location", captured["script"])
        self.assertEqual(captured["timeout"], 30)

    def test_execute_reports_nonzero_exit(self):
        from unittest.mock import patch
        from winbrow.shell import execute_shell_tool

        async def fake_run(script, timeout=30, t0=0.0):
            return {
                "success": True,
                "stdout": "compile error\n__WINBROW_EXIT__:2\n",
                "stderr": "",
                "elapsed_ms": 9.0,
            }

        with patch("winbrow.shell._run_powershell_sync", side_effect=fake_run):
            out = run(execute_shell_tool(
                "run_command",
                {"command": "go build ./...", "cwd": str(Path.cwd())},
            ))
        self.assertFalse(out["success"])
        self.assertEqual(out["exit_code"], 2)
        self.assertIn("compile error", out["output"])

    def test_execute_real_command(self):
        from winbrow.shell import execute_shell_tool

        out = run(execute_shell_tool(
            "run_command",
            {"command": "Write-Output 'shell-live'", "cwd": str(Path.cwd())},
        ))
        self.assertTrue(out["success"], out)
        self.assertEqual(out["exit_code"], 0)
        self.assertIn("shell-live", out["output"])

    def test_execute_blocks_policy_violation(self):
        from winbrow.shell import execute_shell_tool

        out = run(execute_shell_tool(
            "run_command", {"command": "Format-Volume -DriveLetter D"},
        ))
        self.assertFalse(out["success"])
        self.assertIn("blocked by policy", out["output"])

    def test_real_go_build(self):
        """The live failure case: compile a Go web server in its folder."""
        if not shutil.which("go"):
            self.skipTest("Go toolchain not installed")
        from winbrow.shell import execute_shell_tool

        tmp = Path(tempfile.mkdtemp(prefix="winbrow_go_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "main.go").write_text(
            'package main\n\nimport (\n\t"fmt"\n\t"net/http"\n)\n\n'
            'func main() {\n'
            '\thttp.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {\n'
            '\t\tfmt.Fprintln(w, "hello")\n'
            '\t})\n'
            '\thttp.ListenAndServe(":8080", nil)\n'
            '}\n',
            encoding="utf-8",
        )
        # No go.mod yet: `go build ./...` fails until the module is initialized.
        failed = run(execute_shell_tool(
            "run_command",
            {"command": "go build ./...", "cwd": str(tmp), "timeout": "60"},
        ))
        self.assertFalse(failed["success"])
        self.assertNotEqual(failed["exit_code"], 0)

        init = run(execute_shell_tool(
            "run_command",
            {"command": "go mod init example.com/webserver",
             "cwd": str(tmp), "timeout": "60"},
        ))
        self.assertTrue(init["success"], init)

        built = run(execute_shell_tool(
            "run_command",
            {"command": "go build ./...", "cwd": str(tmp), "timeout": "60"},
        ))
        self.assertTrue(built["success"], built)
        self.assertEqual(built["exit_code"], 0)
        self.assertTrue(any(tmp.glob("*.exe")), built)


class TestFileWrites(unittest.TestCase):
    def test_write_named_text_file_is_deterministic(self):
        from winbrow.files import write_named_text_file

        tmp = Path(tempfile.mkdtemp(prefix="winbrow_files_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        out = write_named_text_file(
            {"filename": "watchlist.md", "content": "# Watchlist\n"}, base_dir=tmp)
        self.assertTrue(out["success"], out)
        self.assertEqual((tmp / "watchlist.md").read_text(encoding="utf-8"),
                         "# Watchlist\n")

    def test_write_named_text_file_defaults_extension_and_rejects_paths(self):
        from winbrow.files import write_named_text_file

        tmp = Path(tempfile.mkdtemp(prefix="winbrow_files_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        out = write_named_text_file({"filename": "note", "content": "hi"}, base_dir=tmp)
        self.assertTrue(out["success"], out)
        self.assertTrue((tmp / "note.txt").exists())

        bad = write_named_text_file({"filename": "..\\escape.txt", "content": "x"},
                                    base_dir=tmp)
        self.assertFalse(bad["success"])
        self.assertIn("path separators", bad["output"])


class TestBrowserTools(unittest.TestCase):
    class FakeBrowser:
        def __init__(self, text="repo one 58,234 stars\nrepo two 3,061 stars"):
            self.text = text
            self.navigated = []
            self.clicked = []
            self.typed = []
            self.file_inputs = set()
            self.uploads = []

        async def navigate_to(self, url, new_tab=False):
            self.navigated.append((url, new_tab))
            return {"success": True, "url": url}

        async def execute_js(self, code):
            return {
                "success": True,
                "value": {
                    "title": "Trending",
                    "url": "https://github.com/trending/python",
                    "text": self.text,
                    "links": [
                        {"text": "owner / repo", "href": "https://github.com/owner/repo"},
                    ],
                },
            }

        async def read_page_text(self):
            return {"success": False}

        async def get_current_url(self):
            return {"url": "https://github.com/trending/python"}

        async def click_element(self, selector):
            self.clicked.append(selector)
            return {"success": True, "output": "clicked"}

        async def type_in_element(self, selector, text):
            self.typed.append((selector, text))
            return {"success": True, "output": "typed"}

        async def is_file_input(self, selector):
            return selector in self.file_inputs

        async def set_file_input(self, selector, path):
            self.uploads.append((selector, path))
            return {"success": True, "output": "uploaded"}

    def test_tools_registered_with_browser_impl(self):
        from winbrow.browser_tools import BROWSER_IMPL, get_browser_tools
        from winbrow.permissions import tool_tier

        tools = {t.name: t for t in get_browser_tools()}
        self.assertEqual(set(tools), {
            "browser_navigate", "browser_read_page", "browser_read_full",
            "browser_get_accessibility_tree",
            "browser_interact", "browser_wait", "browser_upload_file",
        })
        self.assertTrue(all(t.impl == BROWSER_IMPL for t in tools.values()))
        self.assertEqual(tool_tier("browser_read_page"), "t0")
        self.assertEqual(tool_tier("browser_read_full"), "t0")
        self.assertEqual(tool_tier("browser_get_accessibility_tree"), "t0")
        self.assertEqual(tool_tier("browser_interact"), "t1")
        self.assertEqual(tool_tier("browser_upload_file"), "t1")

    def test_navigate_returns_page_state(self):
        from winbrow.browser_tools import execute_browser_tool

        browser = self.FakeBrowser()
        out = run(execute_browser_tool(
            "browser_navigate", {"url": "github.com/trending/python"}, browser=browser))
        self.assertTrue(out["success"], out)
        self.assertEqual(browser.navigated, [("https://github.com/trending/python", False)])
        self.assertIn("58,234 stars", out["output"])
        self.assertIn("owner / repo", out["output"])
        self.assertLess(out["output"].index("PAGE TEXT"),
                        out["output"].index("LINKS"))
        self.assertEqual(out["method"], "cdp")

    def test_read_page_uses_current_page_and_truncates(self):
        from winbrow.browser_tools import execute_browser_tool

        browser = self.FakeBrowser(text="A" * 6000)
        out = run(execute_browser_tool(
            "browser_read_page", {"max_chars": "1500"}, browser=browser))
        self.assertTrue(out["success"], out)
        self.assertIn("truncated", out["output"])
        self.assertLess(len(out["output"]), 4000)

    def test_read_full_returns_text_and_filtered_elements(self):
        from winbrow.browser_tools import execute_browser_tool

        class FullBrowser(self.FakeBrowser):
            async def execute_js(self, code):
                if "data-winbrow-id" in code:
                    return {
                        "success": True,
                        "value": {
                            "title": "Trending",
                            "url": "https://github.com/trending/python",
                            "elements": [
                                {"role": "button", "label": "Upload file",
                                 "selector": '[data-winbrow-id="w0"]'},
                                {"role": "link", "label": "owner / repo",
                                 "selector": '[data-winbrow-id="w1"]'},
                            ],
                        },
                    }
                return await super().execute_js(code)

        browser = FullBrowser()
        out = run(execute_browser_tool(
            "browser_read_full", {"target": "upload"}, browser=browser))
        self.assertTrue(out["success"], out)
        self.assertEqual(out["element_count"], 1)
        self.assertEqual(out["target"], "upload")
        self.assertIn("58,234 stars", out["output"])
        self.assertIn("PAGE TEXT", out["output"])
        self.assertIn("INTERACTIVE ELEMENTS: matching 'upload'", out["output"])
        self.assertIn('[data-winbrow-id="w0"]', out["output"])
        self.assertNotIn('[data-winbrow-id="w1"]', out["output"])

    def test_accessibility_tree_filters_by_target_and_chars(self):
        from winbrow.browser_tools import execute_browser_tool

        class TreeBrowser(self.FakeBrowser):
            async def execute_js(self, code):
                return {
                    "success": True,
                    "value": {
                        "title": "Trending",
                        "url": "https://github.com/trending/python",
                        "elements": [
                            {"role": "button", "label": "Upload file",
                             "selector": '[data-winbrow-id="w0"]'},
                            {"role": "link", "label": "owner / repo",
                             "selector": '[data-winbrow-id="w1"]'},
                        ],
                    },
                }

        browser = TreeBrowser()
        out = run(execute_browser_tool(
            "browser_get_accessibility_tree",
            {"target": "upload", "max_chars": "1000"}, browser=browser))
        self.assertTrue(out["success"], out)
        self.assertEqual(out["element_count"], 1)
        self.assertEqual(out["total_elements"], 2)
        self.assertNotIn("owner / repo", out["output"])

        async def dense_js(code):
            return {
                "success": True,
                "value": {
                    "title": "Dense",
                    "url": "https://example.com",
                    "elements": [
                        {"role": "button", "label": "Button " + "x" * 100,
                         "selector": f'[data-winbrow-id="w{i}"]'}
                        for i in range(200)
                    ],
                },
            }

        dense = TreeBrowser()
        dense.execute_js = dense_js
        truncated = run(execute_browser_tool(
            "browser_get_accessibility_tree", {"max_chars": "1200"}, browser=dense))
        self.assertTrue(truncated["_truncated"], truncated)
        self.assertIn("... [truncated]", truncated["output"])

    def test_interact_selector_clicks_and_types(self):
        from winbrow.browser_tools import execute_browser_tool

        browser = self.FakeBrowser()
        clicked = run(execute_browser_tool(
            "browser_interact", {"selector": "#repo-link"}, browser=browser))
        self.assertTrue(clicked["success"], clicked)
        self.assertEqual(browser.clicked, ["#repo-link"])
        self.assertEqual(clicked["action"], "click")

        typed = run(execute_browser_tool(
            "browser_interact",
            {"selector": "#search", "value": "python"}, browser=browser))
        self.assertTrue(typed["success"], typed)
        self.assertEqual(browser.typed, [("#search", "python")])
        self.assertEqual(typed["action"], "type")

    def test_interact_selector_uploads_file(self):
        from winbrow.browser_tools import execute_browser_tool

        browser = self.FakeBrowser()
        browser.file_inputs.add("#file")
        path = r"D:\Docs\LDM seminar.pptx"
        out = run(execute_browser_tool(
            "browser_interact", {"selector": "#file", "value": path}, browser=browser))
        self.assertTrue(out["success"], out)
        self.assertEqual(browser.uploads, [("#file", path)])
        self.assertEqual(browser.typed, [])
        self.assertEqual(out["action"], "upload")

    def test_wait_returns_state(self):
        from winbrow.browser_tools import execute_browser_tool

        browser = self.FakeBrowser()
        out = run(execute_browser_tool(
            "browser_wait", {"seconds": "0"}, browser=browser))
        self.assertTrue(out["success"], out)
        self.assertIn("Trending", out["output"])

    def test_browser_operation_times_out_instead_of_hanging(self):
        from unittest.mock import patch
        from winbrow.browser_tools import execute_browser_tool

        class SlowBrowser(self.FakeBrowser):
            async def navigate_to(self, url, new_tab=False):
                await asyncio.sleep(1)

        with patch("winbrow.browser_tools._BROWSER_OP_TIMEOUT_S", 0.05):
            out = run(execute_browser_tool(
                "browser_navigate", {"url": "https://example.com"},
                browser=SlowBrowser()))
        self.assertFalse(out["success"])
        self.assertTrue(out.get("timeout"))
        self.assertIn("timed out", out["output"])

    def test_brain_history_keeps_browser_evidence(self):
        from winbrow.brain.agent_loop import BrainStepResult, _history_block

        block = _history_block([BrainStepResult(
            "browser_read_page", {}, True, "repo evidence " * 500)])
        self.assertGreater(len(block), 400)
        self.assertLessEqual(len(block), 3600)
        self.assertIn("repo evidence", block)

    def test_browser_turns_get_tighter_reasoning_and_brevity(self):
        from winbrow.brain.model_gateway import (
            PLANNER_CALL_CONFIG,
            REASONING_MAX_TOKENS,
            get_reasoning_config,
        )
        from winbrow.brain.prompt import BROWSER_BREVITY, build_planner_message

        browser_history = [
            {"skill": "browser_navigate"},
            {"skill": "browser_read_full"},
            {"skill": "browser_interact"},
        ]
        self.assertEqual(
            get_reasoning_config(browser_history),
            {"max_tokens": 8000, "reasoning_max": 600},
        )
        self.assertEqual(
            get_reasoning_config([{"skill": "open_file"}]),
            {
                "max_tokens": PLANNER_CALL_CONFIG["max_tokens"],
                "reasoning_max": REASONING_MAX_TOKENS,
            },
        )
        message = build_planner_message(
            "upload the file", [{"skill": "browser_read_full"}])
        self.assertIn(BROWSER_BREVITY, message)

    def test_brain_history_caps_long_browser_loops(self):
        from winbrow.brain.agent_loop import BrainStepResult, _history_block

        steps = [BrainStepResult(
            "browser_read_page", {}, True, f"page {i} " + "x" * 4000)
            for i in range(10)]
        block = _history_block(steps)
        self.assertLessEqual(len(block), 14_500)
        self.assertIn("page 9", block)

    def test_annotate_navigate_flags_wrong_destination(self):
        from winbrow.brain.agent_loop import annotate_result

        raw = {"success": True, "url": "https://github.com/dashboard",
               "title": "GitHub"}
        out = annotate_result(
            "browser_navigate",
            {"url": "https://github.com/trending/python"}, raw)
        self.assertFalse(out["_nav_matched"])
        self.assertIn("intended", out["_warning"])

    def test_annotate_navigate_accepts_normalized_url(self):
        from winbrow.brain.agent_loop import annotate_result

        raw = {"success": True,
               "url": "http://www.github.com/trending/python/?tab=stars",
               "title": "Trending"}
        out = annotate_result(
            "browser_navigate",
            {"url": "https://github.com/trending/python"}, raw)
        self.assertTrue(out["_nav_matched"])
        self.assertNotIn("_warning", out)

    def test_annotate_failure_is_surfaced(self):
        from winbrow.brain.agent_loop import annotate_result

        out = annotate_result(
            "browser_read_page", {},
            {"success": False, "output": "CDP unavailable"})
        self.assertIn("This step failed", out["_warning"])

    def test_brain_history_surfaces_verification_warning(self):
        from winbrow.brain.agent_loop import BrainStepResult, _history_block

        block = _history_block([BrainStepResult(
            "browser_navigate", {"url": "https://github.com/trending"},
            True, "Navigated", warning="Landed on the dashboard, not trending.")])
        self.assertIn("_warning", block)
        self.assertIn("dashboard", block)


class TestClickElement(unittest.TestCase):
    """Element clicks must use real CDP input, not only a synthetic JS click."""

    class FakeController:
        def __init__(self, rect):
            self.rect = rect
            self.clicks = []
            self.js_calls = []

        async def execute_js(self, code):
            self.js_calls.append(code)
            if "getBoundingClientRect" in code:
                return {"success": True, "value": self.rect}
            return {"success": True, "value": True}

        async def click_at(self, x, y):
            self.clicks.append((x, y))
            return {"success": True, "method": "cdp_chrome"}

    def test_clicks_element_center_with_trusted_input(self):
        from winbrow.browser_task import BrowserController

        browser = self.FakeController({
            "x": 120.4, "y": 33.6, "width": 40, "height": 20})
        out = run(BrowserController.click_element(
            browser, '[data-winbrow-id="w15"]'))
        self.assertTrue(out["success"], out)
        self.assertEqual(browser.clicks, [(120, 34)])
        self.assertIn("trusted CDP", out["output"])

    def test_missing_element_is_a_failure(self):
        from winbrow.browser_task import BrowserController

        browser = self.FakeController(None)
        out = run(BrowserController.click_element(browser, "#missing"))
        self.assertFalse(out["success"])
        self.assertIn("not found", out["error"])
        self.assertEqual(browser.clicks, [])


class TestBrowserTaskTabs(unittest.TestCase):
    """A browser task must open its own tab, never reuse the user's tab."""

    def _controller(self):
        from unittest.mock import AsyncMock
        from winbrow.browser_task import BrowserController

        browser = BrowserController()
        browser._tabs = AsyncMock(return_value=(
            "ws",
            [
                {"id": "user-tab", "url": "https://mail.google.com", "title": "Inbox"},
                {"id": "task-tab", "url": "https://github.com", "title": "GitHub"},
            ],
        ))
        browser._open_tab = AsyncMock(
            side_effect=self._open_tab_side_effect(browser))
        browser._activate_tab = AsyncMock()
        return browser

    @staticmethod
    def _open_tab_side_effect(browser):
        async def _open_tab(url):
            # Mirror the real _open_tab: the new tab becomes the task tab.
            browser._current_tab_id = "task-tab"
            return {"id": "task-tab", "url": url}

        return _open_tab

    @staticmethod
    def _fake_page_conn(browser):
        from contextlib import asynccontextmanager

        class _Conn:
            async def call(self, *args, **kwargs):
                return {"result": {}}

        @asynccontextmanager
        async def page_conn(tab):
            yield _Conn()

        browser._page_conn = page_conn

    def test_click_at_intercepts_native_file_chooser(self):
        from contextlib import asynccontextmanager

        browser = self._controller()
        calls = []

        class _Conn:
            async def call(self, method, params=None, timeout=None):
                calls.append((method, params))
                return {"result": {}}

        @asynccontextmanager
        async def page_conn(tab):
            yield _Conn()

        browser._page_conn = page_conn
        out = run(browser.click_at(10, 20))
        self.assertTrue(out["success"], out)
        self.assertIn(
            ("Page.setInterceptFileChooserDialog", {"enabled": True}),
            calls,
        )

    def test_first_navigation_of_task_opens_a_new_tab(self):
        from unittest.mock import patch

        browser = self._controller()
        self._fake_page_conn(browser)
        browser.begin_task()
        with patch("winbrow.browser_task._cdp_available", return_value=True):
            out = run(browser.navigate_to("https://github.com/trending"))

        self.assertTrue(out["success"], out)
        self.assertEqual(out["method"], "cdp_navigate")
        self.assertEqual(out["tab"], "new")
        browser._open_tab.assert_awaited_once_with("about:blank")
        self.assertEqual(browser._agent_tab_id, "task-tab")
        self.assertEqual(browser._current_tab_id, "task-tab")
        self.assertFalse(browser._task_tab_pending)

    def test_later_navigation_in_task_reuses_its_tab(self):
        from unittest.mock import patch

        browser = self._controller()
        self._fake_page_conn(browser)
        browser.begin_task()
        with patch("winbrow.browser_task._cdp_available", return_value=True):
            run(browser.navigate_to("https://github.com/trending"))
            out = run(browser.navigate_to("https://github.com/trending/python"))

        self.assertTrue(out["success"], out)
        self.assertEqual(out["method"], "cdp_navigate")
        browser._open_tab.assert_awaited_once()
        browser._activate_tab.assert_awaited_with("task-tab")

    def test_page_state_cache_reuses_until_forced(self):
        browser = self._controller()
        calls = {"fetch": 0}

        async def current_url():
            return "https://example.com/page"

        async def fetch_state():
            calls["fetch"] += 1
            return {
                "title": "Example",
                "url": "https://example.com/page",
                "text": f"snapshot {calls['fetch']}",
                "links": [],
                "method": "cdp",
            }

        browser._get_current_url = current_url
        browser._fetch_page_state = fetch_state
        first = run(browser.get_page_state())
        cached = run(browser.get_page_state())
        forced = run(browser.get_page_state(force_refresh=True))

        self.assertEqual(calls["fetch"], 2)
        self.assertNotIn("_cached", first)
        self.assertTrue(cached["_cached"])
        self.assertEqual(cached["text"], "snapshot 1")
        self.assertNotIn("_cached", forced)
        self.assertEqual(forced["text"], "snapshot 2")

    def test_navigation_outside_a_task_still_gets_an_agent_tab(self):
        from unittest.mock import patch

        browser = self._controller()
        self._fake_page_conn(browser)
        with patch("winbrow.browser_task._cdp_available", return_value=True):
            out = run(browser.navigate_to("https://github.com/trending"))

        self.assertTrue(out["success"], out)
        self.assertEqual(out["method"], "cdp_navigate")
        browser._open_tab.assert_awaited_once_with("about:blank")
        browser._activate_tab.assert_awaited_with("task-tab")
        self.assertEqual(browser._agent_tab_id, "task-tab")


class TestAgentWiring(unittest.TestCase):
    def setUp(self):
        from winbrow.memory.skills import reset_skills
        self.skills = reset_skills()
        self.skills.clear()      # isolation: wipe the shared json file
        from winbrow.agent import WinBrowAgent
        self.agent = WinBrowAgent(gateway=FakeGateway())
        self._stub_calls = []

        async def stub_run_skill(tool_name, args, **kw):
            self._stub_calls.append((tool_name, args))
            return {"success": True, "output": f"stub {tool_name}"}

        self._orig_run_skill = self.agent.run_skill
        self.agent.run_skill = stub_run_skill
        self.agent.brain.executor = stub_run_skill

        # instrumentation: brain questions the SAME skills store
        self.agent.skills = self.skills

    def tearDown(self):
        self.agent.run_skill = self._orig_run_skill

    def test_all_python_backed_registered(self):
        names = {t.name for t in self.agent.registry.all_tools()}
        for op in ADAPTER_TOOLS:
            self.assertIn(op.tool_name, names)
        self.assertIn("run_command", names)
        self.assertEqual(self.agent.registry.get("run_command").impl, "shell")
        for name in ("browser_navigate", "browser_read_page",
                     "browser_interact", "browser_wait"):
            self.assertIn(name, names)
            self.assertEqual(self.agent.registry.get(name).impl, "browser")

    def test_create_text_file_uses_python_writer(self):
        from unittest.mock import patch

        tmp = Path(tempfile.mkdtemp(prefix="winbrow_agent_files_"))
        self.addCleanup(shutil.rmtree, tmp, True)
        self.agent.run_skill = self._orig_run_skill
        with patch("winbrow.files._text_target_folder", return_value=tmp), \
                patch.dict(os.environ, {"WINBROW_BYPASS_CONFIRM": "1"}):
            out = run(self.agent.run_skill(
                "create_text_file",
                {"filename": "watchlist.md", "content": "# Watchlist\n"},
            ))
        self.assertTrue(out["success"], out)
        self.assertEqual((tmp / "watchlist.md").read_text(encoding="utf-8"),
                         "# Watchlist\n")
        self.assertEqual(out["type"], "tool_execution")

    def test_multistep_browser_routes_to_brain(self):
        from winbrow.brain.agent_loop import BrainResult

        calls = {"brain": 0, "ultrafast": 0}

        async def fake_handle(*args, **kwargs):
            calls["brain"] += 1
            return BrainResult(kind="chat", success=True, response="researched")

        async def fake_ultrafast(*args, **kwargs):
            calls["ultrafast"] += 1
            return {"success": True, "final_answer": "one-shot"}

        self.agent.brain.handle = fake_handle
        self.agent.browser.run_ultrafast_task = fake_ultrafast
        res = run(self.agent.execute(
            "Go to the GitHub trending page for Python. Look at the top 5 "
            "repositories. For each repo, check if it has more than 1000 stars "
            "and was updated in the last 7 days. Add qualifying repos to a new "
            "file called watchlist.md on my Desktop.",
            session_id="m2-browser-brain",
        ))
        self.assertEqual(calls["brain"], 1)
        self.assertEqual(calls["ultrafast"], 0)
        self.assertTrue(res["execution"]["success"], res["execution"])
        self.assertEqual(res["route"]["tier"], "browser_brain")

    def test_simple_browser_task_also_uses_brain(self):
        from winbrow.brain.agent_loop import BrainResult

        calls = {"brain": 0, "ultrafast": 0}

        async def fake_handle(*args, **kwargs):
            calls["brain"] += 1
            return BrainResult(kind="chat", success=True, response="opened")

        async def fake_ultrafast(*args, **kwargs):
            calls["ultrafast"] += 1
            return {"success": True, "final_answer": "one-shot"}

        self.agent.brain.handle = fake_handle
        self.agent.browser.run_ultrafast_task = fake_ultrafast
        res = run(self.agent.execute("open youtube", session_id="m2-browser-simple"))
        self.assertEqual(calls["brain"], 1)
        self.assertEqual(calls["ultrafast"], 0)
        self.assertEqual(res["route"]["tier"], "browser_brain")

    def test_browser_task_skips_stale_skill_replay(self):
        from winbrow.brain.agent_loop import BrainResult

        calls = {"brain": 0}

        async def fake_handle(*args, **kwargs):
            calls["brain"] += 1
            return BrainResult(kind="chat", success=True, response="fresh run")

        self.skills.save(
            "youtube-open", "open youtube",
            [{"skill": "browser_navigate", "args": {"url": "https://youtube.com"}}],
            status="approved")
        self.agent.brain.handle = fake_handle
        res = run(self.agent.execute("open youtube", session_id="m2-browser-replay"))
        self.assertEqual(calls["brain"], 1)
        self.assertEqual(res["execution"]["type"], "brain")

    def test_run_skill_shell_dispatch(self):
        import winbrow.agent as A
        from unittest.mock import patch

        async def fake(name, args):
            return {"success": True, "output": "shell-ok"}

        original = A.execute_shell_tool
        self.agent.run_skill = self._orig_run_skill
        A.execute_shell_tool = fake
        try:
            with patch.dict(os.environ, {"WINBROW_BYPASS_CONFIRM": "1"}):
                out = run(self.agent.run_skill(
                    "run_command",
                    {"command": "go build ./...", "cwd": "."},
                ))
        finally:
            A.execute_shell_tool = original
        self.assertEqual(out["type"], "shell")
        self.assertTrue(out["success"])
        self.assertEqual(out["output"], "shell-ok")

    def test_run_skill_adapter_dispatch(self):
        import winbrow.agent as A

        async def fake(name, args):
            return {"success": True, "output": "adapter-ok"}

        original = A.execute_adapter_tool
        real_run_skill = self._orig_run_skill
        self.agent.run_skill = real_run_skill        # need the real dispatcher
        A.execute_adapter_tool = fake
        try:
            out = run(self.agent.run_skill("vscode_open", {"target": "X:\\t"}))
        finally:
            A.execute_adapter_tool = original
        self.assertEqual(out["type"], "adapter")
        self.assertTrue(out["success"])

    def test_online_replay_quickpath(self):
        # M3: the replay step is T2 (user-data write) — pre-approve it the way
        # a real user would have (remembered allowlist from a prior confirmation).
        from winbrow.permissions import get_permissions, action_key
        get_permissions().remember(
            action_key("vscode_install_extension", {"extension_id": "ms-python.python"}),
            "t2", "vscode_install_extension")
        self.skills.save("ext", "install the python extension in vscode",
                         [{"skill": "vscode_install_extension",
                           "args": {"extension_id": "ms-python.python"}}])
        self.agent.skills = self.skills
        res = run(self.agent.execute(
            "install the python extension in vscode", session_id="m2t"))
        exe = res["execution"]
        self.assertEqual(exe["type"], "replayed_skill")
        self.assertTrue(exe["success"])
        self.assertLess(res["total_elapsed_ms"], 2000)   # acceptance: <2s
        self.assertEqual(res["route"]["tier"], "replay")
        # one stub op was executed, gateway never called
        self.assertEqual(len(self._stub_calls), 1)
        self.assertEqual(self.agent.gateway.calls, 0)

    def test_no_replay_on_unrelated(self):
        self.skills.save("ext", "install the python extension in vscode",
                         [{"skill": "vscode_install_extension", "args": {}}])
        self.agent.skills = self.skills
        res = run(self.agent.execute("what time is it", session_id="m2t"))
        self.assertNotEqual(res["execution"].get("type"), "replayed_skill")

    def test_capture_after_brain_success_is_proposed(self):
        plan = {"kind": "action", "steps": [
            {"skill": "vscode_open", "args": {"target": "D:\\x"}},
            {"skill": "computer_scroll", "args": {"direction": "down"}},
        ], "response": "done"}
        done = {"kind": "done", "summary": "Opened the project and scrolled."}
        gw = FakeGateway([plan, done])
        self.agent.gateway = gw
        self.agent.brain.gateway = gw
        self.agent.brain.max_steps = 3
        res = run(self.agent.execute(
            "open project x in-vscode and prepare", session_id="m2t2"))
        self.assertTrue(res["execution"].get("success"))
        rec = self.skills.get("run-vscode-open-then-computer-scroll")
        self.assertIsNotNone(rec)
        self.assertNotIn("project x", rec.name)
        self.assertEqual(len(rec.steps), 2)
        self.assertEqual(rec.source, "brain")
        self.assertEqual(rec.status, "proposed")
        # Proposals never replay until a human approves them.
        self.assertIsNone(self.skills.find_match(
            "open project x in-vscode and prepare"))

    def test_partial_brain_run_is_not_captured(self):
        plan = {"kind": "action", "steps": [
            {"skill": "vscode_open", "args": {"target": "D:\\x"}},
            {"skill": "computer_scroll", "args": {"direction": "down"}},
        ], "response": "done"}
        # No done turn: the planner drops out, so the run is partial.
        gw = FakeGateway([plan, GatewayUnavailable("offline")])
        self.agent.gateway = gw
        self.agent.brain.gateway = gw
        self.agent.brain.max_steps = 3
        run(self.agent.execute(
            "open project x in-vscode and prepare", session_id="m2t2b"))
        rec = self.skills.get("open-project-x-in-vscode-and-prepare".replace('_', '-')[:60])
        self.assertIsNone(rec)

    def test_capture_skips_meta_skills(self):
        plan = {"kind": "action", "steps": [
            {"skill": "remember_fact", "args": {"fact": "x"}},
        ], "response": "noted"}
        gw = FakeGateway([plan])
        self.agent.gateway = gw
        self.agent.brain.gateway = gw
        run(self.agent.execute("remember my name is pat", session_id="m2t3"))
        recs = [r.id for r in self.skills._records]
        self.assertEqual(recs, [])   # meta-only plan is NOT captured


class TestServerSkillsEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("WINBROW_SKIP_MODEL_DL", "1")
        import server as srv
        from fastapi.testclient import TestClient
        cls.client = TestClient(srv.app)
        srv.agent.skills = SkillMemory(
            Path(_SKILLS_DIR) / "server_case.json")

    def test_endpoints(self):
        r = self.client.get("/api/skills")
        self.assertEqual(r.status_code, 200)
        self.assertIn("skills", r.json())

        # delete without id → 400
        r = self.client.post("/api/skills/delete", json={})
        self.assertEqual(r.status_code, 400)

        # delete unknown id → 200 deleted False
        r = self.client.post("/api/skills/delete", json={"skill_id": "nah"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["deleted"])

        # approve without id → 400; unknown id → approved False
        r = self.client.post("/api/skills/approve", json={})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/skills/approve", json={"skill_id": "nah"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["approved"])

    def test_skill_roundtrip_on_store(self):
        self.client.app  # server context
        store = SkillMemory(Path(_SKILLS_DIR) / "roundtrip.json")
        store.save("fl", "open flash drive flow",
                   [{"skill": "notepad_open", "args": {}}])
        self.assertEqual(len(store), 1)


# --------------------------------------------------------------------------- E2E (opt-in)

@unittest.skipUnless(os.environ.get("WINBROW_M2_E2E"),
                     "E2E requires WINBROW_M2_E2E=1 (launches real apps)")
class TestE2E(unittest.TestCase):
    """
    Ground-truth flows on this machine. Run manually:
      set WINBROW_M2_E2E=1 && python -m pytest test_m2_adapters.py -k E2E -q
    """

    def test_vscode_available_and_list_extensions(self):
        from winbrow.adapters.vscode import list_extensions
        res = run(list_extensions({}))
        if not shutil.which("code"):
            self.skipTest("vscode CLI not on PATH")
        self.assertTrue(res["success"])
        self.assertTrue(res["extensions"])

    def test_paint_available(self):
        import shutil
        self.assertIsNotNone(shutil.which("mspaint"))

    def test_notepad_open_save_roundtrip(self):
        from winbrow.adapters.notepad import OPS
        fn = {op.op_id: op.fn for op in OPS}
        name = f"winbrow_m2_{int(__import__('time').time())}.txt"
        res1 = run(fn["open"]({}))
        self.assertTrue(res1["success"])
        res2 = run(fn["save_as"]({"filename": name}))
        self.assertTrue(res2["success"])
        self.assertIsNotNone(res2["saved_path"])
        os.remove(res2["saved_path"])


if __name__ == "__main__":
    unittest.main()
