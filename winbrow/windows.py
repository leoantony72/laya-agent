"""
Windows OS Execution Layer for WinBrow
=======================================
Equivalent to macbrow's applescript.py, tailored for Windows 10/11 using
native PowerShell, Win32 API, and system automation primitives.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import psutil

log = logging.getLogger("winbrow.windows")

# One-shot fallback is a steady state on loops without subprocess support
# (uvicorn --reload runs a SelectorEventLoop on Windows), so log it once
# instead of on every command.
_ps_fallback_logged = False


@dataclass
class WindowsContext:
    active_app: str = "Desktop"
    active_title: str = ""
    running_apps: list[str] = field(default_factory=list)
    volume_level: int = 50
    is_muted: bool = False
    dark_mode: bool = True
    battery_percent: Optional[int] = None
    battery_plugged: Optional[bool] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "active_app": self.active_app,
            "active_title": self.active_title,
            "running_apps": self.running_apps[:15],
            "volume_level": self.volume_level,
            "is_muted": self.is_muted,
            "dark_mode": self.dark_mode,
            "battery": f"{self.battery_percent}%" if self.battery_percent is not None else "N/A",
        }


# ---------------------------------------------------------------------------
# Context Detection
# ---------------------------------------------------------------------------

COMMON_APP_MAP = {
    "chrome": "Google Chrome",
    "msedge": "Microsoft Edge",
    "firefox": "Mozilla Firefox",
    "code": "Visual Studio Code",
    "notepad": "Notepad",
    "spotify": "Spotify",
    "slack": "Slack",
    "discord": "Discord",
    "explorer": "File Explorer",
    "windowsterminal": "Windows Terminal",
    "powershell": "PowerShell",
    "cmd": "Command Prompt",
    "taskmgr": "Task Manager",
    "calc": "Calculator",
    "mspaint": "Paint",
    "zoom": "Zoom",
    "teams": "Microsoft Teams",
}

SYSTEM_PROCESS_PREFIXES = (
    "svchost", "system", "registry", "smss", "csrss", "wininit", "services",
    "lsass", "conhost", "fontdrvhost", "dwm", "sihost", "ctfmon", "taskhostw",
    "runtimebroker", "shellexperiencehost", "searchhost", "startmenuexperiencehost"
)


# Brief TTL cache for Windows context (process scan + COM reads are 15-600ms).
_CTX_CACHE: dict[str, Any] = {"ctx": None, "ts": 0.0}
_CTX_TTL_S = 2.0


def get_current_windows_context(use_cache: bool = True) -> WindowsContext:
    """Capture live state of Windows: frontmost window, running apps, and system settings.

    Results are cached briefly (2s TTL) because process enumeration + COM
    volume reads cost 15-600ms and every command captures context. Pass
    use_cache=False (or call invalidate_context_cache()) after mutating
    system state such as the volume level.
    """
    now = time.monotonic()
    if use_cache and _CTX_CACHE["ctx"] is not None and (now - _CTX_CACHE["ts"]) < _CTX_TTL_S:
        return _CTX_CACHE["ctx"]
    ctx = _capture_windows_context()
    _CTX_CACHE["ctx"] = ctx
    _CTX_CACHE["ts"] = now
    return ctx


def invalidate_context_cache() -> None:
    """Drop the cached Windows context so the next read is fresh."""
    _CTX_CACHE["ctx"] = None
    _CTX_CACHE["ts"] = 0.0


def _capture_windows_context() -> WindowsContext:
    active_title = ""
    active_app = "Desktop"

    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if hwnd:
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buff = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buff, length + 1)
                active_title = buff.value.strip()

            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                try:
                    p = psutil.Process(pid.value)
                    raw_name = p.name().lower().replace(".exe", "")
                    active_app = COMMON_APP_MAP.get(raw_name, p.name().replace(".exe", ""))
                except Exception:
                    pass
    except Exception as e:
        log.debug(f"Foreground window detection error: {e}")

    # Gather user-facing running applications
    running = set()
    try:
        for proc in psutil.process_iter(["name"]):
            try:
                name = proc.info["name"]
                if not name or not name.lower().endswith(".exe"):
                    continue
                base = name.lower()[:-4]
                if any(base.startswith(sys_p) for sys_p in SYSTEM_PROCESS_PREFIXES):
                    continue
                pretty = COMMON_APP_MAP.get(base, name[:-4])
                running.add(pretty)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception as e:
        log.debug(f"Process list error: {e}")

    running_list = sorted(list(running))

    # Check battery
    battery = psutil.sensors_battery()
    bat_pct = round(battery.percent) if battery else None
    bat_plug = battery.power_plugged if battery else None

    # Check dark mode from Windows Registry
    dark_mode = True
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        dark_mode = (val == 0)
    except Exception:
        pass

    return WindowsContext(
        active_app=active_app,
        active_title=active_title,
        running_apps=running_list,
        volume_level=get_windows_volume(),
        dark_mode=dark_mode,
        battery_percent=bat_pct,
        battery_plugged=bat_plug,
    )


# ---------------------------------------------------------------------------
# Native WASAPI Volume Control
# ---------------------------------------------------------------------------

class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8)
    ]
    def __init__(self, guid_str: str):
        super().__init__()
        g = guid_str.strip("{}").split("-")
        self.Data1 = int(g[0], 16)
        self.Data2 = int(g[1], 16)
        self.Data3 = int(g[2], 16)
        d4 = g[3] + g[4]
        for i in range(8):
            self.Data4[i] = int(d4[i*2:i*2+2], 16)


_CLSID_MMDeviceEnumerator = _GUID("BCDE0395-E52F-467C-8E3D-C4579291692E")
_IID_IMMDeviceEnumerator = _GUID("A95664D2-9614-4F35-A746-DE8DB63617E6")
_IID_IAudioEndpointVolume = _GUID("5CDF2C82-841E-4546-9722-0CF74078229A")


def _parse_volume_level(raw: str) -> float:
    """Parse a volume level (0-100) from free-form text.

    Handles exact values ('100', '40%'), keywords ('max', 'min', 'mute'),
    and full sentences ('set the volume to max', 'turn it all the way up').
    Falls back to None when no level can be determined (caller decides
    default) instead of silently snapping to 50%.
    """
    text = str(raw).strip().lower()
    # Explicit keywords first (word-boundary aware so '0' doesn't match '20')
    if re.search(r"\b(max|maximum|full|all the way up|full blast|max it)\b", text) or "100%" in text:
        return 100.0
    if re.search(r"\b(min|minimum|zero|off|mute|muted|silence|silent)\b", text):
        return 0.0
    m = re.search(r"(\d{1,3})\s*%?", text)
    if m:
        try:
            return max(0.0, min(100.0, float(m.group(1))))
        except ValueError:
            pass
    return None


def set_windows_volume(level_percent: float | str) -> str:
    """Set Windows master audio volume scalar (0.0% to 100.0%) using native WASAPI COM."""
    try:
        parsed = _parse_volume_level(level_percent)
        if parsed is None:
            return (
                f"Volume error: could not determine a level from "
                f"'{level_percent}'. Say e.g. 'set volume to 40' or 'set volume to max'."
            )
        val_pct = parsed

        level_float = max(0.0, min(1.0, val_pct / 100.0))
        ole32 = ctypes.windll.ole32
        ole32.CoInitialize(None)

        enumerator = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(_CLSID_MMDeviceEnumerator),
            None,
            1,
            ctypes.byref(_IID_IMMDeviceEnumerator),
            ctypes.byref(enumerator)
        )
        if hr != 0 or not enumerator:
            return f"Failed to access MMDeviceEnumerator (HRESULT {hr})"

        vtbl_enum = ctypes.cast(ctypes.cast(enumerator, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        GetDefaultAudioEndpoint = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)
        )(vtbl_enum[4])

        device = ctypes.c_void_p()
        hr = GetDefaultAudioEndpoint(enumerator, 0, 1, ctypes.byref(device))
        if hr != 0 or not device:
            return f"Failed to get audio device (HRESULT {hr})"

        vtbl_dev = ctypes.cast(ctypes.cast(device, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        Activate = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        )(vtbl_dev[3])

        audio_vol = ctypes.c_void_p()
        hr = Activate(device, ctypes.byref(_IID_IAudioEndpointVolume), 1, None, ctypes.byref(audio_vol))
        if hr != 0 or not audio_vol:
            return f"Failed to activate AudioEndpointVolume (HRESULT {hr})"

        vtbl_vol = ctypes.cast(ctypes.cast(audio_vol, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        SetMasterVolumeLevelScalar = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.c_float, ctypes.c_void_p
        )(vtbl_vol[7])

        hr = SetMasterVolumeLevelScalar(audio_vol, ctypes.c_float(level_float), None)
        if hr == 0:
            invalidate_context_cache()
            return f"Master volume set to {int(val_pct)}%"
        else:
            return f"Failed to set volume scalar (HRESULT {hr})"
    except Exception as e:
        return f"Volume error: {e}"


def get_windows_volume() -> int:
    """Get current master audio volume percentage (0-100)."""
    try:
        ole32 = ctypes.windll.ole32
        ole32.CoInitialize(None)

        enumerator = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(_CLSID_MMDeviceEnumerator),
            None,
            1,
            ctypes.byref(_IID_IMMDeviceEnumerator),
            ctypes.byref(enumerator)
        )
        if hr != 0 or not enumerator:
            return 50

        vtbl_enum = ctypes.cast(ctypes.cast(enumerator, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        GetDefaultAudioEndpoint = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)
        )(vtbl_enum[4])

        device = ctypes.c_void_p()
        hr = GetDefaultAudioEndpoint(enumerator, 0, 1, ctypes.byref(device))
        if hr != 0 or not device:
            return 50

        vtbl_dev = ctypes.cast(ctypes.cast(device, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        Activate = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.c_ulong, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
        )(vtbl_dev[3])

        audio_vol = ctypes.c_void_p()
        hr = Activate(device, ctypes.byref(_IID_IAudioEndpointVolume), 1, None, ctypes.byref(audio_vol))
        if hr != 0 or not audio_vol:
            return 50

        vtbl_vol = ctypes.cast(ctypes.cast(audio_vol, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
        GetMasterVolumeLevelScalar = ctypes.WINFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_float)
        )(vtbl_vol[9])

        val = ctypes.c_float()
        hr = GetMasterVolumeLevelScalar(audio_vol, ctypes.byref(val))
        if hr == 0:
            return int(round(val.value * 100))
        return 50
    except Exception:
        return 50


# ---------------------------------------------------------------------------
# PowerShell & Command Execution
# ---------------------------------------------------------------------------

async def run_powershell(script: str, timeout: int = 30) -> dict[str, Any]:
    """
    Execute a PowerShell script asynchronously and return stdout, stderr, and exitcode.
    Equivalent to macbrow's osascript execution.

    Uses the persistent host (winbrow.pshost) to avoid the per-command
    process spawn. Falls back to a one-shot process only when the host
    itself is unavailable — never on script errors or timeouts, where
    re-running could double-apply side effects.
    """
    try:
        from .pshost import run_persistent
        res = await run_persistent(script, timeout)
        if res.get("success") or not str(res.get("stderr", "")).startswith(
            ("Persistent PowerShell unavailable", "Persistent host error")
        ):
            return res
        stderr = res.get("stderr", "")
        global _ps_fallback_logged
        if not _ps_fallback_logged:
            _ps_fallback_logged = True
            detail = stderr[:100] if stderr else "no detail"
            log.warning(f"Persistent host down ({detail}); using one-shot PowerShell fallback.")
        else:
            log.debug(f"Persistent host down ({stderr[:100]}); using one-shot PowerShell fallback.")
    except Exception as e:
        log.warning(f"Persistent host import/call failed ({repr(e)}); using one-shot fallback.")
    return await _run_powershell_once(script, timeout)


async def _run_powershell_once(script: str, timeout: int = 30) -> dict[str, Any]:
    """One-shot powershell.exe spawn (fallback path and cold-start warm-up).

    First tries asyncio subprocess; if the event loop does not support
    subprocesses (``NotImplementedError`` on some uvicorn/Windows combos),
    falls back to synchronous ``subprocess.run`` wrapped in an executor so
    the event loop is never blocked.
    """
    t0 = time.perf_counter()
    # Add utf-8 output encoding prefix for clean text capture
    wrapped_script = _wrap_ps_script(script)

    try:
        proc = await asyncio.create_subprocess_exec(
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-Command", wrapped_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

        out_str = stdout.decode("utf-8", errors="replace").strip()
        err_str = stderr.decode("utf-8", errors="replace").strip()

        return {
            "success": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": out_str,
            "stderr": err_str,
            "elapsed_ms": elapsed_ms,
        }
    except NotImplementedError:
        # Event loop does not support async subprocesses (e.g. uvicorn on
        # Windows with SelectorEventLoop).  Fall back to synchronous spawn.
        log.info("Async subprocess not supported; using sync fallback.")
        return await _run_powershell_sync(wrapped_script, timeout, t0)
    except asyncio.TimeoutError:
        return {
            "success": False,
            "returncode": -1,
            "stdout": "",
            "stderr": f"PowerShell command timed out after {timeout} seconds",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
    except Exception as e:
        log.warning(f"Async subprocess failed ({e}); trying sync fallback.")
        return await _run_powershell_sync(wrapped_script, timeout, t0)


def _kill_process_tree(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, timeout=5)
    except Exception:
        pass


def _wrap_ps_script(script: str) -> str:
    # PowerShell requires param() to be the first statement in a command.
    # The OCR script starts with param(), so prefixing it would break it.
    if script.lstrip().lower().startswith("param"):
        return script
    return (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$OutputEncoding = [System.Text.Encoding]::UTF8; "
        + script
    )


def _run_powershell_blocking(
    wrapped_script: str, timeout: int, t0: float,
    script_path: Optional[str] = None,
) -> dict[str, Any]:
    """Blocking PowerShell spawn with temp-file output.

    Output goes to temp files instead of pipes: GUI launchers (VS Code's
    `code` shim) leave a child holding the inherited pipe open, and
    subprocess.run() would then wait for EOF forever. Waiting only on the
    process and reading the files avoids that hang.

    ``script_path`` runs a saved .ps1 via ``-File`` (used for commands longer
    than the Windows -Command limit). Otherwise ``wrapped_script`` runs via
    ``-Command``.
    """
    argv = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
    ]
    if script_path:
        argv += ["-File", str(script_path)]
    else:
        argv += ["-Command", wrapped_script]
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=out_f,
                stderr=err_f,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            return {
                "success": False,
                "returncode": -1,
                "stdout": "",
                "stderr": f"{type(e).__name__}: {e}",
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            }
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc.pid)
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
            return {
                "success": False,
                "returncode": -1,
                "stdout": "",
                "stderr": f"PowerShell command timed out after {timeout} seconds",
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            }
        except Exception as e:
            _kill_process_tree(proc.pid)
            return {
                "success": False,
                "returncode": -1,
                "stdout": "",
                "stderr": f"{type(e).__name__}: {e}",
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            }
        out_f.seek(0)
        err_f.seek(0)
        return {
            "success": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": out_f.read().decode("utf-8", errors="replace").strip(),
            "stderr": err_f.read().decode("utf-8", errors="replace").strip(),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }


def run_powershell_sync(script: str, timeout: int = 30) -> dict[str, Any]:
    """Blocking PowerShell runner for synchronous callers (e.g. Screen.ocr).

    Never call this from the event-loop thread; async code must use
    run_powershell(). Prior to this helper, Screen.ocr called the async
    run_powershell() without awaiting it, which silently produced a
    "coroutine was never awaited" failure and no OCR output.
    """
    return _run_powershell_blocking(_wrap_ps_script(script), timeout,
                                    time.perf_counter())


async def _run_powershell_sync(
    wrapped_script: str, timeout: int, t0: float,
    script_path: Optional[str] = None,
) -> dict[str, Any]:
    """Sync PowerShell in an executor — works on any event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _run_powershell_blocking, wrapped_script, timeout, t0,
        script_path)


