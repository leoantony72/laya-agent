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



def _attach_input_desktop() -> None:
    """Attach the current thread to the interactive user input desktop."""
    try:
        hdesk = user32.OpenInputDesktop(0, False, 0x01FF)
        if hdesk:
            user32.SetThreadDesktop(hdesk)
    except Exception:
        pass

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# --- 64-bit prototypes: HGLOBAL/HANDLE are pointer-sized; without these
# ctypes truncates returns to 32-bit ints and clipboard calls fault with
# "access violation writing 0x0000000000000000" when the truncated handle
# is 0. (_set_clipboard memmove on a NULL GlobalLock pointer, etc.)
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
user32.OpenClipboard.argtypes = [ctypes.c_void_p]          # HWND owner (None)
user32.OpenClipboard.restype = wt.BOOL
user32.CloseClipboard.restype = wt.BOOL
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [wt.UINT]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wt.BOOL

VK_MENU = 0x12
SW_RESTORE = 9
SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_SHOW = 5
WM_CLOSE = 0x0010

_GW_OWNER = 4
MON_DEFAULT = -1
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_CLOAKED = 14

# --- Win32 prototypes --------------------------------------------------------
# ctypes defaults to 32-bit ints; HWNDs and handles are pointer-sized on x64.
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindow.restype = wt.HWND
user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowThreadProcessId.restype = wt.DWORD
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.SetForegroundWindow.restype = wt.BOOL
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL
user32.BringWindowToTop.argtypes = [wt.HWND]
user32.BringWindowToTop.restype = wt.BOOL
user32.SetFocus.argtypes = [wt.HWND]
user32.SetFocus.restype = wt.HWND
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindow.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.IsIconic.argtypes = [wt.HWND]
user32.IsIconic.restype = wt.BOOL
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.AttachThreadInput.restype = wt.BOOL
user32.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_size_t]
user32.keybd_event.restype = None

try:
    dwmapi = ctypes.windll.dwmapi
    dwmapi.DwmGetWindowAttribute.argtypes = [
        wt.HWND, wt.DWORD, ctypes.POINTER(wt.DWORD), wt.DWORD,
    ]
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
except Exception:  # pragma: no cover - DWM is present on Windows 7+
    dwmapi = None

