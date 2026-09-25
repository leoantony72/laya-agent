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


def get_current_windows_context() -> WindowsContext:
    """Capture live state of Windows: frontmost window, running apps, and system settings."""
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
        dark_mode=dark_mode,
        battery_percent=bat_pct,
        battery_plugged=bat_plug,
    )


# ---------------------------------------------------------------------------
# PowerShell & Command Execution
# ---------------------------------------------------------------------------

async def run_powershell(script: str, timeout: int = 30) -> dict[str, Any]:
    """
    Execute a PowerShell script asynchronously and return stdout, stderr, and exitcode.
    Equivalent to macbrow's osascript execution.
    """
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