# ---------------------------------------------------------------------------
# Native Windows Actions
# ---------------------------------------------------------------------------

def send_media_key(key_code: str) -> bool:
    """Send Windows multimedia virtual key (play/pause, next, prev, volume)."""
    VK_MAP = {
        "play_pause": 0xB3,
        "next": 0xB0,
        "prev": 0xB1,
        "stop": 0xB2,
        "volume_mute": 0xAD,
        "volume_down": 0xAE,
        "volume_up": 0xAF,
    }
    vk = VK_MAP.get(key_code)
    if not vk:
        return False
    user32 = ctypes.windll.user32
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
    user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
    return True


async def set_dark_mode(enable: bool) -> dict[str, Any]:
    """Toggle Windows Apps Dark/Light theme via registry."""
    val = 0 if enable else 1
    script = f"""
    Set-ItemProperty -Path HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize -Name AppsUseLightTheme -Value {val}
    Set-ItemProperty -Path HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize -Name SystemUsesLightTheme -Value {val}
    return "Dark mode set to {enable}"
    """
    return await run_powershell(script)


async def open_settings_pane(pane: str) -> dict[str, Any]:
    """Open a Windows 10/11 Settings URI (e.g. ms-settings:sound)."""
    uri = f"ms-settings:{pane}" if not pane.startswith("ms-settings:") else pane
    script = f'Start-Process "{uri}"'
    return await run_powershell(script)


