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
import re
import time
from pathlib import Path
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
    import winbrow.computer.tools as ct
    res = await focus_or_launch_app("notepad")
    await asyncio.sleep(0.6)
    driver = ComputerDriver(guard_screen=False)
    window = driver.wait_for_window("Notepad", timeout=8.0)
    ok = bool(window.get("success"))
    if ok:
        # dismiss any stale modal (Save As) left from an interrupted flow so
        # subsequent typing lands in the editor, not the dialog
        ct.execute_computer_tool("focus_window", {"title": "notepad"})
        ct.execute_computer_tool("computer_key", {"combo": "esc"})
    return {
        "success": ok,
        "output": f"Notepad {'ready' if ok else 'did not verify'} ({res})",
        "window": window.get("window"),
    }


def _title_filename(title: str) -> str:
    """'home.txt - Notepad' → 'home.txt'. Pure helper (testable).

    Modern Notepad titles look like 'home.txt - Notepad' (saved) or
    'Untitled - Notepad' (never saved). Returns '' when not saved/derivable.
    """
    t = (title or "").strip()
    if not t:
        return ""
    for suffix in (" - Notepad", " - notepad", " - NOTEPAD"):
        if t.lower().endswith(suffix.lower()):
            t = t[: -len(suffix)].strip()
            break
    t = re.sub(r"^[*\u2022\u25cf]+\s*", "", t)  # unsaved-marker dots/stars
    if not t or t.lower().startswith("untitled"):
        return ""
    return t.split("/")[-1].split("\\")[-1][:120]


async def save_as(args: Dict[str, Any]) -> dict:
    """Save the notepad text via the Save dialog (Ctrl+S → dialog).

    Args:
        filename: optional; falls back to the Notepad window title when the
            user says just "save it in downloads" (title shows the saved name).
        folder:   optional destination hint (downloads|documents|desktop|
            pictures|videos); 'downloads' -> %USERPROFILE%\\Downloads.
    """
    import re as _re
    from ..windows import run_powershell
    filename = str(args.get("filename", "")).strip()
    folder = str(args.get("folder", "")).strip().lower()
    driver_mod = "winbrow.computer.tools"
    import winbrow.computer.tools as ct
    from ..computer.driver import ComputerDriver

    if not filename:
        # derive from the notepad window title ("home.txt - Notepad" pattern)
        try:
            driver = ComputerDriver()
            wins = driver.list_windows()
            win = next((w for w in wins
                        if "notepad" in (w.process_name or "").lower()
                        or "notepad" in (w.title or "").lower()), None)
            if win is not None:
                filename = _title_filename(win.title)
        except Exception:
            filename = ""
    if not filename:
        return {"success": False,
                "output": "No filename and the Notepad window has no saved "
                          "title yet. Say 'save it as <name>.txt' first."}

    folder_override = ""
    if folder:
        m = _re.match(r"(downloads?|documents?|desktop(ed)?|pics?|pictures?|images?|videos?)", folder)
        if m:
            folder_override = {
                "download": "Downloads", "downloads": "Downloads",
                "document": "Documents", "documents": "Documents",
                "desktop": "Desktop", "desktoped": "Desktop",
                "pic": "Pictures", "pics": "Pictures",
                "picture": "Pictures", "pictures": "Pictures",
                "image": "Pictures", "images": "Pictures",
                "video": "Videos", "videos": "Videos",
            }[m.group(1)]
    if folder_override:
        base = Path(os.environ.get("USERPROFILE", str(Path.home()))) / folder_override
        full = str(base / filename)
    elif os.path.isabs(filename):
        full = filename
    else:
        full = os.path.join(os.path.expanduser("~\\Documents"), filename)

    focus = ct.execute_computer_tool("focus_window", {"title": "notepad"})
    if not focus.get("success"):
        return {"success": False, "output": f"Notepad not focused: {focus.get('output')}"}
    # dismiss any stale dialog from a previous attempt (Esc is a no-op in
    # the editor itself)
    ct.execute_computer_tool("computer_key", {"combo": "esc"})
    await asyncio.sleep(0.4)
    ct.execute_computer_tool("computer_key", {"combo": "ctrl+s"})
    # event-driven: wait until the Save As dialog window actually exists and
    # is focused (blind sleeps raced the dialog either opening late or never)
    from ..computer.driver import ComputerDriver
    driver = ComputerDriver(guard_screen=False)
    dlg = driver.wait_for_window("Save As", timeout=6.0)
    if dlg.get("success"):
        ct.execute_computer_tool("focus_window", {"title": "Save As"})
        await asyncio.sleep(0.5)
    else:
        # no dialog → doc already has a path, ctrl+s saved silently
        await asyncio.sleep(0.5)
    res = ct.execute_computer_tool("file_dialog_save", {"filename": full})
    ok = await _wait_file(full, 6.0)
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
        description="Save the Notepad text through the Save dialog. "
                    "With no filename, uses the Notepad window title.",
        examples=["save my note as todo.txt", "save it as notes.txt",
                  "save it in downloads folder"],
        args={
            "filename": {"instructions":
                "File name (needs .txt etc). Omit to use the window title."},
            "folder": {"instructions":
                "Optional destination: downloads|documents|desktop|pictures|videos",
                "default": ""},
        },
        fn=save_as,
    ),
]
