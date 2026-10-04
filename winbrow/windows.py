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
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import psutil

log = logging.getLogger("winbrow.windows")


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
        log.warning("Persistent host down; using one-shot PowerShell fallback.")
    except Exception as e:
        log.warning(f"Persistent host import/call failed ({e}); using one-shot fallback.")
    return await _run_powershell_once(script, timeout)


async def _run_powershell_once(script: str, timeout: int = 30) -> dict[str, Any]:
    """One-shot powershell.exe spawn (fallback path and cold-start warm-up)."""
    t0 = time.perf_counter()
    # Add utf-8 output encoding prefix for clean text capture
    wrapped_script = (
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$OutputEncoding = [System.Text.Encoding]::UTF8; "
        + script
    )

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
    except asyncio.TimeoutError:
        return {
            "success": False,
            "returncode": -1,
            "stdout": "",
            "stderr": f"PowerShell command timed out after {timeout} seconds",
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
    except Exception as e:
        return {
            "success": False,
            "returncode": -1,
            "stdout": "",
            "stderr": str(e),
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        }


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


async def focus_or_launch_app(app_name: str) -> dict[str, Any]:
    """Activate window if running, or launch executable."""
    script = f"""
    $name = "{app_name}"
    $proc = Get-Process | Where-Object {{ $_.ProcessName -like "*$name*" -or $_.MainWindowTitle -like "*$name*" }} | Select-Object -First 1
    if ($proc -and $proc.MainWindowHandle -ne 0) {{
        $wshell = New-Object -ComObject WScript.Shell
        $wshell.AppActivate($proc.Id) | Out-Null
        return "Activated $($proc.ProcessName)"
    }} else {{
        Start-Process $name -ErrorAction Stop
        return "Launched $name"
    }}
    """
    return await run_powershell(script)


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