_START_APPS_CACHE: list[dict[str, str]] = []
_START_APPS_LOADED = False


def get_start_apps_cached() -> list[dict[str, str]]:
    """Retrieve and cache list of installed applications from Windows Get-StartApps."""
    global _START_APPS_CACHE, _START_APPS_LOADED
    if _START_APPS_LOADED and _START_APPS_CACHE:
        return _START_APPS_CACHE
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-StartApps | ConvertTo-Json"],
            capture_output=True, text=True, timeout=6,
        )
        if p.stdout.strip():
            raw = json.loads(p.stdout)
            if isinstance(raw, dict):
                _START_APPS_CACHE = [raw]
            elif isinstance(raw, list):
                _START_APPS_CACHE = raw
            _START_APPS_LOADED = True
    except Exception as e:
        log.debug(f"Failed to query Get-StartApps: {e}")
    return _START_APPS_CACHE


_GENERIC_WINDOW_TOKENS = {
    "microsoft", "google", "windows", "the", "app", "application",
    "desktop", "studio", "visual",
}


def _window_anchor(name: str, query: str = "") -> str:
    tokens = re.findall(r"[a-z0-9]+", (name or query or "").lower())
    useful = [t for t in tokens if t not in _GENERIC_WINDOW_TOKENS and len(t) >= 3]
    anchor = min(useful, key=len) if useful else (tokens[0] if tokens else "")
    return rf"\b{re.escape(anchor)}\b" if anchor else r"(?!)"


