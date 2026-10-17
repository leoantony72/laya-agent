"""
After Effects Adapter — aerender + JSX (dependency-free)
=========================================================
Render comps headlessly via `aerender.exe` (installed with After Effects)
located under Program Files; project/comp/output are parameters.

Rung: [native command line]. Long renders are best run standalone; the op
streams the exit state on completion (or timeouts gracefully).
"""

from __future__ import annotations

import glob
import logging
import os
from typing import Any, Dict, List, Optional

from .base import AdapterOp
from .vscode import _ps_quote

log = logging.getLogger("winbrow.adapters.aftereffects")


def find_aerender() -> Optional[str]:
    """Locate aerender.exe in the newest Adobe After Effects installation."""
    candidates = []
    for root in (os.environ.get("ProgramFiles", "C:\\Program Files"),
                 os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")):
        candidates.extend(glob.glob(
            os.path.join(root, "Adobe", "After Effects *", "Support Files", "aerender.exe")))
    if not candidates:
        return None
    return sorted(candidates)[-1]


async def render_comp(args: Dict[str, Any]) -> dict:
    """aerender -project X -comp Y -output Z (format driven by output extension)."""
    project = str(args.get("project", "")).strip()
    comp = str(args.get("comp", "")).strip()
    output = str(args.get("output", "")).strip()
    if not project or not output:
        return {"success": False,
                "output": "Need 'project' (aep path) and 'output' (target media path); 'comp' optional"}
    if not os.path.isfile(project):
        return {"success": False, "output": f"Project file not found: {project}"}
    exe = find_aerender()
    if not exe:
        return {"success": False,
                "output": "After Effects (aerender.exe) not found under Program Files\\Adobe"}
    parts = []
    if comp:
        parts += ["-comp", _ps_quote(comp)]
    cmd = (
        "Start-Process -FilePath " + _ps_quote(exe) +
        " -ArgumentList "
        + _ps_quote(" ".join(["-project", _ps_quote(project)] + parts
                             + ["-output", _ps_quote(output)])) +
        " -Wait -NoNewWindow;$LASTEXITCODE"
    )
    from ..windows import run_powershell
    res = await run_powershell(cmd, timeout=600)
    code = res.get("returncode", 1)
    sqited = res.get("stdout", "").strip()
    ok = bool(res.get("success")) and str(code) == "0" or os.path.isfile(output)
    return {
        "success": bool(ok),
        "output": f"aerender exit {code}, output {output}" + (f" ({sqited[:80]}...)" if sqited else ""),
        "rendered": os.path.isfile(output),
    }


async def detect(args: Dict[str, Any]) -> dict:
    exe = find_aerender()
    return {
        "success": exe is not None,
        "output": f"After Effects renderer: {exe}" if exe else "aerender.exe not found",
    }


OPS = [
    AdapterOp(
        app_id="aftereffects", op_id="render_comp",
        description="Render an After Effects comp to a media file via aerender.exe.",
        examples=["render comp teaser from project to desktop as h264", "render my aep comp to mp4"],
        args={
            "project": {"instructions": ".aep project path"},
            "comp": {"instructions": "Comp name; empty = render queue as-is"},
            "output": {"instructions": "Output file path (.mp4/.mov/.avi)"},
        },
        fn=render_comp,
    ),
    AdapterOp(
        app_id="aftereffects", op_id="detect",
        description="Check if After Effects is installed (aerender available).",
        examples=["is after effects installed"],
        args={},
        fn=detect,
    ),
]
