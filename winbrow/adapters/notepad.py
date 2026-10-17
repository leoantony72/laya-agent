"""
Notepad Adapter — launch + save flow composed from ComputerDriver atoms
=======================================================================
Rung: [keyboard + UIA]. Notepad is the reference app for the M1 atoms;
this adapter packages the compound flows as one-call tools.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict

from .base import AdapterOp

log = logging.getLogger("winbrow.adapters.notepad")


async def _wait_file(path: str, timeout: float = 6.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(0.25)
        if os.path.isfile(path):
            return True
    return False


async def open_notepad(args: Dict[str, Any]) -> dict:
    from ..windows import focus_or_launch_app, run_powershell
    from ..computer.driver import ComputerDriver
    res = await focus_or_launch_app("notepad")
    await asyncio.sleep(0.6)
    driver = ComputerDriver()
    window = driver.wait_for_window("Notepad", timeout=8.0)
    ok = bool(window.get("success"))
    return {
        "success": ok,
        "output": f"Notepad {'ready' if ok else 'did not verify'} ({res})",
        "window": window.get("window"),
    }


async def save_as(args: Dict[str, Any]) -> dict:
    """Save the notepad text to a filename via the Save dialog (Ctrl+S → dialog)."""
    filename = str(args.get("filename", "")).strip()
    if not filename:
        return {"success": False, "output": "Missing 'filename'"}
    driver_mod = "winbrow.computer.tools"
    import winbrow.computer.tools as ct
    full = filename if os.path.isabs(filename) else os.path.join(os.path.expanduser("~\\Documents"), filename)
    focus = ct.execute_computer_tool("focus_window", {"title": "notepad"})
    if not focus.get("success"):
        return {"success": False, "output": f"Notepad not focused: {focus.get('output')}"}
    ct.execute_computer_tool("computer_key", {"combo": "ctrl+s"})
    await asyncio.sleep(1.2)  # dialog appears for unsaved docs
    res = ct.execute_computer_tool("file_dialog_save", {"filename": filename})
    ok = await _wait_file(full, 5.0)
    return {**res, "success": bool(ok and res.get("success", True)),
            "saved_path": full if ok else None,
            "output": res.get("output") if ok else f"{res.get('output')} (file not found: {full})"}


OPS = [
    AdapterOp(
        app_id="notepad", op_id="open",
        description="Open Notepad ready for typing.",
        examples=["open notepad", "start a new note"],
        args={},
        fn=open_notepad,
    ),
    AdapterOp(
        app_id="notepad", op_id="save_as",
        description="Save the Notepad text to a file through the Save dialog.",
        examples=["save my note as todo.txt", "save it as notes.txt"],
        args={"filename": {"instructions": "File name (e.g. notes.txt); saved under Documents if relative"}},
        fn=save_as,
    ),
]