def _window_tokens(text: str) -> set[str]:
    """Meaningful lowercase tokens for process/title matching."""
    return {
        token for token in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(token) >= 3 and token not in _GENERIC_WINDOW_TOKENS
    }


def _family_tokens(query: str, canonical: str, exe_name: str,
                   aliases: tuple[str, ...] = ()) -> tuple[set[str], set[str]]:
    """Build the token/compact families an app may appear under.

    Handles process names that carry a suffix ("WhatsApp.Root.exe"), joined
    words ("ms-teams.exe"), and executable aliases ("Code.exe").
    """
    tokens: set[str] = set()
    compacts: set[str] = set()
    for value in (query, canonical, exe_name, *aliases):
        raw = re.sub(r"\.exe$", "", str(value or "").strip().lower())
        norm = re.sub(r"[^a-z0-9]+", " ", raw).strip()
        if not norm:
            continue
        tokens |= _window_tokens(norm)
        compact = norm.replace(" ", "")
        if compact:
            compacts.add(compact)
    return tokens, {c for c in compacts if len(c) >= 3}


def _window_match_score(w, query: str, canonical: str, exe_name: str,
                        aliases: tuple[str, ...] = ()) -> int:
    pname = (getattr(w, "process_name", "") or "").lower()
    pbase = re.sub(r"\.exe$", "", pname)
    pcompact = re.sub(r"[^a-z0-9]", "", pbase)
    ptokens = _window_tokens(pbase)
    title = (getattr(w, "title", "") or "").lower()
    family_tokens, family_compacts = _family_tokens(
        query, canonical, exe_name, aliases)

    # Exact process/executable wins ("code" → Code.exe, not OpenCode.exe).
    if pbase and (pbase in family_compacts or pcompact in family_compacts):
        return 6
    # A shared meaningful token covers WhatsApp.Root.exe and ms-teams.exe.
    if ptokens & family_tokens:
        return 5
    # A title hit is weaker and must not pick a browser's WebView helper.
    anchor = _window_anchor(canonical, query)
    if (title and anchor and re.search(anchor, title, re.I)
            and not any(b in pname for b in ("chrome", "msedge", "brave",
                                             "firefox", "opera"))):
        return 2
    return 0


