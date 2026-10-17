"""
App Adapters — know-how bundles per application (M2)
=====================================================
Each adapter is a set of grounded ops (launch/focus/act/verify) exposed to
the brain and the registry. Ops prefer native surfaces (CLI, COM, JSX)
before falling back to keyboard atoms — reliability scales with the rung.

Adapter ops never raise; they return {success, output, ...extras}.
All COM/scripting happens in PowerShell child processes — no pywin32 dep.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional


@dataclass
class AdapterOp:
    """One grounded capability of one app."""
    app_id: str                        # "vscode", "photoshop", ...
    op_id: str                         # "open" -> tool name f"{app_id}_{op_id}"
    description: str
    examples: List[str] = field(default_factory=list)
    args: Dict[str, dict] = field(default_factory=dict)   # name -> {instructions, required, default}
    fn: Optional[Callable[..., Awaitable[dict]]] = None   # async (args) -> dict

    @property
    def tool_name(self) -> str:
        return f"{self.app_id}_{self.op_id}"
