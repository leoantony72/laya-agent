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


async def open_target(args: Dict[str, Any]) -> dict:
    """Open a folder or file in VS Code (launches window when not running)."""
    target = str(args.get("target", "")).strip()
    if not target:
        return {"success": False, "output": "Missing 'target' (folder or file path)"}
    import os
    if not (os.path.isdir(target) or os.path.isfile(target)):
        return {"success": False, "output": f"Path not found: {target}"}
    res = await _code_cli(f"code {_ps_quote(target)}", timeout=20)
    if not res.get("success"):
        return {"success": False, "output": f"code CLI failed: {res.get('stderr', '')[:180]}"}
    # Verify: the VS Code window with the basename in title appears.
    from ..computer.driver import ComputerDriver
    driver = ComputerDriver()
    window = driver.wait_for_window(re.escape(basename(target)), timeout=12.0)
    ok = bool(window.get("success"))
    return {
        "success": ok,
        "output": (f"Opened in VS Code: {target}" if ok
                   else "code CLI ran, but the window did not verify (may still be loading)"),
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
