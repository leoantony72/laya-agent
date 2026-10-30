"""Generic app-activation tests: hidden windows, honest failure states."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import winbrow.apps as apps_mod
import winbrow.computer.driver as driver_mod
import winbrow.windows as windows_mod
from winbrow.apps import AppEntry, AppMatch


def _window(process, title, hwnd=1, **kwargs):
    data = {
        "hwnd": hwnd,
        "title": title,
        "process_name": process,
        "pid": 100 + hwnd,
        "rect": (-9, -9, 1200, 800),
        "is_visible": True,
        "is_minimized": False,
        "is_cloaked": False,
        "is_toolwindow": False,
        "class_name": "WinUIDesktopWin32WindowClass",
        "owner_hwnd": 0,
        "exe_path": "",
    }
    data.update(kwargs)
    return SimpleNamespace(**data, to_dict=lambda: dict(data))


class FakeDriver:
    def __init__(self, windows, activation=None):
        self.windows = list(windows)
        self.activation = activation or (lambda hwnd: {"success": True})
        self.focused = []

    def list_windows(self, include_hidden=False):
        return list(self.windows)

    def activate_window(self, hwnd):
        self.focused.append(hwnd)
        return self.activation(hwnd)


def _match(name="WhatsApp", target="whatsapp:", kind="protocol", exe=""):
    return AppMatch(
        AppEntry(name=name, target=target, kind=kind, exe_name=exe,
                 aliases=(name.lower(),)),
        1.0,
    )


def _fake_ps(calls=None, result=None):
    async def fake(script, timeout=30):
        if calls is not None:
            calls.append(script)
        return result or {"success": True, "stdout": "__PID__:0", "stderr": ""}
    return fake


def _run(app_name, windows, *, activation=None, process_running=False,
         ps=None, wait_result=(None, None), match="default"):
    fake = FakeDriver(windows, activation)
    app_match = _match() if match == "default" else match

    async def fake_wait(*args, **kwargs):
        return wait_result

    with patch.object(driver_mod, "ComputerDriver", lambda *a, **k: fake), \
         patch.object(apps_mod, "resolve_app", lambda q: app_match), \
         patch.object(windows_mod, "_process_matches",
                      lambda *a, **k: process_running), \
         patch.object(windows_mod, "_wait_for_visible_app_window", fake_wait), \
         patch.object(windows_mod, "run_powershell", ps or _fake_ps()):
        return asyncio.run(windows_mod.focus_or_launch_app(app_name)), fake


def test_hidden_main_window_is_activated_not_assumed_open():
    hidden = _window("WhatsApp.Root.exe", "WhatsApp", hwnd=42,
                     is_visible=False, rect=(-9, -9, 1929, 1029))
    out, fake = _run("whatsapp", [hidden])
    assert out["success"] is True
    assert out["state"] == "activated"
    assert out["window"]["hwnd"] == 42
    assert fake.focused == [42]


def test_visible_window_is_focused():
    visible = _window("Code.exe", "main.go - Visual Studio Code", hwnd=7)
    out, fake = _run(
        "code", [visible],
        match=_match("Visual Studio Code", r"C:\Apps\Code.exe", "exe",
                     "Code.exe"))
    assert out["success"] is True
    assert out["state"] == "focused"
    assert fake.focused == [7]


def test_running_process_without_window_is_not_success():
    out, fake = _run("whatsapp", [], process_running=True)
    assert out["success"] is False
    assert out["state"] == "running_no_window"
    assert "running in the background" in out["output"]
    assert fake.focused == []


def test_launch_error_is_reported_honestly():
    out, _ = _run(
        "whatsapp", [], process_running=False,
        ps=_fake_ps(result={"success": True,
                            "stdout": "__ERR__:protocol not registered",
                            "stderr": ""}))
    assert out["success"] is False
    assert out["state"] == "launch_failed"
    assert "protocol not registered" in out["output"]


def test_launch_then_visible_window_reports_launched():
    visible = _window("WhatsApp.Root.exe", "WhatsApp", hwnd=9)
    out, _ = _run(
        "whatsapp", [], process_running=False,
        wait_result=(visible, {"success": True}))
    assert out["success"] is True
    assert out["state"] == "launched"
    assert out["window"]["hwnd"] == 9


def test_ranking_prefers_hidden_main_over_tool_helper():
    main = _window("WhatsApp.Root.exe", "WhatsApp", hwnd=1,
                   is_visible=False, rect=(-9, -9, 1929, 1029))
    helper = _window("WhatsApp.Root.exe", "WhatsApp Helper", hwnd=2,
                     is_toolwindow=True, class_name="Chrome_WidgetWin_0")
    ranked = windows_mod._find_app_windows(
        FakeDriver([helper, main]), "whatsapp", "WhatsApp", "")
    assert ranked and ranked[0] is main


def test_tray_plumbing_windows_are_ignored():
    tray = _window("WhatsApp.Root.exe", "H.NotifyIcon_abc", hwnd=3,
                   is_visible=False, class_name="H.NotifyIcon_abc")
    ranked = windows_mod._find_app_windows(
        FakeDriver([tray]), "whatsapp", "WhatsApp", "")
    assert ranked == []


def test_exact_process_beats_substring():
    opencode = _window("OpenCode.exe", "OpenCode - session")
    code = _window("Code.exe", "chicken.go - Visual Studio Code")
    assert windows_mod._window_match_score(
        opencode, "code", "Visual Studio Code", "Code.exe") == 0
    assert windows_mod._window_match_score(
        code, "code", "Visual Studio Code", "Code.exe") == 6


def test_unknown_app_never_launches_a_guess():
    calls = []
    out, _ = _run(
        "definitely-not-installed", [], match=None,
        ps=_fake_ps(calls=calls))
    assert out["success"] is False
    assert out["state"] == "not_found"
    assert calls == []


def test_unknown_running_app_with_failed_activation_is_not_not_found():
    hidden = _window("Mystery.exe", "Mystery", hwnd=5, is_visible=False)
    out, _ = _run(
        "mystery", [hidden], match=None,
        activation=lambda hwnd: {"success": False, "output": "kept behind"})
    assert out["success"] is False
    assert out["state"] == "running_no_window"
    assert "has a window" in out["output"]


# ---------------------------------------------------------------------------
# type_text guard: a failed activation must never paste into another window.
# ---------------------------------------------------------------------------

class _AllowAllPerms:
    async def gate(self, *args, **kwargs):
        return True, "", None


class FakeTypingDriver:
    def __init__(self, foreground=None):
        self.typed: list[str] = []
        self.focus_calls: list[str] = []
        self.foreground = foreground or {"success": False}

    def get_foreground(self):
        return self.foreground

    def type_text(self, text, via_clipboard=True):
        self.typed.append(text)
        return {"success": True, "output": "typed"}


def _run_type_text(agent, args, utterance, fake):
    async def fake_focus(app_name):
        fake.focus_calls.append(app_name)
        return fake.focus_result

    from unittest.mock import AsyncMock
    with patch.object(windows_mod, "focus_or_launch_app",
                      AsyncMock(side_effect=fake_focus)), \
         patch.object(driver_mod, "ComputerDriver", lambda *a, **k: fake), \
         patch("winbrow.permissions.get_permissions", lambda: _AllowAllPerms()):
        return asyncio.run(agent.run_skill(
            "type_text", args, utterance=utterance))


def test_type_text_blocked_when_activation_fails():
    from winbrow.agent import WinBrowAgent

    agent = WinBrowAgent()
    fake = FakeTypingDriver()
    fake.focus_result = {
        "success": False,
        "state": "running_no_window",
        "output": ("WhatsApp is running in the background, but it has no "
                   "visible window I can type into."),
        "window": None,
    }
    out = _run_type_text(
        agent, {"text": "hello", "app": "whatsapp"},
        "type hello into whatsapp", fake)

    assert out["success"] is False
    assert out["state"] == "running_no_window"
    assert "Did not type" in out["output"]
    assert fake.typed == []


def test_type_text_blocked_when_prior_activation_failed_and_no_app_given():
    from winbrow.agent import WinBrowAgent

    agent = WinBrowAgent()
    agent._last_focus_failure = {
        "app": "whatsapp",
        "state": "running_no_window",
        "output": "no visible window",
    }
    fake = FakeTypingDriver()
    fake.focus_result = {"success": True}
    out = _run_type_text(agent, {"text": "hello"}, "type hello", fake)

    assert out["success"] is False
    assert "Did not type" in out["output"]
    assert fake.typed == []


def test_type_text_blocked_when_focus_moves_after_activation():
    from winbrow.agent import WinBrowAgent

    agent = WinBrowAgent()
    fake = FakeTypingDriver(foreground={
        "success": True,
        "window": {"pid": 222, "process": "explorer.exe", "hwnd": 1},
    })
    fake.focus_result = {
        "success": True,
        "state": "focused",
        "window": {"pid": 111, "process": "WhatsApp.Root.exe", "hwnd": 42},
    }
    out = _run_type_text(
        agent, {"text": "hello", "app": "whatsapp"},
        "type hello into whatsapp", fake)

    assert out["success"] is False
    assert out["state"] == "focus_lost"
    assert "Did not type" in out["output"]
    assert fake.typed == []


def test_type_text_types_when_focus_is_verified():
    from winbrow.agent import WinBrowAgent

    agent = WinBrowAgent()
    window = {"pid": 111, "process": "WhatsApp.Root.exe", "hwnd": 42}
    fake = FakeTypingDriver(foreground={"success": True, "window": window})
    fake.focus_result = {"success": True, "state": "focused", "window": window}
    out = _run_type_text(
        agent, {"text": "hello", "app": "whatsapp"},
        "type hello into whatsapp", fake)

    assert out["success"] is True
    assert fake.typed == ["hello"]
    assert "whatsapp" in out["output"]


def test_type_text_reuses_last_activated_app_when_omitted():
    from winbrow.agent import WinBrowAgent

    agent = WinBrowAgent()
    window = {"pid": 111, "process": "WhatsApp.Root.exe", "hwnd": 42}
    agent._last_focus_target = {"app": "whatsapp", "window": window}
    fake = FakeTypingDriver(foreground={"success": True, "window": window})
    fake.focus_result = {"success": True, "state": "focused", "window": window}
    out = _run_type_text(agent, {"text": "hello"}, "type hello", fake)

    assert out["success"] is True
    assert fake.focus_calls == ["whatsapp"]
    assert fake.typed == ["hello"]


def test_type_text_blocked_when_foreground_cannot_be_verified():
    from winbrow.agent import WinBrowAgent

    agent = WinBrowAgent()
    fake = FakeTypingDriver(foreground={"success": False})
    fake.focus_result = {
        "success": True,
        "state": "focused",
        "window": {"pid": 111, "process": "WhatsApp.Root.exe", "hwnd": 42},
    }
    out = _run_type_text(
        agent, {"text": "hello", "app": "whatsapp"},
        "type hello into whatsapp", fake)

    assert out["success"] is False
    assert out["state"] == "focus_unverified"
    assert fake.typed == []


def test_computer_type_alias_blocked_after_failed_activation():
    from winbrow.agent import WinBrowAgent
    import winbrow.agent as agent_mod

    agent = WinBrowAgent()
    agent._last_focus_failure = {
        "app": "whatsapp",
        "state": "running_no_window",
        "output": "no visible window",
    }
    with patch("winbrow.permissions.get_permissions",
               lambda: _AllowAllPerms()), \
         patch.object(agent_mod, "execute_computer_tool",
                      side_effect=AssertionError("must not type")):
        out = asyncio.run(agent.run_skill(
            "computer_type", {"text": "hello"}, utterance="type hello"))

    assert out["success"] is False
    assert "Did not type" in out["output"]