# Top-level windows that are shell/tray/IME plumbing, never an app's main UI.
_HELPER_WINDOW_CLASSES = {
    "IME", "MSCTFIME UI", "tooltips_class32", "ForegroundStaging",
    "SystemTray_Main", "TopLevelWindowForOverflowXam",
    "XamlExplorerHostIslandWindow", "DummyDWMListenerWindow",
    "ThumbnailDeviceHelperWnd", "EdgeUiInputTopWndClass",
    "GDI+ Hook Window Class", "Base_PowerMessageWindow",
    "crashpad_SessionEndWatcher", "DDEMLEvent", "DDEMLMom",
    "OleDdeWndClass", "Electron_NotifyIconHostWindow",
    "Electron_SystemPreferencesHostWindow",
}


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
    is_cloaked: bool = False
    is_toolwindow: bool = False
    class_name: str = ""
    owner_hwnd: int = 0
    exe_path: str = ""

    @property
    def center(self) -> Tuple[int, int]:
        l, t, r, b = self.rect
        return ((l + r) // 2, (t + b) // 2)

    @property
    def area(self) -> int:
        l, t, r, b = self.rect
        return max(0, r - l) * max(0, b - t)

    def to_dict(self) -> dict:
        return {
            "hwnd": self.hwnd, "title": self.title, "process": self.process_name,
            "pid": self.pid, "rect": list(self.rect),
            "minimized": self.is_minimized,
            "visible": self.is_visible,
            "cloaked": self.is_cloaked,
            "tool_window": self.is_toolwindow,
            "class": self.class_name,
            "owner": self.owner_hwnd,
            "exe": self.exe_path,
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

    # ---------------------------------------------------------------- geometry
    def _cursor_pos(self) -> Tuple[int, int]:
        point = wt.POINT()
        user32.GetCursorPos(ctypes.byref(point))
        return int(point.x), int(point.y)

    @staticmethod
    def _virtual_rect() -> Tuple[int, int, int, int]:
        """Virtual desktop bounds across all monitors (x, y, w, h)."""
        return (
            int(user32.GetSystemMetrics(76)),   # SM_XVIRTUALSCREEN
            int(user32.GetSystemMetrics(77)),   # SM_YVIRTUALSCREEN
            int(user32.GetSystemMetrics(78)),   # SM_CXVIRTUALSCREEN
            int(user32.GetSystemMetrics(79)),   # SM_CYVIRTUALSCREEN
        )

    def _validate_point(self, x: int, y: int) -> str:
        """Return '' when (x, y) is safe to click, else the reason."""
        vx, vy, vw, vh = self._virtual_rect()
        if vw > 0 and vh > 0 and not (vx <= x < vx + vw and vy <= y < vy + vh):
            return f"target ({x},{y}) is outside the screen bounds"
        w, h = self._primary_size()
        corners = {
            (0, 0), (max(0, w - 1), 0),
            (0, max(0, h - 1)), (max(0, w - 1), max(0, h - 1)),
        }
        if (x, y) in corners:
            return (
                f"target ({x},{y}) is exactly on a screen corner "
                "(PyAutoGUI fail-safe point)"
            )
        return ""

    @staticmethod
    def _primary_size() -> Tuple[int, int]:
        return (int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1)))

    def _escape_failsafe_corner(self) -> bool:
        """Nudge a cursor parked in a PyAutoGUI corner a few pixels inward.

        PyAutoGUI checks the CURRENT position before every action, so a mouse
        left resting in a corner makes every click fail even when the target is
        valid. FAILSAFE stays enabled: a user who moves the mouse into a corner
        while an action runs still aborts it.
        """
        try:
            import pyautogui
            x, y = pyautogui.position()
            w, h = pyautogui.size()
        except Exception:
            return False
        x, y, w, h = int(x), int(y), int(w), int(h)
        corners = {
            (0, 0), (max(0, w - 1), 0),
            (0, max(0, h - 1)), (max(0, w - 1), max(0, h - 1)),
        }
        if (x, y) not in corners:
            return False
        nx = 2 if x < w // 2 else max(2, w - 3)
        ny = 2 if y < h // 2 else max(2, h - 3)
        user32.SetCursorPos(nx, ny)
        time.sleep(0.02)
        log.info(
            f"Cursor was in a PyAutoGUI fail-safe corner; nudged to ({nx},{ny})"
        )
        return True

    @staticmethod
    def _to_pyautogui(x: int, y: int) -> Tuple[int, int]:
        """Scale to PyAutoGUI's space if DPI awareness made the sizes differ."""
        try:
            import pyautogui
            pw, ph = pyautogui.size()
        except Exception:
            return x, y
        rw = int(user32.GetSystemMetrics(0))
        rh = int(user32.GetSystemMetrics(1))
        if rw <= 0 or rh <= 0 or (int(pw), int(ph)) == (rw, rh):
            return x, y
        return int(round(x * pw / rw)), int(round(y * ph / rh))

    @staticmethod
    def _failsafe_output(target: str = "") -> str:
        where = f" at {target}" if target else ""
        return (
            f"PyAutoGUI fail-safe triggered{where}: the cursor is in a screen "
            "corner. Move the mouse away from the corner (or use a keyboard "
            "shortcut) and retry."
        )

    # ---------------------------------------------------------------- mouse
    def click(
        self, x: Optional[int] = None, y: Optional[int] = None,
        button: str = "left", double: bool = False, move_first: bool = True,
    ) -> dict:
        """Click at coordinates (defaults: current cursor position)."""
        self._guard("click")
        import pyautogui
        target = ""
        if x is not None and y is not None:
            x, y = int(x), int(y)
            target = f"({x},{y})"
            problem = self._validate_point(x, y)
            if problem:
                return {"success": False, "output": f"Refusing click: {problem}."}
        self._escape_failsafe_corner()
        if x is not None and y is not None:
            x, y = self._to_pyautogui(x, y)
        try:
            if x is not None and y is not None and move_first:
                pyautogui.moveTo(x, y, duration=0.0)
            pyautogui.click(
                button=button, clicks=2 if double else 1,
                interval=0.05 if double else 0,
            )
        except pyautogui.FailSafeException:
            log.warning(
                f"Click fail-safe target={target or 'current cursor'} "
                f"cursor={self._cursor_pos()} screen={self._primary_size()}"
            )
            return {"success": False, "output": self._failsafe_output(target)}
        return {
            "success": True,
            "output": f"Clicked {button} at ({x}, {y})" if x is not None else "Clicked",
        }

    def move(self, x: int, y: int) -> dict:
        self._guard("move")
        import pyautogui
        x, y = int(x), int(y)
        problem = self._validate_point(x, y)
        if problem:
            return {"success": False, "output": f"Refusing move: {problem}."}
        self._escape_failsafe_corner()
        x, y = self._to_pyautogui(x, y)
        try:
            pyautogui.moveTo(x, y, duration=0.0)
        except pyautogui.FailSafeException:
            return {"success": False, "output": self._failsafe_output(f"({x},{y})")}
        return {"success": True, "output": f"Moved cursor to ({x}, {y})"}

    def drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.25) -> dict:
        self._guard("drag")
        import pyautogui
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        problem = self._validate_point(x1, y1) or self._validate_point(x2, y2)
        if problem:
            return {"success": False, "output": f"Refusing drag: {problem}."}
        self._escape_failsafe_corner()
        ax, ay = self._to_pyautogui(x1, y1)
        bx, by = self._to_pyautogui(x2, y2)
        try:
            pyautogui.moveTo(ax, ay, duration=0.0)
            pyautogui.drag(bx - ax, by - ay, duration=duration, button="left")
        except pyautogui.FailSafeException:
            return {"success": False, "output": self._failsafe_output(f"({x2},{y2})")}
        return {"success": True, "output": f"Dragged ({x1},{y1}) -> ({x2},{y2})"}

    def scroll(self, amount: int = 3, direction: str = "down", x: Optional[int] = None,
               y: Optional[int] = None) -> dict:
        self._guard("scroll")
        import pyautogui
        self._escape_failsafe_corner()
        try:
            if x is not None and y is not None:
                px, py = self._to_pyautogui(int(x), int(y))
                pyautogui.moveTo(px, py, duration=0.0)
            clicks = int(amount) * (-1 if direction == "down" else 1)
            pyautogui.scroll(clicks)
        except pyautogui.FailSafeException:
            return {"success": False, "output": self._failsafe_output()}
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
        self._escape_failsafe_corner()
        try:
            pyautogui.hotkey(*keys)
        except pyautogui.FailSafeException:
            return {"success": False, "output": self._failsafe_output()}
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
        self._escape_failsafe_corner()
        try:
            pyautogui.typewrite(text, interval=0.01)
        except pyautogui.FailSafeException:
            return {"success": False, "output": self._failsafe_output()}
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
            if not h:
                return {"success": False, "output": "GlobalAlloc failed"}
            ptr = kernel32.GlobalLock(h)
            if not ptr:
                kernel32.GlobalFree(h)
                return {"success": False, "output": "GlobalLock failed"}
            try:
                ctypes.memmove(ptr, data, len(data))
            finally:
                kernel32.GlobalUnlock(h)
                user32.SetClipboardData(CF_UNICODETEXT, h)  # h freed by system on close
            return {"success": True, "output": f"Clipboard set ({len(text)} chars)"}
        finally:
            user32.CloseClipboard()

    # ---------------------------------------------------------------- windows
    @staticmethod
    def _is_cloaked(hwnd: int) -> int:
        """DWM cloak state: 1=app, 2=shell, 4=inherited (0 = normal)."""
        if dwmapi is None:
            return 0
        value = wt.DWORD(0)
        try:
            hr = dwmapi.DwmGetWindowAttribute(
                wt.HWND(hwnd), DWMWA_CLOAKED,
                ctypes.byref(value), ctypes.sizeof(value))
            if hr == 0:
                return int(value.value)
        except Exception:
            pass
        return 0

    def list_windows(self, include_titles: bool = True,
                     include_hidden: bool = False) -> List[WindowInfo]:
        """Enumerate top-level windows.

        The default keeps the historical contract (visible windows with
        captions). ``include_hidden=True`` also returns hidden, minimized and
        cloaked windows, which is how a tray-only app is found when its main
        window still exists but is not on screen.
        """
        _attach_input_desktop()
        wins: List[WindowInfo] = []
        try:
            import psutil
        except ImportError:
            psutil = None

        proc_cache: dict[int, tuple[str, str]] = {}

        def _proc_info(pid: int) -> tuple[str, str]:
            if pid in proc_cache:
                return proc_cache[pid]
            name = exe = ""
            if psutil is not None:
                try:
                    proc = psutil.Process(pid)
                    name = proc.name()
                    if include_hidden:
                        try:
                            exe = proc.exe()
                        except Exception:
                            exe = ""
                except Exception:
                    pass
            proc_cache[pid] = (name, exe)
            return name, exe

        def _cb(hwnd, _lparam):
            visible = bool(user32.IsWindowVisible(hwnd))
            if not visible and not include_hidden:
                return True

            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            t = title.value.strip()
            if include_titles and not t and not include_hidden:
                return True

            rect = wt.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)

            exstyle = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            tool = bool(exstyle & WS_EX_TOOLWINDOW)

            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            class_name = cls.value

            if include_hidden and not visible:
                # Message-only / IME / tray plumbing can never be the app UI
                # the user asked for; skip it before it reaches ranking.
                if class_name in _HELPER_WINDOW_CLASSES:
                    return True
                if not t and (tool or area <= 1):
                    return True

            pid = wt.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            proc, exe = _proc_info(pid.value)
            wins.append(WindowInfo(
                hwnd=hwnd, title=t, process_name=proc, pid=pid.value,
                rect=(rect.left, rect.top, rect.right, rect.bottom),
                is_visible=visible,
                is_minimized=bool(user32.IsIconic(hwnd)),
                is_cloaked=bool(self._is_cloaked(hwnd)),
                is_toolwindow=tool,
                class_name=class_name,
                owner_hwnd=int(user32.GetWindow(hwnd, _GW_OWNER) or 0),
                exe_path=exe,
            ))
            return True

        user32.EnumWindows(ENUM_WND_PROC(_cb), 0)
        return wins

    def find_windows(self, title_re: str = "", process_re: str = "",
                     include_hidden: bool = False) -> List[WindowInfo]:
        tit = re.compile(title_re, re.IGNORECASE) if title_re else None
        pro = re.compile(process_re, re.IGNORECASE) if process_re else None
        out = []
        for w in self.list_windows(include_hidden=include_hidden):
            if tit and not tit.search(w.title):
                continue
            if pro and not pro.search(w.process_name or ""):
                continue
            out.append(w)
        return out

    @staticmethod
    def _force_foreground(hwnd: int) -> None:
        """Foreground-lock workaround: attach, tap ALT, raise, then focus."""
        cur_tid = kernel32.GetCurrentThreadId()
        fg_hwnd = user32.GetForegroundWindow()
        fg_tid = wt.DWORD(0)
        if fg_hwnd:
            user32.GetWindowThreadProcessId(fg_hwnd, ctypes.byref(fg_tid))
        tgt_tid = int(user32.GetWindowThreadProcessId(hwnd, None) or 0)
        attached: list[int] = []
        for tid in {int(fg_tid.value or 0), tgt_tid}:
            if tid and tid != cur_tid:
                try:
                    if user32.AttachThreadInput(cur_tid, tid, True):
                        attached.append(tid)
                except Exception:
                    pass
        try:
            user32.keybd_event(VK_MENU, 0, 0, 0)
            user32.keybd_event(VK_MENU, 0, 2, 0)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            try:
                user32.SetFocus(hwnd)
            except Exception:
                pass
        finally:
            for tid in attached:
                try:
                    user32.AttachThreadInput(cur_tid, tid, False)
                except Exception:
                    pass

    def activate_window(self, hwnd: int, timeout: float = 1.2) -> dict:
        """Show, restore and foreground a window — verified, not assumed.

        ``focus_window`` historically returned success even when Windows kept
        another window in front, which let callers type into the wrong app.
        This method only reports success once the target is visible AND is the
        foreground window.
        """
        self._guard("focus window")
        _attach_input_desktop()
        if not hwnd:
            return {"success": False, "output": "No window handle", "hwnd": 0,
                    "visible": False, "foreground": False}
        try:
            if not user32.IsWindow(hwnd):
                return {"success": False,
                        "output": f"Window {hwnd} no longer exists",
                        "hwnd": hwnd, "visible": False, "foreground": False}
        except Exception:
            pass

        cloaked = self._is_cloaked(hwnd)
        if cloaked:
            return {
                "success": False,
                "output": ("Window is cloaked (another virtual desktop or "
                           "hidden by the shell) and cannot be activated"),
                "hwnd": hwnd,
                "visible": bool(user32.IsWindowVisible(hwnd)),
                "foreground": False,
                "cloaked": cloaked,
            }

        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        else:
            user32.ShowWindow(hwnd, SW_SHOW)
        # Some apps animate the restore; give the window a few frames to show.
        for _ in range(10):
            if user32.IsWindowVisible(hwnd):
                break
            time.sleep(0.03)
        if not user32.IsWindowVisible(hwnd):
            return {"success": False,
                    "output": f"Window {hwnd} is still hidden after ShowWindow",
                    "hwnd": hwnd, "visible": False, "foreground": False}

        deadline = time.time() + max(0.2, timeout)
        while True:
            if user32.GetForegroundWindow() == hwnd:
                return {"success": True, "output": f"Focused window {hwnd}",
                        "hwnd": hwnd, "visible": True, "foreground": True}
            if time.time() >= deadline:
                break
            self._force_foreground(hwnd)
            time.sleep(0.05)
        return {
            "success": False,
            "output": (f"Window {hwnd} is visible but Windows kept another "
                       f"window in the foreground"),
            "hwnd": hwnd, "visible": True, "foreground": False,
        }

    def focus_window(self, hwnd: int) -> dict:
        """Bring a window to the foreground (restore first if minimized)."""
        res = self.activate_window(hwnd)
        if res.get("success"):
            res["output"] = f"Focused window {hwnd}"
        else:
            res["output"] = f"Could not focus window {hwnd}: {res.get('output', '')}"
        return res

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

    def wait_for_window(self, title_re: str, timeout: float = 15.0,
                        include_hidden: bool = False) -> dict:
        """Poll for a window whose title matches; returns the match."""
        pat = re.compile(title_re, re.IGNORECASE)
        deadline = time.time() + timeout
        while time.time() < deadline:
            for w in self.list_windows(include_hidden=include_hidden):
                if pat.search(w.title):
                    return {"success": True, "output": w.title,
                            "hwnd": w.hwnd, "window": w.to_dict()}
            time.sleep(0.25)
        return {"success": False,
                "output": f"No window matching r'{title_re}' within {timeout}s"}

    def get_foreground(self) -> dict:
        hwnd = user32.GetForegroundWindow()
        for w in self.list_windows(include_titles=False, include_hidden=True):
            if w.hwnd == hwnd:
                return {"success": True, "window": w.to_dict(),
                        "output": f"{w.process_name}: {w.title}"}
        return {"success": False, "output": "Foreground window not found",
                "hwnd": hwnd}
