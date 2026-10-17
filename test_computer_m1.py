"""
M1 Tests — Native Computer Control (logic-level, non-destructive)
==================================================================
Covers driver logic, ladder grounding, registry integration, and the
agent dispatch path WITHOUT performing destructive GUI input.

E2E (real mouse/keyboard) runs only when WINBROW_M1_E2E=1.
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from winbrow.computer import (
    COMPUTER_IMPL, ComputerDriver, GroundingLadder, GroundingHit, Screen,
    UIAElement, get_computer_tools, execute_computer_tool,
)
from winbrow.computer.driver import WindowInfo


# --------------------------------------------------------------------------- driver logic

def _win(title="Test Win", proc="notepad.exe", hwnd=1, rect=(0, 0, 400, 300), minimized=False):
    w = WindowInfo(hwnd=hwnd, title=title, process_name=proc, pid=1234, rect=rect)
    w.is_minimized = minimized
    return w


class TestKeyCombo:
    def test_combo_basic(self):
        d = ComputerDriver()
        assert d.parse_key_combo("ctrl+shift+s") == ["ctrl", "shift", "s"]

    def test_combo_aliases_and_seps(self):
        d = ComputerDriver()
        assert d.parse_key_combo("ALT F4") == ["alt", "f4"]
        assert d.parse_key_combo("control - alt - delete") == ["ctrl", "alt", "delete"]
        assert d.parse_key_combo("escape") == ["esc"]

    def test_combo_single_char(self):
        d = ComputerDriver()
        assert d.parse_key_combo("j") == ["j"]

    def test_combo_invalid(self):
        d = ComputerDriver()
        with pytest.raises(ValueError):
            d.parse_key_combo("ctrl+%%")
        with pytest.raises(ValueError):
            d.parse_key_combo("")
        with pytest.raises(ValueError):
            d.parse_key_combo("+++")

    def test_locked_guard(self):
        d = ComputerDriver()
        d._locked = True
        with pytest.raises(RuntimeError):
            d.press_key("ctrl+s")


class TestWindowInfo:
    def test_center(self):
        assert _win(rect=(10, 20, 110, 70)).center == (60, 45)

    def test_list_windows_nonempty(self):
        d = ComputerDriver()
        wins = d.list_windows()
        assert isinstance(wins, list)
        for w in wins:
            assert w.title and w.hwnd

    def test_find_windows_by_process(self):
        import re
        d = ComputerDriver()
        proc = next((w.process_name for w in d.list_windows() if w.process_name), None)
        if not proc:
            pytest.skip("no resolvable process names")
        hits = d.find_windows(process_re=re.escape(proc))
        assert hits

    def test_foreground_readable(self):
        d = ComputerDriver()
        res = d.get_foreground()
        assert isinstance(res, dict)


def re_escape(s: str) -> str:
    import re
    return re.escape(s) if s else ".*"


# --------------------------------------------------------------------------- atoms + dispatcher

class TestExecuteComputerTool:
    def test_unknown_tool(self):
        res = execute_computer_tool("nonexistent_atom", {})
        assert res["success"] is False

    def test_key_bad_combo(self):
        res = execute_computer_tool("computer_key", {"combo": "zz%%"})
        assert res["success"] is False
        assert "Bad combo" in res.get("output", "")

    def test_click_missing_coords(self):
        res = execute_computer_tool("computer_click", {})
        assert res["success"] is False

    def test_locked_input_rejected(self):
        from winbrow.computer import tools as ct

        class LockedDriver:
            _locked: bool = True

            def _guard(self, what: str) -> None:
                raise RuntimeError("locked")

            def press_key(self, combo: str) -> dict:
                self._guard(combo)

        ct._driver = LockedDriver()
        try:
            res = execute_computer_tool("computer_key", {"combo": "ctrl+s"})
        finally:
            ct._driver = None
        assert res["success"] is False
        assert "locked" in (res.get("output", "") or "")

    def test_minimize_missing_app(self):
        res = execute_computer_tool("minimize_all_except", {})
        assert res["success"] is False

    def test_wait_for_window_missing_title(self):
        res = execute_computer_tool("wait_for_window", {})
        assert res["success"] is False


# --------------------------------------------------------------------------- registry integration

@pytest.fixture(scope="module")
def agent():
    from winbrow.agent import WinBrowAgent as A
    return A()


class TestRegistryIntegration:
    def test_atom_tools_present(self, agent):
        t = agent.registry.get("computer_click_text")
        assert t is not None
        assert t.impl == COMPUTER_IMPL
        assert agent.registry.get("computer_type") is not None
        assert agent.registry.get("minimize_all_except") is not None

    def test_atom_render_script_empty(self):
        for t in get_computer_tools():
            assert t.render_script({}) == ""  # never executed as script

    def test_no_duplicate_atoms(self, agent):
        names = [t.name for t in get_computer_tools()]
        assert len(names) == len(set(names))

    def test_run_skill_dispatch_computer(self, agent, monkeypatch):
        calls = {}

        def stub(name, args):
            calls[name] = args
            return {"success": True, "output": "stubbed"}

        monkeypatch.setattr("winbrow.agent.execute_computer_tool", stub)
        out = asyncio.run(agent.run_skill("computer_click", {"x": "5", "y": "6"}))
        assert out["type"] == "computer"
        assert out["success"] is True
        assert calls == {"computer_click": {"x": "5", "y": "6"}}

    def test_regular_tools_still_scripted(self, agent):
        t = agent.registry.get("current_time")
        if t is None:
            pytest.skip("seed tool missing")
        assert t.impl is None or t.impl != COMPUTER_IMPL
        assert t.render_script({}) != ""


# --------------------------------------------------------------------------- grounding ladder

class TestGroundingLadder:
    def _driver_stub(self):
        return SimpleNamespace(user32=SimpleNamespace(GetForegroundWindow=lambda: 4242))

    def _uia_stub(self, elements):
        return SimpleNamespace(observe=lambda hwnd, max_nodes=None: elements)

    def test_hit_uia_exact_name(self):
        el = UIAElement(name="Save As", control_type="Button",
                        rect=(100, 200, 60, 20), invoke_supported=True)
        ladder = GroundingLadder(driver=self._driver_stub(),
                                 uia=self._uia_stub([el]), screen=None)
        hit = ladder.resolve_target("Save As", hwnd=1)
        assert hit.ok and hit.source == "uia"
        assert (hit.x, hit.y) == (130, 210)
        assert hit.matched == "Save As"

    def test_hit_uia_token_overlap(self):
        el = UIAElement(name="Open Recent Menu", control_type="Button",
                        rect=(0, 0, 50, 20), invoke_supported=True)
        ladder = GroundingLadder(driver=self._driver_stub(),
                                 uia=self._uia_stub([el]), screen=None)
        hit = ladder.resolve_target("recent menu", hwnd=1)
        assert hit.ok and hit.source == "uia"

    def test_ocr_fallback_rung(self):
        screen = SimpleNamespace(ocr=lambda: {"available": True, "text": "Export",
                                             "lines": [{"text": "Export", "rect": [50, 400, 80, 30]}]})
        ladder = GroundingLadder(driver=self._driver_stub(),
                                 uia=self._uia_stub([]), screen=screen)
        hit = ladder.resolve_target("export", hwnd=1)
        assert hit.ok and hit.source == "ocr"
        assert (hit.x, hit.y) == (90, 415)

    def test_miss_reports_reason(self):
        ladder = GroundingLadder(driver=self._driver_stub(),
                                 uia=self._uia_stub([]),
                                 screen=SimpleNamespace(ocr=lambda: {"available": True, "lines": []}))
        hit = ladder.resolve_target("nonexistentwidget", hwnd=1)
        assert not hit.ok
        assert hit.reason

    def test_observe_nonexistent_window(self):
        d = ComputerDriver()
        ladder = GroundingLadder(driver=d, screen=None)
        ctx = ladder.observe(hwnd=0x999999, include_ocr=False)
        assert ctx["window"] is None

    def test_hit_shape(self):
        d = GroundingHit(ok=True, source="uia", x=3, y=4, matched="Ok").to_dict()
        assert d["found"] and d["x"] == 3 and d["y"] == 4


# --------------------------------------------------------------------------- screen

class TestScreen:
    def test_template_marker_intact(self):
        from winbrow.computer.screen import _PS_OCR_SCRIPT
        assert "param([string]$ImagePath, [string]$OutPath)" in _PS_OCR_SCRIPT

    def test_capture_returns_view(self):
        screen = Screen()
        view = screen.capture()
        assert os.path.exists(view.image_path)
        assert view.width > 0 and view.height > 0
        assert view.rect == (0, 0, view.width, view.height)

    def test_compare_images(self):
        screen = Screen()
        a = screen.capture()
        res = screen.compare_images(a.image_path, a.image_path)
        assert res["changed"] is False  # identical input → no change

    def test_tool_records(self):
        tools = get_computer_tools()
        assert len(tools) == 10
        assert all(t.impl == COMPUTER_IMPL for t in tools)
        assert all(t.script == "" for t in tools)


# --------------------------------------------------------------------------- E2E (opt-in)

@pytest.mark.skipif(not os.environ.get("WINBROW_M1_E2E"),
                    reason="E2E requires WINBROW_M1_E2E=1")
class TestE2E:
    """Real GUI moves — run manually with `set WINBROW_M1_E2E=1`."""

    def test_click_safe_spot(self):
        d = ComputerDriver()
        r = d.click(x=10, y=10)
        assert r["success"]

    def test_type_and_paste_roundtrip(self):
        d = ComputerDriver()
        d.set_clipboard("winbrow-e2e")
        assert d.get_clipboard() == "winbrow-e2e"

    def test_wait_for_window_and_focus(self):
        d = ComputerDriver()
        wins = d.list_windows()
        assert wins
        res = d.wait_for_window(re_escape(wins[0].title), timeout=3)
        assert res["success"] and res["hwnd"] == wins[0].hwnd

    def test_minimize_all_except_self(self):
        d = ComputerDriver()
        res = d.minimize_all_except("explorer.exe")
        assert res["success"]
