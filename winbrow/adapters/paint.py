"""
Paint Adapter — canvas keyboard flow + verify
==============================================
mspaint is a ribbon app — the flow is: open with file, Ctrl+I inverts
colors, save-copy through the Save-As dialog. Verify hooks: window title
bears the document name; save verifies the file on disk.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict

from .base import AdapterOp

log = logging.getLogger("winbrow.adapters.paint")


async def open_image(args: Dict[str, Any]) -> dict:
    path = str(args.get("path", "")).strip()
    if not path or not os.path.isfile(path):
        return {"success": False, "output": f"Image file not found: {path}"}
    from ..windows import run_powershell
    from ..computer.driver import ComputerDriver
    q = path.replace("'", "''")
    res = await run_powershell(f"Start-Process mspaint -ArgumentList '{q}'", timeout=20)
    await asyncio.sleep(1.0)
    driver = ComputerDriver()
    window = driver.wait_for_window("Paint", timeout=10.0)
    if window.get("success"):
        driver.focus_window(window["hwnd"])
    return {
        "success": bool(window.get("success")),
        "output": (f"Paint opened with {os.path.basename(path)}" if window.get("success")
                   else "Paint did not verify (check mspaint availability)"),
        "window": window.get("window"),
    }


async def invert_colors(args: Dict[str, Any]) -> dict:
    """Ctrl+I in Invert in Paint's image area."""
    import winbrow.computer.tools as ct
    focus = ct.execute_computer_tool("focus_window", {"title": "paint"})
    if not focus.get("success"):
        return {"success": False, "output": f"Paint not focused: {focus.get('output')}"}
    driver = focus.get("_driver")  # not set; the atom reuses its own singleton
    res = ct.execute_computer_tool("computer_key", {"combo": "ctrl+i"})
    return {**res, "output": f"Inverted colors in Paint ({res.get('output', '')})"}


async def save_copy(args: Dict[str, Any]) -> dict:
    """Save-As via Paint's dialog: Ctrl+Shift+S wait → filename → Enter."""
    filename = str(args.get("filename", "")).strip()

    import winbrow.computer.tools as ct
    focus = ct.execute_computer_tool("focus_window", {"title": "paint"})
    if not focus.get("success"):
        return {"success": False, "output": f"Paint not focused: {focus.get('output')}"}
    ct.execute_computer_tool("computer_key", {"combo": "ctrl+shift+s"})
    await asyncio.sleep(1.2)
    res = ct.execute_computer_tool("file_dialog_save", {"filename": filename} if filename else {})
    ok = False
    if filename:
        full = filename if os.path.isabs(filename) else os.path.join(os.path.expanduser("~\\Pictures"), filename)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            await asyncio.sleep(0.25)
            if os.path.isfile(full):
                ok = True
                res["saved_path"] = full
                break
    return {**res, "success": bool(res.get("success", True) and (ok or not filename))}


OPS = [
    AdapterOp(
        app_id="paint", op_id="open_image",
        description="Open an image in Paint for quick edits.",
        examples=["open leoantony.png in paint", "open screenshot in mspaint"],
        args={"path": {"instructions": "Image path"}},
        fn=open_image,
    ),
    AdapterOp(
        app_id="paint", op_id="invert_colors",
        description="Invert the colors of the open Paint canvas.",
        examples=["invert colors in paint", "invert my image colors"],
        args={},
        fn=invert_colors,
    ),
    AdapterOp(
        app_id="paint", op_id="save_copy",
        description="Save the Paint canvas to a file via its Save-As dialog.",
        examples=["save painting as sketch.png", "save it to pictures"],
        args={"filename": {"instructions": "Target file name (under Pictures if relative)"}},
        fn=save_copy,
    ),
]
