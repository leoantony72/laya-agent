"""
Office Adapter — Word/Excel/PowerPoint COM via PowerShell
==========================================================
Open documents and Word → PDF export. All COM manipulation is done in
PowerShell child processes (no pywin32). Word.Application is the
canonical surface; Excel/PPT are natural extensions of the same pattern.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

from .base import AdapterOp
from .vscode import _ps_quote

log = logging.getLogger("winbrow.adapters.office")

_WORD_EXT = {".doc", ".docx", ".rtf", ".txt"}
_EXCEL_EXT = {".xls", ".xlsx", ".csv"}
_PPT_EXT = {".ppt", ".pptx"}


def _doc_kind(path: str) -> Optional[str]:
    ext = os.path.splitext(path)[1].lower()
    if ext in _WORD_EXT:
        return "Word"
    if ext in _EXCEL_EXT:
        return "Excel"
    if ext in _PPT_EXT:
        return "PowerPoint"
    return None


async def open_doc(args: Dict[str, Any]) -> dict:
    path = str(args.get("path", "")).strip()
    if not path or not os.path.isfile(path):
        return {"success": False, "output": f"Document not found: {path}"}
    if _doc_kind(path) is None:
        return {"success": False,
                "output": f"Unsupported document type: {path} (Word/Excel/PowerPoint only)"}
    from ..windows import run_powershell
    res = await run_powershell(f"Start-Process {_ps_quote(path)}", timeout=20)
    name = os.path.basename(path)
    return {
        "success": bool(res.get("success")),
        "output": f"Opened {name} in Office" if res.get("success") else "Office open failed",
        "kind": _doc_kind(path),
    }


async def save_as_pdf(args: Dict[str, Any]) -> dict:
    """Export the OPEN document in a COM active app, or a path, to PDF."""
    path = str(args.get("path", "")).strip()
    out_path = str(args.get("out_path", "")).strip()
    if not path or not os.path.isfile(path):
        return {"success": False, "output": f"Document not found: {path}"}
    if not out_path:
        base, _ = os.path.splitext(path)
        out_path = base + ".pdf"
    kind = _doc_kind(path)
    if kind != "Word":
        return {"success": False,
                "output": f"PDF export supported for Word docs only right now (got {kind})"}
    from ..windows import run_powershell
    script = (
        "try {"
        f" $app = New-Object -ComObject Word.Application; $app.Visible = $false;"
        f" $doc = $app.Documents.Open({_ps_quote(path)});"
        f" $doc.ExportAsFixedFormat({_ps_quote(out_path)}, 17);"
        " $doc.Close($false); $app.Quit();"
        f" Write-Output ({_ps_quote('PDF OK ' + out_path)});"
        "} catch { Write-Output ('ERR ' + $_); if ($doc) { $doc.Close($false) }; if ($app) { $app.Quit() } }"
    )
    res = await run_powershell(script, timeout=120)
    out = res.get("stdout", "")
    ok = "PDF OK" in out or os.path.isfile(out_path)
    return {
        "success": ok,
        "output": out.strip()[:300] if not ok else f"Exported PDF: {out_path}",
        "pdf": out_path if ok else None,
    }


OPS = [
    AdapterOp(
        app_id="office", op_id="open_doc",
        description="Open a Word/Excel/PowerPoint document.",
        examples=["open the report docx", "open my budget in excel"],
        args={"path": {"instructions": "Document path (.doc/.docx/.xls/.xlsx/.ppt/.pptx/.rtf/.csv/.txt)"}},
        fn=open_doc,
    ),
    AdapterOp(
        app_id="office", op_id="save_as_pdf",
        description="Export a Word document to PDF via Word COM.",
        examples=["export the report to pdf", "save doc as pdf on desktop"],
        args={
            "path": {"instructions": "Word document path"},
            "out_path": {"instructions": "Target PDF path (optional; default = same base name)"},
        },
        fn=save_as_pdf,
    ),
]