# Shell surfaces that are top-level windows but never an app's main UI.
_NON_APP_WINDOW_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
    "XamlExplorerHostIslandWindow", "TopLevelWindowForOverflowXam",
    "SystemTray_Main", "ForegroundStaging", "tooltips_class32",
    "DummyDWMListenerWindow", "ThumbnailDeviceHelperWnd",
    "EdgeUiInputTopWndClass", "GDI+ Hook Window Class",
    "Base_PowerMessageWindow", "crashpad_SessionEndWatcher",
    "DDEMLEvent", "DDEMLMom", "OleDdeWndClass",
}
# Dynamic helper classes (the suffix is per-instance, so match the prefix).
_NON_APP_WINDOW_PREFIXES = (
    "H.NotifyIcon", ".NET-BroadcastEventWindow", "GDI+ Hook Window Class",
    "Chrome_WidgetWin_0", "Electron_NotifyIconHostWindow",
    "Electron_SystemPreferencesHostWindow",
)


def _window_rank(w, query: str, canonical: str, exe_name: str,
                 aliases: tuple[str, ...] = ()) -> Optional[tuple]:
    """Rank windows of the app family, preferring a usable main window.

    Score dominates; then we prefer uncloaked, non-tool, titled, visible,
    unowned, non-minimized and large windows. A hidden main window therefore
    beats a visible tray/tooltip helper of the same process.
    """
    score = _window_match_score(w, query, canonical, exe_name, aliases)
    if score <= 0:
        return None
    cls = str(getattr(w, "class_name", "") or "")
    if cls in _NON_APP_WINDOW_CLASSES or cls.startswith(_NON_APP_WINDOW_PREFIXES):
        return None
    rect = getattr(w, "rect", (0, 0, 0, 0)) or (0, 0, 0, 0)
    try:
        left, top, right, bottom = rect
        area = max(0, right - left) * max(0, bottom - top)
    except Exception:
        area = 0
    visible = bool(getattr(w, "is_visible", True))
    title = (getattr(w, "title", "") or "").strip()
    # A hidden, untitled, tool or zero-size window is plumbing, not UI.
    if not visible and (not title or getattr(w, "is_toolwindow", False)
                        or area <= 1):
        return None
    return (
        score,
        0 if getattr(w, "is_cloaked", False) else 1,
        0 if getattr(w, "is_toolwindow", False) else 1,
        1 if title else 0,
        1 if visible else 0,
        1 if not int(getattr(w, "owner_hwnd", 0) or 0) else 0,
        0 if getattr(w, "is_minimized", False) else 1,
        1 if area >= 200 * 150 else 0,
        area,
    )


