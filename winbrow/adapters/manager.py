"""
Adapter Manager — one registry entrypoint for all apps (M2)
============================================================
Exposes every AdapterOp as a Tool (impl="adapter") so the brain and the
fast-path share one action space, and dispatches calls to the op fn.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from .base import AdapterOp
from .vscode import OPS as _vscode
from .photoshop import OPS as _photoshop
from .aftereffects import OPS as _aftereffects
from .office import OPS as _office
from .notepad import OPS as _notepad
from .paint import OPS as _paint

log = logging.getLogger("winbrow.adapters.manager")

ADAPTER_IMPL = "adapter"

ADAPTER_TOOLS: List[AdapterOp] = _vscode + _photoshop + _aftereffects + _office + _notepad + _paint

_BY_TOOL: Dict[str, AdapterOp] = {op.tool_name: op for op in ADAPTER_TOOLS}
def get_adapter_tools():
    """Registry Tool records (impl=adapter, empty script — like computer atoms)."""
    from ..registry import ArgSpec, Tool

    out = []
    for op in ADAPTER_TOOLS:
        args = [
            ArgSpec(name=name, kind="text", instructions=meta.get("instructions", ""))
            for name, meta in op.args.items()
        ]
        out.append(Tool(
            name=op.tool_name,
            description=op.description,
            script="",                      # dispatched in Python, never scripted
            scope=None,                     # launch-type ops must be globally visible
            args=args,
            examples=op.examples[:4],
            impl=ADAPTER_IMPL,
        ))
    return out


async def execute_adapter_tool(name: str, args: dict) -> dict:
    """Adapter dispatch — never raises, uniform result dicts."""
    args = args or {}
    op = _BY_TOOL.get(name)
    if op is None or op.fn is None:
        return {"success": False, "output": f"Unknown adapter op '{name}'"}
    # arg presence check (required unless default given in meta)
    for name_, meta in op.args.items():
        if str(args.get(name_, "")).strip() == "" and "default" not in meta:
            return {"success": False,
                    "output": f"Missing required arg '{name_}' for {name}"}
    try:
        return await op.fn(args)
    except Exception as e:
        log.warning(f"adapter op '{name}' failed: {e}")
        return {"success": False, "output": f"{name} failed: {e}"}


def adapter_for_app(app_id: str) -> List[AdapterOp]:
    low = app_id.lower()
    return [op for op in ADAPTER_TOOLS if op.app_id == low]


def adapters_available() -> Dict[str, bool]:
    """Cheap presence check per adapter (CLI on PATH / exe found). Best-effort."""
    import shutil
    out: Dict[str, bool] = {}
    for op in ADAPTER_TOOLS:
        app = op.app_id
        if app in out:
            continue
        if app == "vscode":
            out[app] = shutil.which("code") is not None
        elif app == "aftereffects":
            from .aftereffects import find_aerender
            out[app] = find_aerender() is not None
        elif app in ("photoshop", "office"):
            out[app] = True        # COM availability verified at call time
        else:
            out[app] = True        # system apps (notepad/paint) ship with Windows
    return out
