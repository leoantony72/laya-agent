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


def _window_match_score(w, query: str, canonical: str, exe_name: str) -> int:
    pname = (w.process_name or "").lower()
    pbase = re.sub(r"\.exe$", "", pname)
    pcompact = re.sub(r"[^a-z0-9]", "", pbase)
    title = (w.title or "").lower()

    candidates: set[str] = set()
    for value in (query, canonical):
        norm = re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()
        if norm:
            candidates.add(norm)
            candidates.add(norm.replace(" ", ""))
    stem = re.sub(r"\.exe$", "", (exe_name or "").lower())
    if stem:
        candidates.add(stem)
        candidates.add(re.sub(r"(?:32|64)$", "", stem))
    candidates = {c for c in candidates if len(c) >= 3}

    if pbase and (pbase in candidates or pcompact in candidates):
        return 4
    if pbase and any(
            pbase.startswith(c) or c.startswith(pbase) for c in candidates):
        return 3
    if (title and _window_anchor(canonical, query)
            and re.search(_window_anchor(canonical, query), title, re.I)
            and not any(b in pname for b in ("chrome", "msedge", "brave",
                                             "firefox", "opera"))):
        return 2
    return 0


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


def _process_matches(exe_name: str, canonical: str) -> bool:
    stem = re.sub(r"\.exe$", "", (exe_name or "").lower())
    stem = re.sub(r"(?:32|64)$", "", stem)
    fallback = re.sub(r"[^a-z0-9]+", "", (canonical or "").lower())
    if not stem and not fallback:
        return False
    for proc in psutil.process_iter(["name"]):
        try:
            pname = (proc.info.get("name") or "").lower()
            pbase = re.sub(r"\.exe$", "", pname)
            pcompact = re.sub(r"[^a-z0-9]", "", pbase)
            if stem and (pbase.startswith(stem) or stem.startswith(pbase)):
                return True
            if fallback and (pcompact == fallback or pcompact.startswith(fallback)):
                return True
        except Exception:
            continue
    return False


async def focus_or_launch_app(app_name: str) -> dict[str, Any]:
    """Activate a running app, or launch the resolved installed app."""
    raw = str(app_name or "").strip()
    if not raw:
        return {"success": False, "output": "Missing application name"}

    from .apps import clean_query, resolve_app, resolve_shortcut_target, suggestions

    query = clean_query(raw)
    match = await asyncio.to_thread(resolve_app, query)
    canonical = match.entry.name if match is not None else query
    exe_name = match.entry.exe_name if match is not None else ""

    # 1. Focus an existing window before launching a second copy.
    # Exact process base wins ("code" → Code.exe), then prefix, then an
    # anchored title hit. "code" must never match OpenCode.exe.
    try:
        from .computer.driver import ComputerDriver
        driver = ComputerDriver(guard_screen=False)
        best, best_score = None, 0
        for w in driver.list_windows():
            score = _window_match_score(w, query, canonical, exe_name)
            if score > best_score:
                best, best_score = w, score
        if best is not None:
            driver.focus_window(best.hwnd)
            await asyncio.sleep(0.2)
            return {
                "success": True,
                "output": f"Activated {best.process_name or canonical}",
                "window": best.to_dict(),
            }
    except Exception as e:
        log.debug(f"Native focus check error: {e}")

    # 2. No app matched: never launch a guess.
    if match is None:
        literal = None
        try:
            import shutil
            literal = shutil.which(query) or shutil.which(query + ".exe")
        except Exception:
            literal = None
        if not literal:
            options = await asyncio.to_thread(suggestions, query)
            hint = ""
            if options:
                hint = " Did you mean " + ", ".join(options) + "?"
            return {
                "success": False,
                "output": f"I couldn't find an installed app called '{query}'.{hint}",
            }

    target = match.entry.target if match is not None else literal
    if match is not None and match.entry.kind == "shortcut":
        resolved = await asyncio.to_thread(resolve_shortcut_target, target) or ""
        if resolved:
            exe_name = os.path.basename(resolved)

    res = await run_powershell(_app_launch_script(target))
    stdout = str(res.get("stdout") or "")
    stderr = str(res.get("stderr") or "").strip()
    if "__ERR__:" in stdout:
        detail = stdout.split("__ERR__:", 1)[1].strip()
        return {"success": False,
                "output": f"Couldn't launch {canonical}: {detail}"}
    if not res.get("success", False):
        detail = stderr or "launch command failed"
        return {"success": False,
                "output": f"Couldn't launch {canonical}: {detail}"}
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

    # 3. Verify the app actually started; never claim success blindly.
    try:
        from .computer.driver import ComputerDriver
        driver = ComputerDriver(guard_screen=False)
        w_res = driver.wait_for_window(
            _window_anchor(canonical, query), timeout=5.0)
        if w_res.get("success") and w_res.get("window"):
            driver.focus_window(w_res["window"]["hwnd"])
            await asyncio.sleep(0.25)
            return {
                "success": True,
                "output": f"Launched and focused {canonical}",
                "window": w_res.get("window"),
            }
    except AttributeError:
        pass
    except Exception as e:
        log.debug(f"Window wait/focus after launch error: {e}")

    if launch_pid and psutil.pid_exists(launch_pid):
        return {"success": True, "output": f"Launched {canonical}"}
    if _process_matches(exe_name, canonical):
        return {"success": True, "output": f"Launched {canonical}"}
    if match is not None and match.entry.kind in ("appid", "protocol"):
        return {"success": True, "output": f"Launched {canonical}"}
    return {
        "success": False,
        "output": f"I started {canonical}, but couldn't confirm its window appeared.",
    }


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
