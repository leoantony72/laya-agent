from .base import AdapterOp
from .vscode import OPS as vscode_ops
from .photoshop import OPS as photoshop_ops
from .aftereffects import OPS as aftereffects_ops
from .office import OPS as office_ops
from .notepad import OPS as notepad_ops
from .paint import OPS as paint_ops
from .manager import (
    get_adapter_tools,
    execute_adapter_tool,
    ADAPTER_IMPL,
    ADAPTER_TOOLS,
    adapter_for_app,
    adapters_available,
)

__all__ = [
    "AdapterOp",
    "vscode_ops", "photoshop_ops", "aftereffects_ops",
    "office_ops", "notepad_ops", "paint_ops",
    "get_adapter_tools", "execute_adapter_tool", "ADAPTER_IMPL", "ADAPTER_TOOLS",
    "adapter_for_app", "adapters_available",
]
