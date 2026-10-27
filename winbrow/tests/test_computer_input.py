"""Native input guards: fail-safe corners, coordinates, key-combo aliases."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.computer import tools as ct
from winbrow.computer.driver import ComputerDriver
from winbrow.computer import driver as driver_module
from winbrow.computer.ladder import GroundingLadder


def _driver() -> ComputerDriver:
    return ComputerDriver()


def test_validate_point_rejects_corners_and_offscreen():
    d = _driver()
    w, h = d._primary_size()
    assert d._validate_point(0, 0)
    assert d._validate_point(w - 1, 0)
    assert d._validate_point(0, h - 1)
    assert d._validate_point(w - 1, h - 1)
    assert "outside" in d._validate_point(-50, -50)
    assert d._validate_point(100, 100) == ""


def test_click_refuses_corner_without_touching_mouse(monkeypatch):
    d = _driver()
    corralled = []
    monkeypatch.setattr(
        d, "_escape_failsafe_corner",
        lambda: corralled.append(True) or False,
    )
    result = d.click(0, 0)
    assert result["success"] is False
    assert "Refusing click" in result["output"]
    assert corralled == []  # validation happens before any input


def test_click_refuses_offscreen_point(monkeypatch):
    d = _driver()
    monkeypatch.setattr(d, "_escape_failsafe_corner", lambda: True)
    result = d.click(-10, 10)
    assert result["success"] is False
    assert "outside" in result["output"]


def test_corner_nudge_moves_cursor_inward(monkeypatch):
    pyautogui = pytest.importorskip("pyautogui")
    d = _driver()
    monkeypatch.setattr(pyautogui, "position", lambda: (0, 0))
    monkeypatch.setattr(pyautogui, "size", lambda: (1920, 1080))
    moved = []
    monkeypatch.setattr(
        driver_module.user32, "SetCursorPos",
        lambda x, y: moved.append((x, y)),
    )
    assert d._escape_failsafe_corner() is True
    assert moved == [(2, 2)]

    moved.clear()
    monkeypatch.setattr(pyautogui, "position", lambda: (1919, 1079))
    assert d._escape_failsafe_corner() is True
    assert moved == [(1917, 1077)]


def test_corner_nudge_noop_away_from_corner(monkeypatch):
    pyautogui = pytest.importorskip("pyautogui")
    d = _driver()
    monkeypatch.setattr(pyautogui, "position", lambda: (400, 300))
    moved = []
    monkeypatch.setattr(
        driver_module.user32, "SetCursorPos",
        lambda x, y: moved.append((x, y)),
    )
    assert d._escape_failsafe_corner() is False
    assert moved == []


def test_computer_key_accepts_key_alias(monkeypatch):
    class FakeDriver:
        def press_key(self, combo):
            return {"success": True, "output": combo}

    monkeypatch.setattr(ct, "_get_driver", lambda: FakeDriver())
    result = ct.execute_computer_tool("computer_key", {"key": "ctrl+f"})
    assert result["success"] is True
    assert result["output"] == "ctrl+f"


def test_computer_key_accepts_hotkey_alias(monkeypatch):
    class FakeDriver:
        def press_key(self, combo):
            return {"success": True, "output": combo}

    monkeypatch.setattr(ct, "_get_driver", lambda: FakeDriver())
    result = ct.execute_computer_tool("computer_key", {"hotkey": "alt+f4"})
    assert result["success"] is True
    assert result["output"] == "alt+f4"


def test_computer_key_missing_combo_message():
    result = ct.execute_computer_tool("computer_key", {})
    assert result["success"] is False
    assert "combo" in result["output"]


@pytest.mark.skipif(sys.platform != "win32", reason="UIA is Windows-only")
def test_uia_engine_initializes_by_clsid():
    pytest.importorskip("comtypes")
    from winbrow.computer.uia import UIATree

    tree = UIATree()
    assert tree._get_engine() is not None


@pytest.mark.skipif(sys.platform != "win32", reason="UIA is Windows-only")
def test_uia_observe_invalid_hwnd_is_empty():
    pytest.importorskip("comtypes")
    from winbrow.computer.uia import UIATree

    assert UIATree().observe(0) == []


# ---------------------------------------------------------------------------
# OCR word-level grounding (merged OCR lines must not be clicked at line center)
# ---------------------------------------------------------------------------

def _ladder_with_ocr(line: dict) -> GroundingLadder:
    driver = type("D", (), {"user32": type("U", (), {
        "GetForegroundWindow": staticmethod(lambda: 4242),
    })})()
    uia = type("U", (), {"observe": lambda self, hwnd, max_nodes=None: []})()
    screen = type("S", (), {"ocr": lambda self: {"available": True, "lines": [line]}})()
    return GroundingLadder(driver=driver, uia=uia, screen=screen)


def test_ocr_word_phrase_uses_word_rect_not_line_center():
    line = {
        "text": "WhatsApp Chats Search or start a new chat Recent searches",
        "rect": [0, 0, 1200, 30],
        "words": [
            {"text": "WhatsApp", "rect": [0, 0, 100, 30]},
            {"text": "Chats", "rect": [110, 0, 60, 30]},
            {"text": "Search", "rect": [300, 0, 60, 30]},
            {"text": "or", "rect": [365, 0, 25, 30]},
            {"text": "start", "rect": [395, 0, 50, 30]},
            {"text": "a", "rect": [450, 0, 15, 30]},
            {"text": "new", "rect": [470, 0, 40, 30]},
            {"text": "chat", "rect": [515, 0, 45, 30]},
            {"text": "Recent", "rect": [600, 0, 70, 30]},
        ],
    }
    hit = _ladder_with_ocr(line).resolve_target("Search or start a new chat", hwnd=1)
    assert hit.ok and hit.source == "ocr"
    assert (hit.x, hit.y) == (430, 15)


def test_ocr_word_single_token_in_merged_line():
    line = {
        "text": "99+ Sunny WhatsApp 27-10-2026 Chats Anix All Contacts",
        "rect": [0, 200, 1200, 32],
        "words": [
            {"text": "99+", "rect": [0, 200, 40, 32]},
            {"text": "Sunny", "rect": [50, 200, 60, 32]},
            {"text": "WhatsApp", "rect": [120, 200, 80, 32]},
            {"text": "27-10-2026", "rect": [210, 200, 90, 32]},
            {"text": "Chats", "rect": [310, 200, 60, 32]},
            {"text": "Anix", "rect": [380, 200, 50, 32]},
            {"text": "All", "rect": [440, 200, 30, 32]},
            {"text": "Contacts", "rect": [480, 200, 80, 32]},
        ],
    }
    hit = _ladder_with_ocr(line).resolve_target("Anix", hwnd=1)
    assert hit.ok and hit.source == "ocr"
    assert (hit.x, hit.y) == (405, 216)


def test_ocr_line_fallback_when_words_missing():
    line = {
        "text": "Search or start a new chat",
        "rect": [100, 50, 200, 20],
    }
    hit = _ladder_with_ocr(line).resolve_target("Search or start a new chat", hwnd=1)
    assert hit.ok and hit.source == "ocr"
    assert (hit.x, hit.y) == (200, 60)


def test_ocr_rect_normalization_accepts_all_shapes():
    from winbrow.computer.screen import _rect4

    assert _rect4("37 14 12 12") == (37, 14, 12, 12)
    assert _rect4([37, 14, 12, 12], 10, 20) == (47, 34, 12, 12)
    assert _rect4({"x": 1, "y": 2, "w": 3, "h": 4}) == (1, 2, 3, 4)
    assert _rect4({"left": 5, "top": 6, "width": 7, "height": 8}) == (5, 6, 7, 8)
    assert _rect4(None) == (0, 0, 0, 0)
