"""
Tool Registry for WinBrow
=========================
Equivalent to macbrow's registry.py. Defines structured tools with
speculative typed arguments, script templates, and support for learned/persisted tools.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from .policy import validate_script_safety
from .windows import WindowsContext

log = logging.getLogger("winbrow.registry")

MAX_CHOICE_OPTIONS = 64
LEARNED_TOOLS_PATH = pathlib.Path(__file__).parent.parent / "tools" / "learned.json"


@dataclass
class ArgSpec:
    name: str
    kind: Literal["enum", "text"]
    instructions: str
    criteria: dict[str, str] = field(default_factory=dict)
    default: Optional[str] = None


@dataclass
class Tool:
    name: str
    description: str
    script: str  # PowerShell or Python template with {{name}} placeholders
    scope: Optional[str] = None  # Specific application (e.g. "Google Chrome") or None
    args: list[ArgSpec] = field(default_factory=list)
    speak: str = "done"  # "done" | "result" | template
    examples: list[str] = field(default_factory=list)
    is_learned: bool = False

    def choice_description(self) -> dict[str, Any]:
        """Description passed to Laya router for tool choice."""
        desc: dict[str, Any] = {
            "what": self.description,
            "examples": self.examples[:4],
        }
        if self.scope:
            desc["scope"] = f"Only when {self.scope} is relevant or focused."
        return desc

    def render_script(self, args: dict[str, str]) -> str:
        """Render script template by substituting {{arg}} with safely escaped values."""
        rendered = self.script
        for arg in self.args:
            val = args.get(arg.name, arg.default or "")
            # Escape quotes in replacement values
            safe_val = str(val).replace('"', '`"').replace("'", "''")
            rendered = rendered.replace(f"{{{{{arg.name}}}}}", safe_val)
        return rendered


class ToolRegistry:
    def __init__(self, learned_path: Optional[pathlib.Path] = None):
        self._tools: dict[str, Tool] = {}
        self.learned_path = learned_path or LEARNED_TOOLS_PATH
        self._register_builtins()
        self.load_learned()

    def register(self, tool: Tool) -> None:
        validate_script_safety(tool.script)
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def available(self, ctx: WindowsContext) -> list[Tool]:
        """Return tools available in the current Windows context."""
        tools = []
        for t in self._tools.values():
            if t.scope is None:
                tools.append(t)
            elif ctx.active_app.lower() in t.scope.lower():
                tools.append(t)
        return tools

    def all_tools(self) -> list[Tool]:
        return list(self._tools.values())

    # ------------------------------------------------------------------ Built-ins
    def _register_builtins(self) -> None:
        # 1. Volume & Audio
        self.register(Tool(
            name="system_volume",
            description="Adjust system audio volume, mute, or unmute speaker sound.",
            args=[
                ArgSpec(
                    name="action",
                    kind="enum",
                    instructions="Volume action requested",
                    criteria={
                        "up": "increase volume, louder, turn it up",
                        "down": "decrease volume, quieter, lower sound",
                        "mute": "mute sound, silence, quiet",
                        "unmute": "unmute, restore sound",
                    },
                    default="up",
                )
            ],
            script="""
            $w = New-Object -ComObject WScript.Shell
            switch ("{{action}}") {
                "up"     { for($i=0; $i -lt 5; $i++) { $w.SendKeys([char]175) }; return "Volume increased" }
                "down"   { for($i=0; $i -lt 5; $i++) { $w.SendKeys([char]174) }; return "Volume decreased" }
                "mute"   { $w.SendKeys([char]173); return "Volume muted" }
                "unmute" { $w.SendKeys([char]173); return "Volume unmuted" }
                default  { return "Volume adjusted" }
            }
            """,
            examples=["volume up", "turn it down", "mute the sound", "unmute audio"],
        ))

        # 2. Media Controls
        self.register(Tool(
            name="media_playback",
            description="Control media playback like play, pause, next track, or previous song.",
            args=[
                ArgSpec(
                    name="operation",
                    kind="enum",
                    instructions="Which playback control?",
                    criteria={
                        "play_pause": "play or pause music, resume track",
                        "next": "next song, skip track",
                        "prev": "previous song, go back a track",
                    },
                    default="play_pause",
                )
            ],
            script="""
            $w = New-Object -ComObject WScript.Shell
            switch ("{{operation}}") {
                "play_pause" { $w.SendKeys([char]179); return "Toggled play/pause" }
                "next"       { $w.SendKeys([char]176); return "Skipped to next track" }
                "prev"       { $w.SendKeys([char]177); return "Returned to previous track" }
                default      { return "Media key sent" }
            }
            """,
            examples=["pause the music", "play", "skip song", "next track", "previous track"],
        ))

        # 3. Theme Toggle (Dark / Light Mode)
        self.register(Tool(
            name="toggle_dark_mode",
            description="Switch Windows system and app appearance between dark theme and light theme.",
            args=[
                ArgSpec(
                    name="mode",
                    kind="enum",
                    instructions="Which theme mode?",
                    criteria={
                        "dark": "turn on dark mode, make it dark",
                        "light": "turn on light mode, make it light",
                        "toggle": "switch or toggle theme",
                    },
                    default="toggle",
                )
            ],
            script="""
            $path = "HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize"
            $current = (Get-ItemProperty -Path $path -Name "AppsUseLightTheme" -ErrorAction SilentlyContinue).AppsUseLightTheme
            $target = if ("{{mode}}" -eq "dark") { 0 } elseif ("{{mode}}" -eq "light") { 1 } else { if ($current -eq 0) { 1 } else { 0 } }
            Set-ItemProperty -Path $path -Name "AppsUseLightTheme" -Value $target
            Set-ItemProperty -Path $path -Name "SystemUsesLightTheme" -Value $target
            $name = if ($target -eq 0) { "Dark" } else { "Light" }
            return "Switched Windows to $name mode"
            """,
            examples=["turn on dark mode", "switch to light mode", "toggle theme"],
        ))

        # 4. App Launch & Activate
        self.register(Tool(
            name="app_focus_or_launch",
            description="Open an application, or bring its window to the front if already open.",
            args=[
                ArgSpec(
                    name="app_name",
                    kind="text",
                    instructions="Which application to open or focus?",
                    default="notepad",
                )
            ],
            script="""
            $target = "{{app_name}}".Trim()
            $proc = Get-Process | Where-Object { $_.ProcessName -like "*$target*" -or $_.MainWindowTitle -like "*$target*" } | Select-Object -First 1
            if ($proc -and $proc.MainWindowHandle -ne 0) {
                $ws = New-Object -ComObject WScript.Shell
                $ws.AppActivate($proc.Id) | Out-Null
                return "Brought $($proc.ProcessName) to focus"
            } else {
                Start-Process $target -ErrorAction SilentlyContinue
                return "Launched $target"
            }
            """,
            examples=["open chrome", "switch to vs code", "launch notepad", "bring spotify to front"],
        ))

        # 5. Window Management
        self.register(Tool(
            name="window_action",
            description="Manage the active window: minimize, maximize, snap left/right, show desktop.",
            args=[
                ArgSpec(
                    name="action",
                    kind="enum",
                    instructions="Which window management action?",
                    criteria={
                        "minimize": "minimize current window",
                        "maximize": "maximize current window, full screen",
                        "snap_left": "snap window to left half",
                        "snap_right": "snap window to right half",
                        "desktop": "show desktop, minimize all windows",
                        "close": "close active window",
                    },
                    default="minimize",
                )
            ],
            script="""
            $w = New-Object -ComObject WScript.Shell
            switch ("{{action}}") {
                "minimize"   { $w.SendKeys("% n"); return "Minimized window" }
                "maximize"   { $w.SendKeys("% x"); return "Maximized window" }
                "desktop"    { (New-Object -ComObject Shell.Application).ToggleDesktop(); return "Toggled desktop" }
                "close"      { $w.SendKeys("%{F4}"); return "Closed window" }
                default      { return "Window action completed" }
            }
            """,
            examples=["minimize this window", "maximize window", "show desktop", "close active window"],
        ))

        # 6. Organize Desktop Loose Files
        self.register(Tool(
            name="organize_desktop",
            description="Clean and organize loose files on Desktop into a neat dated folder.",
            args=[],
            script="""
            $desktop = [Environment]::GetFolderPath('Desktop')
            $folderName = "Desktop Archive " + (Get-Date -Format 'yyyy-MM-dd')
            $target = Join-Path $desktop $folderName
            if (-not (Test-Path $target)) { New-Item -ItemType Directory -Path $target | Out-Null }
            $files = Get-ChildItem -Path $desktop -File | Where-Object { $_.Name -notlike "*.lnk" -and $_.Name -notlike "Desktop Archive*" }
            $count = $files.Count
            $files | Move-Item -Destination $target
            return "Organized $count files into $folderName"
            """,
            examples=["clean up my desktop", "organize desktop files", "tidy up desktop"],
        ))

        # 7. Web Search / URL Navigation
        self.register(Tool(
            name="web_search",
            description="Search the web using Google or navigate directly to a website.",
            args=[
                ArgSpec(
                    name="query",
                    kind="text",
                    instructions="What to search for or URL to navigate to?",
                    default="google.com",
                )
            ],
            script="""
            $q = "{{query}}".Trim()
            if ($q -match "^https?://") {
                Start-Process $q
                return "Opened $q"
            } else {
                $enc = [Uri]::EscapeDataString($q)
                Start-Process "https://www.google.com/search?q=$enc"
                return "Searched web for '$q'"
            }
            """,
            examples=["search google for weather in Tokyo", "open youtube.com", "search for latest news"],
        ))

        # 8. Windows Settings Panes
        self.register(Tool(
            name="windows_settings",
            description="Open specific Windows 10/11 Settings panes like sound, network, bluetooth, or display.",
            args=[
                ArgSpec(
                    name="pane",
                    kind="enum",
                    instructions="Which settings pane?",
                    criteria={
                        "sound": "sound, audio, microphone settings",
                        "network": "wi-fi, internet, network status",
                        "bluetooth": "bluetooth and other devices",
                        "display": "screen resolution, monitor, display",
                        "batterysaver": "battery and power settings",
                        "appsfeatures": "installed apps and programs",
                    },
                    default="sound",
                )
            ],
            script="""
            Start-Process "ms-settings:{{pane}}"
            return "Opened Windows {{pane}} settings"
            """,
            examples=["open sound settings", "check wifi settings", "open bluetooth settings", "display settings"],
        ))

        # 9. Clipboard Operations
        self.register(Tool(
            name="clipboard_inspect",
            description="Get, show, or clear the current Windows clipboard contents.",
            args=[
                ArgSpec(
                    name="action",
                    kind="enum",
                    instructions="Action on clipboard",
                    criteria={
                        "get": "show or read clipboard content",
                        "clear": "clear or empty clipboard",
                    },
                    default="get",
                )
            ],
            script="""
            if ("{{action}}" -eq "clear") {
                Set-Clipboard -Value ""
                return "Clipboard cleared"
            } else {
                $c = Get-Clipboard
                if ($c) { return "Clipboard: $c" } else { return "Clipboard is empty" }
            }
            """,
            examples=["what is in my clipboard", "show clipboard", "clear clipboard"],
        ))

        # 10. Wi-Fi Password Retrieval
        self.register(Tool(
            name="get_wifi_password",
            description="Retrieve the stored Wi-Fi password/security key for current or saved wireless networks.",
            args=[
                ArgSpec(
                    name="profile",
                    kind="text",
                    instructions="Wi-Fi network SSID or leave empty for current network",
                    default="",
                )
            ],
            script="""
            $target = "{{profile}}".Trim()
            if (-not $target) {
                $cur = (netsh wlan show interfaces) | Select-String "SSID\\s*:\\s*(.+)" | ForEach-Object { $_.Matches[0].Groups[1].Value.Trim() } | Select-Object -First 1
                if ($cur) { $target = $cur }
            }
            if ($target) {
                $xml = netsh wlan show profile name="$target" key=clear 2>$null
                $pass = $xml | Select-String "Key Content\\s*:\\s*(.+)" | ForEach-Object { $_.Matches[0].Groups[1].Value.Trim() } | Select-Object -First 1
                if ($pass) { return "Wi-Fi '$target' Password: $pass" }
                else { return "No password required or not found for '$target'" }
            } else {
                $profiles = (netsh wlan show profiles) | Select-String "All User Profile\\s*:\\s*(.+)" | ForEach-Object { $_.Matches[0].Groups[1].Value.Trim() }
                return "Known Wi-Fi networks: " + ($profiles -join ", ")
            }
            """,
            examples=["show my wifi password", "what is my wifi password", "wifi security key", "get wifi pass"],
        ))

        # 11. Battery Status & Health Report
        self.register(Tool(
            name="battery_health",
            description="Show battery charge percentage, power status, or generate a full battery health diagnostic report.",
            args=[
                ArgSpec(
                    name="action",
                    kind="enum",
                    instructions="Battery action",
                    criteria={
                        "status": "battery status, percentage, charge level",
                        "report": "detailed battery health report, degradation, cycles",
                    },
                    default="status",
                )
            ],
            script="""
            if ("{{action}}" -eq "report") {
                $reportPath = "$env:TEMP\\battery-report.html"
                powercfg /batteryreport /output "$reportPath" | Out-Null
                if (Test-Path $reportPath) {
                    Start-Process "$reportPath"
                    return "Generated and opened full battery diagnostic report"
                }
            }
            $b = Get-CimInstance -ClassName Win32_Battery -ErrorAction SilentlyContinue
            if ($b) {
                $status = switch ($b.BatteryStatus) { 1 {"Discharging"} 2 {"On AC Power"} 3 {"Fully Charged"} 4 {"Low"} 5 {"Critical"} 6 {"Charging"} default {"Unknown"} }
                return "Battery: $($b.EstimatedChargeRemaining)% ($status), Est. Runtime: $($b.EstimatedRunTime) mins"
            } else {
                return "No physical battery detected (Desktop PC or virtualized system)"
            }
            """,
            examples=["battery health", "show battery level", "generate battery report", "how much battery is left"],
        ))

        # 12. System Specs & Performance
        self.register(Tool(
            name="system_spec_info",
            description="Display system specifications: CPU usage, RAM memory, disk space, Windows version, and uptime.",
            args=[],
            script="""
            $os = Get-CimInstance Win32_OperatingSystem
            $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
            $uptime = (Get-Date) - $os.LastBootUpTime
            $days = $uptime.Days
            $hours = $uptime.Hours
            $mins = $uptime.Minutes
            $totalRam = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
            $freeRam = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
            $usedRam = [math]::Round($totalRam - $freeRam, 1)
            $c = Get-PSDrive C
            $freeDisk = [math]::Round($c.Free / 1GB, 1)
            $totalDisk = [math]::Round(($c.Used + $c.Free) / 1GB, 1)
            return "OS: $($os.Caption) (Build $($os.BuildNumber))`nCPU: $($cpu.Name.Trim())`nRAM: ${usedRam}GB / ${totalRam}GB used`nDisk C: ${freeDisk}GB free of ${totalDisk}GB`nUptime: ${days}d ${hours}h ${mins}m"
            """,
            examples=["system specs", "show system info", "how much ram do i have", "system uptime", "disk space left"],
        ))

        # 13. Clean Temporary Files
        self.register(Tool(
            name="clean_temp_files",
            description="Safely delete temporary files and cache from Windows Temp directories to free up disk space.",
            args=[],
            script="""
            $tempPaths = @($env:TEMP, "$env:SystemRoot\\Temp")
            $freedCount = 0
            foreach ($p in $tempPaths) {
                if (Test-Path $p) {
                    Get-ChildItem -Path $p -Recurse -Force -ErrorAction SilentlyContinue |
                        Where-Object { -not $_.PSIsContainer } |
                        ForEach-Object {
                            try { Remove-Item $_.FullName -Force -ErrorAction Stop; $freedCount++ } catch {}
                        }
                }
            }
            return "Cleaned up $freedCount temporary files from cache"
            """,
            examples=["clean temp files", "clear temporary files", "clean cache to free space", "empty temp"],
        ))

        # 14. Kill Unresponsive or Specific Process
        self.register(Tool(
            name="kill_unresponsive_process",
            description="Force terminate a frozen, hung, or specific application process.",
            args=[
                ArgSpec(
                    name="target",
                    kind="text",
                    instructions="Process name to terminate",
                    default="hung",
                )
            ],
            script="""
            $t = "{{target}}".Trim()
            if ($t -eq "hung" -or $t -eq "unresponsive") {
                $hung = Get-Process | Where-Object { $_.Responding -eq $false }
                if ($hung) {
                    $hung | Stop-Process -Force
                    return "Killed unresponsive processes: " + ($hung.ProcessName -join ", ")
                } else {
                    return "No unresponsive processes detected"
                }
            } else {
                $procs = Get-Process | Where-Object { $_.ProcessName -like "*$t*" }
                if ($procs) {
                    $procs | Stop-Process -Force
                    return "Terminated $($procs.Count) instance(s) of $t"
                } else {
                    return "No running process matched '$t'"
                }
            }
            """,
            examples=["kill unresponsive apps", "force close app", "terminate frozen process", "kill task notepad"],
        ))

        # 15. Network Status & IP Config
        self.register(Tool(
            name="network_status",
            description="Check internet connectivity, ping latency, local IP address, and active network adapters.",
            args=[],
            script="""
            $ip = (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notlike "*Loopback*" -and $_.IPAddress -notlike "169.254*" } | Select-Object -First 1).IPAddress
            $ping = Test-Connection -ComputerName 1.1.1.1 -Count 1 -Quiet -ErrorAction SilentlyContinue
            $status = if ($ping) { "Online (Ping OK)" } else { "Offline / Unreachable" }
            return "Internet: $status | Local IP: $ip"
            """,
            examples=["check internet connection", "network status", "what is my ip", "ping test"],
        ))


    # ------------------------------------------------------------------ Persistence
    def load_learned(self) -> None:
        """Load dynamically generated/learned tools from tools/learned.json."""
        if not self.learned_path.exists():
            return
        try:
            with open(self.learned_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                args = [
                    ArgSpec(
                        name=a["name"],
                        kind=a.get("kind", "text"),
                        instructions=a.get("instructions", ""),
                        criteria=a.get("criteria", {}),
                        default=a.get("default"),
                    )
                    for a in item.get("args", [])
                ]
                tool = Tool(
                    name=item["name"],
                    description=item["description"],
                    script=item["script"],
                    scope=item.get("scope"),
                    args=args,
                    speak=item.get("speak", "done"),
                    examples=item.get("examples", []),
                    is_learned=True,
                )
                self.register(tool)
            log.info(f"Loaded {len(data)} learned tools from {self.learned_path}")
        except Exception as e:
            log.warning(f"Failed to load learned tools: {e}")

    def save_learned(self, tool: Tool) -> None:
        """Save a new dynamically generated tool to tools/learned.json for instant reuse."""
        tool.is_learned = True
        self.register(tool)
        self.learned_path.parent.mkdir(parents=True, exist_ok=True)

        existing = []
        if self.learned_path.exists():
            try:
                with open(self.learned_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = []

        # Filter out existing with same name
        existing = [item for item in existing if item["name"] != tool.name]

        tool_dict = {
            "name": tool.name,
            "description": tool.description,
            "script": tool.script,
            "scope": tool.scope,
            "speak": tool.speak,
            "examples": tool.examples,
            "args": [
                {
                    "name": a.name,
                    "kind": a.kind,
                    "instructions": a.instructions,
                    "criteria": a.criteria,
                    "default": a.default,
                }
                for a in tool.args
            ],
        }
        existing.append(tool_dict)

        with open(self.learned_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        log.info(f"Persisted learned tool '{tool.name}' to {self.learned_path}")
