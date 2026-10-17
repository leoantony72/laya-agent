"""
Photoshop Adapter — COM + JSX (via PowerShell)
==============================================
Photoshop exposes DoJavaScript (ExtendScript JSX) through its COM class
`Photoshop.Application`. We drive it from PowerShell child processes so no
pywin32 dependency is needed. JSX files are materialized by Python into
%TEMP%/winbrow/ then loaded with DoJavaScript; the JSX returns a short
status string captured from COM.

Rung: [native scripting] — dialogs that appear (save confirm etc.) are
handled by the ComputerDriver atoms on demand.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .base import AdapterOp

log = logging.getLogger("winbrow.adapters.photoshop")

_TMP = Path(tempfile.gettempdir()) / "winbrow"
_TMP.mkdir(parents=True, exist_ok=True)


def _jsx_path(tag: str) -> Path:
    return _TMP / f"ps_{tag}_{int(time.time()*1000)}.jsx"


def _temp_and_script(tag: str, jsx: str):
    """Write, DEDENT-free, the JSX to temp and return the PowerShell that runs it."""
    p = _jsx_path(tag)
    p.write_text(jsx, encoding="utf-8")
    from .vscode import _ps_quote
    script = (
        f"$jpath = {_ps_quote(str(p))}; "
        "$ps = New-Object -ComObject Photoshop.Application; "
        "$jsx = [IO.File]::ReadAllText($jpath); "
        "$out = $ps.DoJavaScript($jsx); "
        'Write-Output ("RESULT:" + $out)'
    )
    return script


def _parse(res: dict) -> dict:
    out = str(res.get("stdout", ""))
    marker = "RESULT:"
    idx = out.find(marker)
    snippet = out[idx + len(marker):].strip() if idx >= 0 else ""
    low = snippet.lower()
    ok = bool(res.get("success")) and snippet != "" and not low.startswith("err") and "nofile" not in low and "no-doc" not in low
    msg = snippet if snippet else (res.get("stderr", "") or out or "Photoshop COM did not respond")
    return {"success": ok, "output": msg[:300], "detail": snippet or out[:300]}


def _strip_leaf(path: str) -> str:
    return str(path).replace("\\", "/").replace("'", "\\'")


# --------------------------------------------------------------------------- ops

async def open_image(args: Dict[str, Any]) -> dict:
    path = str(args.get("path", "")).strip()
    if not path or not os.path.isfile(path):
        return {"success": False, "output": f"Image file not found: {path}"}
    leaf = _strip_leaf(path)
    jsx = """
        var f = new File('__PATH__');
        if (!f.exists) { 'NOFILE ' + f.fsName; }
        else { var d = app.open(f); 'OPENED ' + d.name; }
    """.replace("__PATH__", leaf)
    from ..windows import run_powershell
    res = await run_powershell(_temp_and_script("open", jsx), timeout=90)  # COM may cold-start PS
    out = _parse(res)
    if out["success"]:
        from ..computer.driver import ComputerDriver
        import re
        window = ComputerDriver().wait_for_window(
            re.escape(out["detail"].replace("OPENED ", "")[:30]), timeout=8.0)
        out["window"] = window.get("window")
    return out


async def desaturate(args: Dict[str, Any]) -> dict:
    """Desaturate the active document (full HSV → 0). Percent<100 is approximated."""
    pct = args.get("percent", 100)
    try:
        pct = max(0, min(100, int(float(str(pct) or 100))))
    except (ValueError, TypeError):
        pct = 100

    if pct == 100:
        jsx = """
            if (app.documents.length == 0) { 'NO-DOC'; }
            else { var d = app.activeDocument; d.activeLayer.desaturate(); 'DESAT ' + d.name; }
        """
    else:
        # Hue/Saturation dialog is the only partial path; keyboard fallback.
        jsx = "if (app.documents.length == 0) { 'NO-DOC'; } else { 'PARTIAL-NEEDS-DIALOG ' + app.activeDocument.name; }"
    from ..windows import run_powershell
    res = await run_powershell(_temp_and_script("desat", jsx), timeout=45)
    out = _parse(res)
    if pct != 100 and out["detail"].startswith("PARTIAL-NEEDS-DIALOG"):
        out["output"] = out["detail"] + " — partial desaturation needs the dialog; use percent=100 or run the brain flow"
        out["success"] = False
    return out


async def save_as(args: Dict[str, Any]) -> dict:
    """Save active document as a copy to path. Format inferred from extension."""
    save_path = str(args.get("path", "")).strip()
    if not save_path:
        return {"success": False, "output": "Missing 'path'"}
    leaf = _strip_leaf(save_path)
    ext = save_path.rsplit(".", 1)[-1].lower() if "." in save_path else "psd"
    # Use saveAs with format options; "true" = asCopy so the original stays open.
    jsx = f"""
        if (app.documents.length == 0) {{ 'NO-DOC'; }}
        else {{
          var d = app.activeDocument; var p = new File('{leaf}');
          var opts = null; var ext = '{ext}';
          if (ext == 'png') {{ opts = new PNGSaveOptions(); }}
          else if (ext == 'jpg' || ext == 'jpeg') {{ opts = new JPEGSaveOptions(); opts.quality = 10; }}
          else {{ opts = new PhotoshopSaveOptions(); }}
          d.saveAs(p, opts, true, Extension.LOWERCASE);
          'SAVED ' + d.name + ' -> {leaf}';
        }}
    """
    from ..windows import run_powershell
    res = await run_powershell(_temp_and_script("saveas", jsx), timeout=60)
    out = _parse(res)
    if out["success"] and os.path.isfile(save_path):
        out["saved_path"] = save_path
    elif out["success"]:
        time.sleep(1.0)
        out["success"] = os.path.isfile(save_path)
        if not out["success"]:
            out["output"] = "Photoshop reported the save, but the file is not visible yet"
    return out


OPS = [
    AdapterOp(
        app_id="photoshop", op_id="open_image",
        description="Open an image file in Adobe Photoshop (COM).",
        examples=["open wedding.jpg in photoshop", "open that png in photoshop"],
        args={"path": {"instructions": "Absolute image path"}},
        fn=open_image,
    ),
    AdapterOp(
        app_id="photoshop", op_id="desaturate",
        description="Desaturate the active Photoshop document (full graying at percent=100).",
        examples=["desaturate it in photoshop", "make it black and white in photoshop"],
        args={"percent": {"instructions": "0-100; 100 = fully desaturated", "default": "100"}},
        fn=desaturate,
    ),
    AdapterOp(
        app_id="photoshop", op_id="save_as",
        description="Save a copy of the active Photoshop document to a path.",
        examples=["save as copy_desat.jpg in photoshop", "export as png to desktop"],
        args={"path": {"instructions": "Full save path including extension (png/jpg/psd)"}},
        fn=save_as,
    ),
]
