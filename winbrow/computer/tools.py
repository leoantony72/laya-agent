"""
Computer Skill Atoms — Brain-Callable Native Actions
=====================================================
Python-backed tools the planner can emit as steps (impl="computer").

Atoms stay single-purpose; multi-step flows (open → type → save dialog)
are composed by the brain's agent loop. Every atom returns
{"success": bool, "output": str, ...extras}.

Registration: get_computer_tools() returns Tool records with
impl="computer" and empty script; args carry JSON-arg names.
Dispatch:     execute_computer_tool(name, args, ctx).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("winbrow.computer.tools")

COMPUTER_IMPL = "computer"

# Lazily-created singletons
_driver = None
_screen = None
_uia = None
_ladder = None


def _get_driver():
    global _driver
    if _driver is None:
        from .driver import ComputerDriver
        _driver = ComputerDriver()
    return _driver


def _get_screen():
    global _screen
    if _screen is None:
        from .screen import Screen
        _screen = Screen()
    return _screen


def _get_ladder():
    global _ladder
    if _ladder is None:
        from .ladder import GroundingLadder
        _ladder = GroundingLadder(driver=_get_driver(), uia=_uia, screen=_get_screen())
    return _ladder


def foreground_hwnd() -> int:
    import ctypes
    return ctypes.windll.user32.GetForegroundWindow()


# ---------------------------------------------------------------------------
# Atom implementations
# ---------------------------------------------------------------------------

def _focus_window(args: dict) -> dict:
    """snapshot args: {title (substring/regex, optional)}"""
    driver = _get_driver()
    title = str(args.get("title", "")).strip()
    if not title:
        fg = foreground_hwnd()
        for wi in driver.list_windows():
            if wi.hwnd == fg:
                return {"success": True, "output": f"Foreground: {wi.process_name}: {wi.title}",
                        "window": wi.to_dict()}
        return {"success": False, "output": "No foreground window"}
    matches = driver.find_windows(title_re=title)
    if not matches:
        return {"success": False, "output": f"No window matching '{title}'"}
    matches.sort(key=lambda w: (not w.is_minimized, -(w.rect[2] - w.rect[0])))
    res = driver.focus_window(matches[0].hwnd)
    res["window"] = matches[0].to_dict()
    time.sleep(0.2)  # let app settle (dialogs, animation)
    return res


def _computer_click(args: dict) -> dict:
    """args: {x, y, double?, button?}"""
    driver = _get_driver()
    x, y = int(args.get("x", -1)), int(args.get("y", -1))
    if x < 0 or y < 0:
        return {"success": False, "output": "Missing x/y coordinates"}
    return driver.click(x=x, y=y, button=str(args.get("button", "left")),
                        double=bool(args.get("double", False)))


def _computer_click_text(args: dict) -> dict:
    """args: {text, hwnd?} — ground a target on the current window and click."""
    driver = _get_driver()
    text = str(args.get("text", "")).strip()
    if not text:
        return {"success": False, "output": "Missing text to click"}
    hwnd = args.get("hwnd") or foreground_hwnd()
    hit = _get_ladder().resolve_target(text, hwnd=int(hwnd))
    data = hit.to_dict()
    if not hit.ok:
        return {"success": False, "output": f"Could not ground '{text}': {hit.reason}",
                "grounding": data}
    r = driver.click(x=hit.x, y=hit.y)
    r["grounding"] = data
    return r


def _computer_type(args: dict) -> dict:
    """args: {text} — types into the FOCUSED control (caller focuses first)."""
    text = str(args.get("text", ""))
    return _get_driver().type_text(text, via_clipboard=True)


def _computer_key(args: dict) -> dict:
    """args: {combo} e.g. 'ctrl+s', 'alt+f4'"""
    combo = str(args.get("combo", "")).strip()
    if not combo:
        return {"success": False, "output": "Missing key combo"}
    try:
        return _get_driver().press_key(combo)
    except ValueError as e:
        return {"success": False, "output": f"Bad combo: {e}"}


def _computer_scroll(args: dict) -> dict:
    amount = int(args.get("amount", 3) or 3)
    direction = str(args.get("direction", "down"))
    return _get_driver().scroll(amount=amount, direction=direction)


def _computer_paste(args: dict) -> dict:
    text = args.get("text")
    return _get_driver().paste(str(text) if text is not None else None)


def _minimize_all_except(args: dict) -> dict:
    app = str(args.get("app", "")).strip()
    if not app:
        return {"success": False, "output": "Missing 'app' to keep in front"}
    return _get_driver().minimize_all_except(app)


def _wait_for_window(args: dict) -> dict:
    title = str(args.get("title", "")).strip()
    timeout = float(args.get("timeout", 15) or 15)
    if not title:
        return {"success": False, "output": "Missing title"}
    res = _get_driver().wait_for_window(title, timeout=timeout)
    if res.get("success"):
        hwnd = res["window"]["hwnd"]
        time.sleep(0.3)
        _get_driver().focus_window(hwnd)
    return res


def _screen_read_text(args: dict) -> dict:
    """OCR the foreground window (or screen) and return lines with rects."""
    hwnd = args.get("hwnd") or foreground_hwnd()
    win = None
    for w in _get_driver().list_windows():
        if w.hwnd == hwnd:
            win = w
            break
    screen = _get_screen()
    try:
        ocr = screen.ocr(screen.capture_window(win).rect if win else None)
    except Exception as e:
        return {"success": False, "output": f"OCR failed: {e}"}
    if not ocr.get("available"):
        return {"success": False,
                "output": f"OCR unavailable ({ocr.get('reason') or ocr.get('error', 'unknown')})"}
    lines = ocr.get("lines", [])
    return {"success": True, "text": ocr.get("text", ""), "lines": lines,
            "output": f"Read {len(lines)} lines of text"}


def _file_dialog_save(args: dict) -> dict:
    """args: {filename?} — drive the focused Save/Save-As dialog: set name, Enter."""
    driver = _get_driver()
    filename = str(args.get("filename", "")).strip()
    driver._guard("save dialog")
    try:
        if filename:
            driver.press_key("ctrl+a")   # select dialog name field text (explorer dialogs)
            time.sleep(0.25)
            driver.type_text(filename, via_clipboard=True)
            time.sleep(0.4)
        driver.press_key("enter")
        time.sleep(0.4)
        # overwrite prompt ("Confirm Save As") when the target already exists
        if driver.wait_for_window("^Confirm Save As$", timeout=0.8).get("success"):
            driver.press_key("y")
            time.sleep(0.4)
        return {"success": True,
                "output": f"Save dialog handled: '{filename}', confirmed with Enter"}
    except Exception as e:
        return {"success": False, "output": f"Save dialog failed: {e}"}


# Actions exposed to the dispatcher below
_ACTIONS: dict[str, Callable[[dict], dict]] = {
    "focus_window": _focus_window,
    "computer_click": _computer_click,
    "computer_click_text": _computer_click_text,
    "computer_type": _computer_type,
    "computer_key": _computer_key,
    "computer_scroll": _computer_scroll,
    "computer_paste": _computer_paste,
    "minimize_all_except": _minimize_all_except,
    "wait_for_window": _wait_for_window,
    "screen_read_text": _screen_read_text,
    "file_dialog_save": _file_dialog_save,
}


def execute_computer_tool(name: str, args: dict) -> dict:
    """Dispatch a computer atom by tool name. Never raises."""
    args = args or {}
    action = _ACTIONS.get(name)
    if action is None:
        return {"success": False, "output": f"Unknown computer tool '{name}'"}
    try:
        return action(args)
    except Exception as e:
        log.warning(f"computer atom '{name}' failed: {e}")
        return {"success": False, "output": f"{name} failed: {e}"}


# ---------------------------------------------------------------------------
# Registry registration (empty script; rendered_script propagation guarded)
# ---------------------------------------------------------------------------

def get_computer_tools():
    """Tool list registered into the ToolRegistry at init."""
    from ..registry import ArgSpec, Tool

    return [
        Tool(
            name="computer_click_text",
            description="Click a button/menu/text item by description in the focused app window (UIA→OCR grounding).",
            script="",
            impl=COMPUTER_IMPL,
            args=[ArgSpec(name="text", kind="text", instructions="Text on the control to click, e.g. 'Save As'")],
            examples=["click save", "click the export button", "press play"],
        ),
        Tool(
            name="computer_click",
            description="Click the mouse at screen coordinates in the focused app (for ungrounded pixels).",
            script="",
            impl=COMPUTER_IMPL,
            args=[
                ArgSpec(name="x", kind="text", instructions="screen x coordinate"),
                ArgSpec(name="y", kind="text", instructions="screen y coordinate"),
            ],
            examples=["click there", "click at 900 450"],
        ),
        Tool(
            name="computer_type",
            description="Type text into the currently focused app/control (clipboard+paste).",
            script="",
            impl=COMPUTER_IMPL,
            args=[ArgSpec(name="text", kind="text", instructions="Text to type")],
            examples=["type hello into the box", "type the filename"],
        ),
        Tool(
            name="computer_key",
            description="Press a keyboard shortcut in the focused app, e.g. ctrl+s, alt+f4.",
            script="",
            impl=COMPUTER_IMPL,
            args=[ArgSpec(name="combo", kind="text", instructions="Key combo like ctrl+shift+s")],
            examples=["save with ctrl s", "press escape"],
        ),
        Tool(
            name="computer_scroll",
            description="Scroll the view in the focused app.",
            script="",
            impl=COMPUTER_IMPL,
            args=[ArgSpec(name="direction", kind="enum",
                          instructions="Scroll direction", criteria={"down": "down", "up": "up"},
                          default="down")],
            examples=["scroll down"],
        ),
        Tool(
            name="focus_window",
            description="Bring an app window to the front by title.",
            script="",
            impl=COMPUTER_IMPL,
            args=[ArgSpec(name="title", kind="text", instructions="Window title text to find")],
            examples=["focus notepad", "bring paint to the front"],
        ),
        Tool(
            name="wait_for_window",
            description="Wait until an app window with a title exists, then focus it.",
            script="",
            impl=COMPUTER_IMPL,
            args=[ArgSpec(name="title", kind="text", instructions="Title text to wait for"),
                  ArgSpec(name="timeout", kind="text", instructions="Seconds to wait", default="10")],
            examples=["wait until notepad opens"],
        ),
        Tool(
            name="minimize_all_except",
            description="Minimize every window except the named app's windows.",
            script="",
            impl=COMPUTER_IMPL,
            args=[ArgSpec(name="app", kind="text", instructions="Process or window title to keep, e.g. Code.exe")],
            examples=["minimize everything except vscode", "clean the desktop except chrome"],
        ),
        Tool(
            name="screen_read_text",
            description="Read the screen/window with OCR and return line texts with click coordinates.",
            script="",
            impl=COMPUTER_IMPL,
            examples=["what does the dialog say", "read the popup"],
        ),
        Tool(
            name="file_dialog_save",
            description="Complete the focused Save/Save-As dialog with a filename and confirm.",
            script="",
            impl=COMPUTER_IMPL,
            args=[ArgSpec(name="filename", kind="text", instructions="File name incl. extension")],
            examples=["save the file as sketch.png", "save it"],
        ),
    ]