def _list_windows_for_activation(driver):
    """Call list_windows(include_hidden=True) with old/test doubles too."""
    try:
        return driver.list_windows(include_hidden=True)
    except TypeError:
        return driver.list_windows()


def _find_app_windows(driver, query: str, canonical: str, exe_name: str,
                      aliases: tuple[str, ...] = ()) -> list:
    ranked = []
    for w in _list_windows_for_activation(driver):
        rank = _window_rank(w, query, canonical, exe_name, aliases)
        if rank is not None:
            ranked.append((rank, w))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [w for _, w in ranked]


def _activate_window(driver, hwnd: int) -> dict:
    """Use the verified activator when available (legacy doubles use focus)."""
    activate = getattr(driver, "activate_window", None)
    if callable(activate):
        return activate(hwnd)
    return driver.focus_window(hwnd)


def _activation_ok(res) -> bool:
    # Legacy drivers returned None; absence of an explicit failure is success.
    if res is None or not isinstance(res, dict):
        return True
    return bool(res.get("success", True))


async def _wait_for_visible_app_window(driver, query: str, canonical: str,
                                       exe_name: str,
                                       aliases: tuple[str, ...] = (),
                                       timeout: float = 8.0):
    """Wait until the app exposes a visible, non-cloaked window we can focus."""
    deadline = time.time() + max(0.5, timeout)
    while time.time() < deadline:
        try:
            windows = await asyncio.to_thread(
                _find_app_windows, driver, query, canonical, exe_name, aliases)
        except Exception:
            windows = []
        for w in windows:
            if not getattr(w, "is_visible", True):
                continue
            if getattr(w, "is_cloaked", False):
                continue
            res = await asyncio.to_thread(_activate_window, driver, w.hwnd)
            if _activation_ok(res):
                return w, res
        await asyncio.sleep(0.3)
    return None, None


def resolve_app_launch_target(app_name: str) -> Optional[str]:
    """Resolve a spoken app name to an exe path, shortcut, AppID or protocol."""
    from .apps import clean_query, resolve_app
    name = clean_query(app_name)
    if not name:
        return None
    match = resolve_app(name)
    return match.entry.target if match is not None else None


def _app_launch_script(target: str) -> str:
    quoted = "'" + str(target).replace("'", "''") + "'"
    return f"""
    $ErrorActionPreference = 'Stop'
    $target = {quoted}
    try {{
        $isShell = $target -like 'shell:*'
        $isProtocol = $target -match '^[a-zA-Z][a-zA-Z0-9+.-]*:'
        if ($isShell -or ($isProtocol -and -not (Test-Path -LiteralPath $target))) {{
            Start-Process -FilePath $target | Out-Null
            Write-Output "__PID__:0"
        }} else {{
            $p = Start-Process -FilePath $target -PassThru -ErrorAction Stop
            if ($p -and $p.Id) {{ Write-Output ("__PID__:" + $p.Id) }}
            else {{ Write-Output "__PID__:0" }}
        }}
    }} catch {{
        Write-Output ("__ERR__:" + $_.Exception.Message)
    }}
    """


