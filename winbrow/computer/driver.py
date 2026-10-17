"""
Computer Driver — Native Windows Input & Window Control
========================================================
The single surface the brain (and any skill) uses to control the mouse,
keyboard, clipboard, and windows of ANY application — including ones with
weak UIA support (Paint, Photoshop panels, custom dialogs).

Implementation notes:
  • DPI-aware: the process opts into per-monitor v2 awareness at import so
    screen coordinates from UIA/mss/OCR all agree with mouse coordinates.
  • Keyboard unicode goes clipboard+Ctrl+V (fast, layout-proof).
  • Window actions work on HWND handles discovered via EnumWindows.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("winbrow.computer.driver")

# --- process-wide DPI awareness (must be set before coordinate math) ---------
def _set_dpi_awareness() -> None:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor v2
    except Exception:
        pass


user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

VK_MENU = 0x12
SW_RESTORE = 9
SW_MINIMIZE = 6
SW_MAXIMIZE = 3
WM_CLOSE = 0x0010

_GW_OWNER = 4
MON_DEFAULT = -1


# ---------------------------------------------------------------------------
# Window handle structures
# ---------------------------------------------------------------------------

@dataclass
class WindowInfo:
    hwnd: int
    title: str
    process_name: str = ""
    pid: int = 0
    rect: Tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, right, bottom
    is_visible: bool = True
    is_minimized: bool = False

    @property
    def center(self) -> Tuple[int, int]:
        l, t, r, b = self.rect
        return ((l + r) // 2, (t + b) // 2)

    def to_dict(self) -> dict:
        return {
            "hwnd": self.hwnd, "title": self.title, "process": self.process_name,
            "pid": self.pid, "rect": list(self.rect),
            "minimized": self.is_minimized,
        }


ENUM_WND_PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


# ---------------------------------------------------------------------------
# ComputerDriver
# ---------------------------------------------------------------------------

class ComputerDriver:
    """Mouse / keyboard / clipboard / window control for native apps."""

    def __init__(self, guard_screen: bool = True):
        _set_dpi_awareness()
        self._locked = False
        # guard: refuse input while the workstation is locked (nothing to control)
        try:
            self._locked = bool(user32.GetForegroundWindow() == 0)
        except Exception:
            self._locked = False

    # ---------------------------------------------------------------- utilities
    def _guard(self, what: str) -> None:
        if self._locked:
            raise RuntimeError(f"Workstation locked; cannot {what}")

    @property
    def screen_size(self) -> Tuple[int, int]:
        return (user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))

    # ---------------------------------------------------------------- mouse
    def click(
        self, x: Optional[int] = None, y: Optional[int] = None,
        button: str = "left", double: bool = False, move_first: bool = True,
    ) -> dict:
        """Click at coordinates (defaults: current cursor position)."""
        self._guard("click")
        import pyautogui
        if x is not None and y is not None:
            pyautogui.moveTo(int(x), int(y), duration=0.0) if move_first else None
        pyautogui.click(button=button, clicks=2 if double else 1, interval=0.05 if double else 0)
        return {"success": True, "output": f"Clicked {button} at ({x}, {y})" if x is not None else "Clicked"}

    def move(self, x: int, y: int) -> dict:
        self._guard("move")
        import pyautogui
        pyautogui.moveTo(int(x), int(y), duration=0.0)
        return {"success": True, "output": f"Moved cursor to ({x}, {y})"}

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.25) -> dict:
        self._guard("drag")
        import pyautogui
        pyautogui.moveTo(int(x1), int(y1), duration=0.0)
        pyautogui.drag(int(x2) - int(x1), int(y2) - int(y1), duration=duration, button="left")
        return {"success": True, "output": f"Dragged ({x1},{y1}) -> ({x2},{y2})"}

    def scroll(self, amount: int = 3, direction: str = "down", x: Optional[int] = None,
               y: Optional[int] = None) -> dict:
        self._guard("scroll")
        import pyautogui
        if x is not None and y is not None:
            pyautogui.moveTo(int(x), int(y), duration=0.0)
        clicks = int(amount) * (-1 if direction == "down" else 1)
        pyautogui.scroll(clicks)
        return {"success": True, "output": f"Scrolled {direction} {amount}"}

    # ---------------------------------------------------------------- keyboard
    _KEY_ALIASES = {
        "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift",
        "win": "win", "cmd": "win", "esc": "esc", "escape": "esc",
        "enter": "enter", "return": "enter", "tab": "tab", "space": "space",
        "backspace": "backspace", "delete": "delete", "del": "delete",
        "up": "up", "down": "down", "left": "left", "right": "right",
        "home": "home", "end": "end", "pageup": "pageup", "pagedown": "pagedown",
        "f1": "f1", "f2": "f2", "f3": "f3", "f4": "f4", "f5": "f5",
        "f6": "f6", "f7": "f7", "f8": "f8", "f9": "f9", "f10": "f10",
        "f11": "f11", "f12": "f12", "insert": "insert", "printscreen": "printscreen",
    }

    @classmethod
    def parse_key_combo(cls, combo: str) -> List[str]:
        """'ctrl+shift+s' → ['ctrl', 'shift', 's']; validates & normalizes."""
        if not combo or not combo.strip():
            raise ValueError("Empty key combo")
        keys = []
        for part in re.split(r"[+\-\s]+", combo.strip().lower()):
            part = part.strip().strip("+")
            if not part:
                continue
            key = cls._KEY_ALIASES.get(part, part)
            if key not in cls._KEY_ALIASES.values() and (len(key) != 1 or not key.isprintable()):
                raise ValueError(f"Unknown key '{part}' in combo '{combo}'")
            keys.append(key)
        if not keys:
            raise ValueError(f"No keys parsed from '{combo}'")
        return keys

    def press_key(self, combo: str) -> dict:
        """Press a key combo like 'ctrl+s', 'alt+f4', 'enter'."""
        self._guard("press keys")
        import pyautogui
        keys = self.parse_key_combo(combo)
        pyautogui.hotkey(*keys)
        return {"success": True, "output": f"Pressed {'+'.join(keys)}"}

    def type_text(self, text: str, via_clipboard: bool = True) -> dict:
        """Type text into the focused control. Clipboard+paste is layout-proof."""
        self._guard("type")
        if not text:
            return {"success": True, "output": "Nothing to type"}
        if via_clipboard:
            self.set_clipboard(text)
            time.sleep(0.05)
            return self.press_key("ctrl+v")
        import pyautogui
        pyautogui.typewrite(text, interval=0.01)
        return {"success": True, "output": f"Typed {len(text)} chars"}

    def paste(self, text: Optional[str] = None) -> dict:
        """Put text in the clipboard and paste into the focused app."""
        if text is not None:
            self.set_clipboard(text)
        return self.press_key("ctrl+v")

    # ---------------------------------------------------------------- clipboard
    def get_clipboard(self) -> str:
        CF_UNICODETEXT = 13
        if not user32.OpenClipboard(None):
            return ""
        try:
            if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                return ""
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ""
            ptr = kernel32.GlobalLock(h)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()

    def set_clipboard(self, text: str) -> dict:
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        if not text:
            text = " "
        data = text.encode("utf-16-le") + b"\x00\x00"
        if not user32.OpenClipboard(None):
            raise RuntimeError("Could not open clipboard")
        try:
            user32.EmptyClipboard()
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            ptr = kernel32.GlobalLock(h)
            ctypes.memmove(ptr, data, len(data))
            kernel32.GlobalUnlock(h)
            user32.SetClipboardData(CF_UNICODETEXT, h)
            return {"success": True, "output": f"Clipboard set ({len(text)} chars)"}
        finally:
            user32.CloseClipboard()

    # ---------------------------------------------------------------- windows
    def list_windows(self, include_titles: bool = True) -> List[WindowInfo]:
        """All visible top-level windows with captions."""
        wins: List[WindowInfo] = []
        try:
            import psutil
        except ImportError:
            psutil = None

        def _cb(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                title = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, title, 512)
                t = title.value
                if t.strip():
                    pid = wt.DWORD(0)
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    rect = wt.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    proc = ""
                    if psutil is not None:
                        try:
                            proc = psutil.Process(pid.value).name()
                        except Exception:
                            proc = ""
                    wins.append(WindowInfo(
                        hwnd=hwnd, title=t, process_name=proc, pid=pid.value,
                        rect=(rect.left, rect.top, rect.right, rect.bottom),
                        is_minimized=bool(user32.IsIconic(hwnd)),
                    ))
            return True

        user32.EnumWindows(ENUM_WND_PROC(_cb), 0)
        return wins

    def find_windows(self, title_re: str = "", process_re: str = "") -> List[WindowInfo]:
        tit = re.compile(title_re, re.IGNORECASE) if title_re else None
        pro = re.compile(process_re, re.IGNORECASE) if process_re else None
        out = []
        for w in self.list_windows():
            if tit and not tit.search(w.title):
                continue
            if pro and not pro.search(w.process_name or ""):
                continue
            out.append(w)
        return out

    def focus_window(self, hwnd: int) -> dict:
        """Bring a window to the foreground (restore first if minimized)."""
        self._guard("focus window")
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.1)
        if user32.GetForegroundWindow() != hwnd:
            # Foreground-lock workaround: brief ALT tap + SetForegroundWindow
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.keybd_event(VK_MENU, 0, 2, 0)
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.05)
        ok = user32.GetForegroundWindow() == hwnd
        time.sleep(0.15)
        return {"success": True, "output": f"Focused window {hwnd}" + ("" if ok else " (may be partial)")}

    def window_action(self, hwnd: int, action: str) -> dict:
        actions = {
            "minimize": (SW_MINIMIZE, "Minimized"),
            "maximize": (SW_MAXIMIZE, "Maximized"),
            "restore": (SW_RESTORE, "Restored"),
        }
        if action == "close":
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            return {"success": True, "output": "Closed window"}
        if action not in actions:
            raise ValueError(f"Unknown window action '{action}'")
        flag, msg = actions[action]
        user32.ShowWindow(hwnd, flag)
        time.sleep(0.1)
        return {"success": True, "output": msg}

    def minimize_all_except(self, process_re: str) -> dict:
        """Minimize every visible app window except processes matching process_re."""
        self._guard("minimize windows")
        pro = re.compile(process_re, re.IGNORECASE)
        kept, minimized = [], 0
        for w in self.list_windows():
            if pro.search(w.process_name or ""):
                kept.append(w.title[:40])
                continue
            # skip our own agent windows
            if "winbrow" in (w.process_name or "").lower():
                continue
            user32.ShowWindow(w.hwnd, SW_MINIMIZE)
            minimized += 1
        return {
            "success": True,
            "output": f"Minimized {minimized} windows; kept {len(kept)}: {', '.join(kept[:5])}",
        }

    def wait_for_window(self, title_re: str, timeout: float = 15.0) -> dict:
        """Poll for a window whose title matches; returns the match."""
        pat = re.compile(title_re, re.IGNORECASE)
        deadline = time.time() + timeout
        while time.time() < deadline:
            for w in self.list_windows():
                if pat.search(w.title):
                    return {"success": True, "output": w.title,
                            "hwnd": w.hwnd, "window": w.to_dict()}
            time.sleep(0.25)
        return {"success": False,
                "output": f"No window matching r'{title_re}' within {timeout}s"}

    def get_foreground(self) -> dict:
        hwnd = user32.GetForegroundWindow()
        for w in self.list_windows():
            if w.hwnd == hwnd:
                return {"success": True, "window": w.to_dict(),
                        "output": f"{w.process_name}: {w.title}"}
        return {"success": False, "output": "Foreground window not found"}
