"""Regression tests for the "open chrome → x.com search → empty page" trace.

Covers:
- a blank (still-loading) page snapshot is retried, flagged, and never cached;
- read-only browser observations may be repeated by the planner;
- "open chrome" can never silently open Notepad, and browsers are offered by
  the common-app tool.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from winbrow import browser_task
from winbrow.brain.agent_loop import REPEATABLE_SKILLS, annotate_result

SEED = Path(__file__).resolve().parent / "tools" / "seed.json"


def run(coro):
    return asyncio.run(coro)


def _state(title="", text="", url="https://x.com/search?q=onepiece", links=None):
    return {"title": title, "text": text, "url": url, "links": links or []}


class StubPage:
    """BrowserController with a scripted execute_js sequence."""

    def __init__(self, states):
        self._states = list(states)
        self.fetches = 0
        self.url = "https://x.com/search?q=onepiece"

    async def _get_current_url(self):
        return self.url

    async def execute_js(self, code):
        self.fetches += 1
        index = min(self.fetches - 1, len(self._states) - 1)
        return {"success": True, "value": self._states[index]}

    async def read_page_text(self):
        return {"success": False}


def _controller(states):
    controller = browser_task.BrowserController.__new__(browser_task.BrowserController)
    controller._last_page_state = None
    controller._last_page_state_url = None
    stub = StubPage(states)
    controller._get_current_url = stub._get_current_url
    controller.execute_js = stub.execute_js
    controller.read_page_text = stub.read_page_text
    return controller, stub


class TestBlankPageState(unittest.TestCase):
    def test_blank_snapshot_is_flagged_and_not_cached(self):
        controller, stub = _controller([
            _state(),                    # navigate committed, nothing rendered
            _state(title="Search / X", text="One Piece results"),
        ])
        with patch.object(browser_task, "_PAGE_READY_WAIT_S", 0.0):
            blank = run(controller.get_page_state(force_refresh=True))
        self.assertTrue(blank.get("_blank"), blank)
        self.assertFalse(controller._last_page_state, "blank state was cached")

        # The next read must hit the page again instead of replaying the blank.
        with patch.object(browser_task, "_PAGE_READY_WAIT_S", 0.0):
            good = run(controller.get_page_state())
        self.assertEqual(good["title"], "Search / X")
        self.assertNotIn("_cached", good)
        self.assertEqual(stub.fetches, 2)

    def test_fetch_waits_for_a_rendered_document(self):
        controller, stub = _controller([
            _state(),
            _state(),
            _state(title="Search / X", text="One Piece results"),
        ])
        with patch.object(browser_task, "_PAGE_READY_WAIT_S", 1.0), \
                patch.object(browser_task, "_PAGE_READY_POLL_S", 0.0):
            state = run(controller.get_page_state(force_refresh=True))
        self.assertEqual(state["title"], "Search / X")
        self.assertFalse(state.get("_blank"))
        self.assertEqual(stub.fetches, 3)

    def test_non_web_page_is_not_polled(self):
        controller, stub = _controller([_state(url="about:blank")])
        with patch.object(browser_task, "_PAGE_READY_WAIT_S", 5.0), \
                patch.object(browser_task, "_PAGE_READY_POLL_S", 0.0):
            state = run(controller.get_page_state(force_refresh=True))
        self.assertTrue(state.get("_blank"))
        self.assertEqual(stub.fetches, 1)


class TestBrowserCaps(unittest.TestCase):
    def test_state_cap_and_link_budget(self):
        from winbrow import browser_tools as bt
        self.assertGreaterEqual(
            bt._MAX_STATE_CHARS,
            bt._MAX_READ_FULL_TEXT_CHARS + bt._MAX_READ_FULL_TREE_CHARS,
        )
        self.assertEqual(bt._MAX_PAGE_CHARS, 12_000)
        self.assertEqual(bt._MAX_TREE_CHARS, 12_000)
        self.assertEqual(bt._MAX_LINKS, 60)

    def test_arg_specs_match_caps(self):
        from winbrow.browser_tools import get_browser_tools
        tools = {tool.name: tool for tool in get_browser_tools()}
        read = {a.name: a for a in tools["browser_read_page"].args}
        self.assertEqual(read["max_chars"].default, "12000")
        full = {a.name: a for a in tools["browser_read_full"].args}
        self.assertEqual(full["max_text_chars"].default, "12000")
        self.assertEqual(full["max_tree_chars"].default, "12000")
        tree = {a.name: a for a in tools["browser_get_accessibility_tree"].args}
        self.assertEqual(tree["max_chars"].default, "12000")


class TestPlannerRepeatAndWarnings(unittest.TestCase):
    def test_reads_and_waits_are_repeatable(self):
        for skill in ("browser_read_page", "browser_read_full",
                      "browser_wait", "browser_get_accessibility_tree"):
            self.assertIn(skill, REPEATABLE_SKILLS)
        for skill in ("browser_navigate", "browser_interact", "open_file"):
            self.assertNotIn(skill, REPEATABLE_SKILLS)

    def test_blank_read_gets_a_warning(self):
        out = annotate_result(
            "browser_read_page", {},
            {"success": True, "title": "", "text": "", "links": []},
        )
        self.assertIn("still loading", out.get("_warning", "").lower())

    def test_blank_flag_reaches_the_tool_output(self):
        from winbrow.browser_tools import execute_browser_tool

        class BlankBrowser:
            async def get_page_state(self, force_refresh=False):
                return {
                    "title": "", "text": "", "links": [],
                    "url": "https://x.com/search?q=onepiece",
                    "method": "cdp", "_blank": True,
                }

        out = run(execute_browser_tool(
            "browser_read_page", {}, browser=BlankBrowser()))
        self.assertTrue(out.get("_blank"), out)
        self.assertIn("still blank", out.get("_warning", "").lower())

    def test_rendered_read_has_no_blank_warning(self):
        out = annotate_result(
            "browser_read_page", {},
            {"success": True, "title": "Search / X", "text": "results"},
        )
        self.assertNotIn("still loading", out.get("_warning", "").lower())


class TestAppResolution(unittest.TestCase):
    def setUp(self):
        with SEED.open(encoding="utf-8") as fh:
            self.tools = {t["name"]: t for t in json.load(fh)}

    def test_app_focus_has_no_notepad_default(self):
        arg = self.tools["app_focus_or_launch"]["args"][0]
        self.assertEqual(arg["name"], "app_name")
        self.assertEqual(arg.get("default"), "")

    def test_common_app_offers_browsers(self):
        criteria = self.tools["open_common_app"]["args"][0]["criteria"]
        for name in ("chrome", "edge", "brave"):
            self.assertIn(name, criteria)
        self.assertIn("'chrome': 'chrome'", self.tools["open_common_app"]["script"])


class TestBrowserPreference(unittest.TestCase):
    def tearDown(self):
        browser_task.set_preferred_browser("")

    def test_aliases(self):
        self.assertEqual(browser_task.canonical_browser("Google Chrome"), "chrome")
        self.assertEqual(browser_task.canonical_browser("Edge"), "msedge")
        self.assertEqual(browser_task.canonical_browser("brave browser"), "brave")
        self.assertEqual(browser_task.canonical_browser("notepad"), "")

    def test_requested_browser_goes_first(self):
        browser_task.set_preferred_browser("chrome")
        order = browser_task._browser_order()
        self.assertEqual(order[0], "chrome")
        self.assertEqual(set(order), {"chrome", "brave", "msedge"})
        browser_task.set_preferred_browser("")
        self.assertEqual(browser_task._preferred_browser(), "")

    def test_goal_text_selects_the_browser(self):
        cases = {
            "open chrome and go to x.com and search for onepiece": "chrome",
            "go to github.com in brave": "brave",
            "use microsoft edge to open the docs": "msedge",
        }
        for text, expected in cases.items():
            self.assertEqual(browser_task.note_browser_from_text(text), expected)
        self.assertEqual(browser_task.note_browser_from_text("open notepad"), "")
        self.assertEqual(
            browser_task.note_browser_from_text("search edge cases in python"), "")


class _LaunchBrowser:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def launch_with_cdp(self, url="", guide=True):
        self.calls += 1
        return self.result


class TestOpenRequestedBrowser(unittest.TestCase):
    def tearDown(self):
        browser_task.set_preferred_browser("")

    def test_chrome_is_launched_with_control(self):
        from winbrow.agent import WinBrowAgent

        agent = type("A", (), {})()
        agent.browser = _LaunchBrowser({
            "success": True, "method": "launch_default_profile",
            "cdp_backend": "http", "message": "Launched chrome with CDP",
        })
        out = run(WinBrowAgent._open_requested_browser(agent, "Chrome"))
        self.assertTrue(out["success"], out)
        self.assertEqual(browser_task._preferred_browser(), "chrome")
        self.assertEqual(agent.browser.calls, 1)

    def test_non_browser_name_is_left_to_the_app_launcher(self):
        from winbrow.agent import WinBrowAgent

        agent = type("A", (), {})()
        agent.browser = _LaunchBrowser({"cdp_backend": "http"})
        out = run(WinBrowAgent._open_requested_browser(agent, "Notepad"))
        self.assertEqual(out, {})
        self.assertEqual(agent.browser.calls, 0)
        self.assertEqual(browser_task._preferred_browser(), "")

    def test_running_browser_without_debugging_falls_back(self):
        from winbrow.agent import WinBrowAgent

        agent = type("A", (), {})()
        agent.browser = _LaunchBrowser({
            "success": True, "method": "opt_in_required", "cdp_backend": None,
        })
        out = run(WinBrowAgent._open_requested_browser(agent, "chrome"))
        self.assertEqual(out, {})
        self.assertEqual(browser_task._preferred_browser(), "chrome")


if __name__ == "__main__":
    unittest.main()