def _process_matches(exe_name: str, canonical: str, query: str = "",
                     aliases: tuple[str, ...] = ()) -> bool:
    """True when a process in the app's family is running.

    Token-aware: "code" matches Code.exe but not OpenCode.exe; "whatsapp"
    matches WhatsApp.Root.exe; "teams" matches ms-teams.exe.
    """
    family_tokens, family_compacts = _family_tokens(
        query, canonical, exe_name, aliases)
    if not family_tokens and not family_compacts:
        return False
    for proc in psutil.process_iter(["name"]):
        try:
            pname = (proc.info.get("name") or "").lower()
            pbase = re.sub(r"\.exe$", "", pname)
            pcompact = re.sub(r"[^a-z0-9]", "", pbase)
            if pbase and (pbase in family_compacts or pcompact in family_compacts):
                return True
            if _window_tokens(pbase) & family_tokens:
                return True
        except Exception:
            continue
    return False


def _app_activation_failure(canonical: str, state: str, output: str,
                            candidates: list) -> dict[str, Any]:
    window = None
    process = ""
    if candidates:
        first = candidates[0]
        try:
            window = first.to_dict()
        except Exception:
            window = None
        process = getattr(first, "process_name", "") or ""
    return {
        "success": False,
        "state": state,
        "output": output,
        "window": window,
        "process": process,
        "app": canonical,
    }


