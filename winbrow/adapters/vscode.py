"""
VS Code Adapter — CLI + Command Palette
========================================
[native CLI] — `code` on PATH is a rock-solid automation surface:
open folder/file (window title verify), install extensions
(--install-extension + list verify), run command ids
(--command, palette equivalents headless).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, Optional

from .base import AdapterOp

log = logging.getLogger("winbrow.adapters.vscode")


def _ps_quote(s: str) -> str:
    """Single-quote-safe embed of a value into a PowerShell argument string."""
    return "'" + str(s).replace("'", "''") + "'"


def basename(target: str) -> str:
    import os
    return os.path.basename(str(target).rstrip("\\/")) or str(target)


# --------------------------------------------------------------------------- ops

async def _code_cli(cline: str, timeout: int = 25) -> dict:
    from ..windows import run_powershell
    return await run_powershell(cline, timeout=timeout)


async def _launch_detached(target: str) -> dict:
    """Start VS Code without waiting for the CLI process.

    The `code` shim runs Code.exe synchronously and the GUI child inherits
    stdout/stderr, so capturing the CLI's output can block until VS Code
    closes. Start-Process detaches it; the window is verified separately.
    """
    from ..windows import run_powershell
    script = (
        "$cmd = Get-Command code.cmd -ErrorAction SilentlyContinue; "
        "$exe = $null; "
        "if ($cmd) { $exe = Join-Path (Split-Path (Split-Path $cmd.Source)) 'Code.exe' }; "
        "if (-not $exe -or -not (Test-Path $exe)) { "
        "  $exe = (Get-Command Code.exe -ErrorAction SilentlyContinue).Source }; "
        f"$target = {_ps_quote(target)}; "
        "if ($exe) { Start-Process -FilePath $exe -ArgumentList ('\"' + $target + '\"') } "
        "else { Start-Process -FilePath $target }; "
        'return "Launched VS Code with $target"'
    )
    return await run_powershell(script, timeout=10)


async def open_target(args: Dict[str, Any]) -> dict:
    """Open a folder or file in VS Code (launches window when not running)."""
    target = str(args.get("target", "")).strip()
    if not target:
        return {"success": False, "output": "Missing 'target' (folder or file path)"}
    import os
    if not (os.path.isdir(target) or os.path.isfile(target)):
        return {"success": False, "output": f"Path not found: {target}"}
    res = await _launch_detached(target)
    if not res.get("success"):
        return {"success": False, "output": f"Could not launch VS Code: {res.get('stderr', '')[:180]}"}
    # Verify the window when possible, but a title mismatch does not mean the
    # launch failed (fresh windows may still say "Welcome"). The task should
    # continue to the next step instead of retrying a launch that worked.
    from ..computer.driver import ComputerDriver
    driver = ComputerDriver()
    window = driver.wait_for_window(re.escape(basename(target)), timeout=12.0)
    verified = bool(window.get("success"))
    return {
        "success": True,
        "output": (f"Opened in VS Code: {target}" if verified
                   else f"VS Code launched with {target} (window not verified yet)"),
        "window": window.get("window"),
    }


async def install_extension(args: Dict[str, Any]) -> dict:
    """Install a marketplace extension id (e.g. ms-python.python)."""
    ext = str(args.get("extension_id", "")).strip()
    if not ext:
        return {"success": False, "output": "Missing 'extension_id'"}
    res = await _code_cli(
        f"$null = code --install-extension {_ps_quote(ext)} --force; code --list-extensions",
        timeout=60,
    )
    out = res.get("stdout", "")
    installed = ext.lower() in re.split(r"[\r\n]+", out.lower())
    return {
        "success": installed,
        "output": (f"Installed extension {ext}" if installed
                   else f"Extension {ext} not reported as installed ({out[:120]}...)"),
    }


async def run_command(args: Dict[str, Any]) -> dict:
    """Run a VS Code command id in the most recent window (palette substitute)."""
    command_id = str(args.get("command_id", "")).strip()
    if not command_id:
        return {"success": False, "output": "Missing 'command_id' (e.g. workbench.action.files.saveAll)"}
    res = await _code_cli(f"code --command {_ps_quote(command_id)}", timeout=15)
    return {
        "success": bool(res.get("success")),
        "output": f"Command dispatched: {command_id}" if res.get("success")
        else f"code --command failed: {res.get('stderr', '')[:120]}",
    }


async def list_extensions(args: Dict[str, Any]) -> dict:
    res = await _code_cli("code --list-extensions", timeout=20)
    return {
        "success": bool(res.get("success")),
        "output": f"{len([l for l in res.get('stdout', '').splitlines() if l.strip()])} extensions installed",
        "extensions": [l.strip() for l in res.get("stdout", "").splitlines() if l.strip()],
    }


OPS = [
    AdapterOp(
        app_id="vscode", op_id="open",
        description="Open a file or folder in VS Code.",
        examples=["open D:\\code in vscode", "code open the project folder"],
        args={"target": {"instructions": "Absolute folder/file path"}},
        fn=open_target,
    ),
    AdapterOp(
        app_id="vscode", op_id="install_extension",
        description="Install a VS Code extension from the marketplace by id.",
        examples=["install the python extension in vscode", "add ms-python extension"],
        args={"extension_id": {"instructions": "Extension id, e.g. ms-python.python"}},
        fn=install_extension,
    ),
    AdapterOp(
        app_id="vscode", op_id="run_command",
        description="Run a VS Code command id (command palette action).",
        examples=["run the save all command in code", "toggle terminal in vscode"],
        args={"command_id": {"instructions": "Command id e.g. workbench.action.terminal.toggleTerminal"}},
        fn=run_command,
    ),
    AdapterOp(
        app_id="vscode", op_id="list_extensions",
        description="List installed VS Code extensions.",
        examples=["what extensions do I have", "list code extensions"],
        args={},
        fn=list_extensions,
    ),
]