async def focus_or_launch_app(app_name: str) -> dict[str, Any]:
    """Bring an app's *visible* window to the front, launching if needed.

    Generic activation ladder (no app-specific branches):
      1. Find a window in the app's process family — including hidden,
         minimized and tray-only windows — and show + foreground it.
      2. If no window can be activated, ask the existing instance to reveal
         itself by launching its registered activation target (protocol,
         AppID, shortcut or exe). Single-instance apps handle this by
         activating their existing window.
      3. Wait for a visible, non-cloaked window and verify it is foreground.
      4. If the process is running but no usable window appears, return
         ``state="running_no_window"``. Process existence is never success.

    A successful result always includes a verified visible foreground window.
    """
    raw = str(app_name or "").strip()
    if not raw:
        return {"success": False, "state": "not_found",
                "output": "Missing application name"}

    from .apps import (clean_query, resolve_app, resolve_shortcut_target,
                       suggestions)

    query = clean_query(raw)
    match = await asyncio.to_thread(resolve_app, query)
    canonical = match.entry.name if match is not None else query
    exe_name = match.entry.exe_name if match is not None else ""
    aliases = tuple(match.entry.aliases) if match is not None else ()
    kind = match.entry.kind if match is not None else ""
    target = match.entry.target if match is not None else ""

    # 1. No catalog match: a running window can still identify the app;
    # otherwise only launch an exact executable on PATH. Never guess.
    if match is None:
        literal = None
        try:
            import shutil
            literal = shutil.which(query) or shutil.which(query + ".exe")
        except Exception:
            literal = None
        if literal:
            target = literal
            kind = "exe"
            exe_name = os.path.basename(literal)

    if kind == "shortcut":
        resolved = await asyncio.to_thread(resolve_shortcut_target, target) or ""
        if resolved:
            exe_name = os.path.basename(resolved)

    try:
        from .computer.driver import ComputerDriver
        driver = ComputerDriver(guard_screen=False)
    except Exception as e:
        log.debug(f"Computer driver unavailable for app activation: {e}")
        return {
            "success": False,
            "state": "launch_failed",
            "output": f"I couldn't inspect windows to open {canonical}: {e}",
            "window": None,
            "process": "",
            "app": canonical,
        }

    # 2. Existing window — visible, minimized, hidden in the tray, or cloaked.
    try:
        candidates = await asyncio.to_thread(
            _find_app_windows, driver, query, canonical, exe_name, aliases)
    except Exception as e:
        log.debug(f"App window enumeration failed for {canonical}: {e}")
        candidates = []

    activation_error = ""
    for w in candidates:
        if getattr(w, "is_cloaked", False):
            activation_error = activation_error or (
                "its window is on another virtual desktop")
            continue
        res = await asyncio.to_thread(_activate_window, driver, w.hwnd)
        if _activation_ok(res):
            was_hidden = (
                not getattr(w, "is_visible", True)
                or getattr(w, "is_minimized", False)
            )
            state = "activated" if was_hidden else "focused"
            window_dict = w.to_dict() if hasattr(w, "to_dict") else {}
            if isinstance(window_dict, dict):
                # Activation is verified: report the window as it is now, not
                # as it was enumerated before ShowWindow.
                window_dict["visible"] = True
                window_dict["minimized"] = False
            return {
                "success": True,
                "state": state,
                "output": (
                    f"Activated {canonical} ({w.process_name or 'window'})"
                    if was_hidden else
                    f"Focused {canonical} ({w.process_name or 'window'})"
                ),
                "window": window_dict,
                "process": getattr(w, "process_name", "") or "",
                "app": canonical,
            }
        if isinstance(res, dict) and res.get("output"):
            activation_error = activation_error or str(res["output"])

    # No launch target. If a window exists, report the activation failure;
    # otherwise this is an unknown app — never guess.
    if not target:
        if candidates:
            detail = ""
            if activation_error:
                detail = f" {activation_error.rstrip('.')}."
            return _app_activation_failure(
                canonical, "running_no_window",
                (f"{canonical} has a window, but I couldn't bring it to the "
                 f"front.{detail}"),
                candidates)
        options = await asyncio.to_thread(suggestions, query)
        hint = ""
        if options:
            hint = " Did you mean " + ", ".join(options) + "?"
        return {
            "success": False,
            "state": "not_found",
            "output": f"I couldn't find an installed app called '{query}'.{hint}",
        }

    # 3. Ask the running instance to show itself, or launch it if absent.
    running_before = await asyncio.to_thread(
        _process_matches, exe_name, canonical, query, aliases)
    res = await run_powershell(_app_launch_script(target)) or {}
    stdout = str(res.get("stdout") or "")
    stderr = str(res.get("stderr") or "").strip()
    if "__ERR__:" in stdout:
        detail = stdout.split("__ERR__:", 1)[1].strip()
        return _app_activation_failure(
            canonical, "launch_failed",
            f"Couldn't launch {canonical}: {detail}", candidates)
    if not res.get("success", False):
        detail = stderr or "launch command failed"
        return _app_activation_failure(
            canonical, "launch_failed",
            f"Couldn't launch {canonical}: {detail}", candidates)

    launch_pid = None
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("__PID__:"):
            try:
                value = int(line.split(":", 1)[1])
                launch_pid = value if value > 0 else None
            except ValueError:
                launch_pid = None
    log.info(f"Launch request for {canonical} -> {target} (pid={launch_pid})")

    # 4. Wait for a window that is actually visible; focus it.
    found, _ = await _wait_for_visible_app_window(
        driver, query, canonical, exe_name, aliases, timeout=8.0)
    if found is not None:
        state = "activated" if (running_before or candidates) else "launched"
        verb = "Activated" if state == "activated" else "Launched and focused"
        return {
            "success": True,
            "state": state,
            "output": f"{verb} {canonical} ({found.process_name or 'window'})",
            "window": found.to_dict() if hasattr(found, "to_dict") else {},
            "process": getattr(found, "process_name", "") or "",
            "app": canonical,
        }

    # 5. Honest failure: process existence is not "open".
    process_running = await asyncio.to_thread(
        _process_matches, exe_name, canonical, query, aliases)
    if process_running or candidates:
        detail = ""
        if candidates:
            first = candidates[0]
            detail = (
                " Its window exists but could not be shown"
                f" ({getattr(first, 'process_name', '') or 'unknown process'})."
            )
        if activation_error:
            detail = f" {activation_error.rstrip('.')}."
        return _app_activation_failure(
            canonical, "running_no_window",
            (f"{canonical} is running in the background, but it has no visible "
             f"window I can type into.{detail}"),
            candidates)
    return _app_activation_failure(
        canonical, "launch_failed",
        f"I started {canonical}, but no visible window appeared.",
        candidates)


async def clean_desktop_to_folder() -> dict[str, Any]:
    """Organize desktop files into a neat 'Organized Desktop (Date)' folder."""
    script = """
    $desktop = [Environment]::GetFolderPath('Desktop')
    $target = Join-Path $desktop ("Desktop Archive " + (Get-Date -Format 'yyyy-MM-dd'))
    if (-not (Test-Path $target)) { New-Item -ItemType Directory -Path $target | Out-Null }
    $items = Get-ChildItem -Path $desktop -File | Where-Object { $_.Name -notlike "*.lnk" -and $_.Name -notlike "Desktop Archive*" }
    $count = $items.Count
    $items | Move-Item -Destination $target
    return "Moved $count loose files into $target"
    """
    return await run_powershell(script)
