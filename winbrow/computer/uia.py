"""
UIA Tree — Windows UIAutomation Grounding (Rung 2 of the Ladder)
=================================================================
Focused UIA helper built on the working comtypes pattern from
accessibility.py, packaged for the brain's action loop:

  • observe(hwnd)      — compact tree of interactive elements for prompts
  • find(text, hwnd)   — elements whose name/label contain text
  • clickable rects    — every element carries a screen-coordinate bounding
                         rect so clicks need no pixel vision

Elements that expose the Invoke pattern are invoked directly; everything
else is clicked by computed rect-center (the driver handles the input).
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

log = logging.getLogger("winbrow.computer.uia")

_CONTROL_TYPE_NAMES = {
    50000: "Button", 50002: "CheckBox", 50003: "ComboBox", 50004: "Edit",
    50005: "Hyperlink", 50007: "ListItem", 50008: "List", 50009: "Menu",
    50010: "MenuBar", 50011: "MenuItem", 50012: "ProgressBar",
    50013: "RadioButton", 50014: "Tab", 50015: "TabItem", 50016: "Text",
    50017: "ToolBar", 50018: "ToolTip", 50020: "TreeItem", 50021: "Tree",
    50022: "Custom", 50023: "Group", 50024: "Thumb", 50025: "DataGrid",
    50026: "DataItem", 50027: "Document", 50028: "SplitButton", 50029: "Window",
    50030: "Pane", 50034: "TitleBar", 50035: "Separator",
}

_INTERACTIVE_TYPES = {
    "Button", "CheckBox", "ComboBox", "Edit", "Hyperlink", "ListItem",
    "MenuItem", "TabItem", "SplitButton", "RadioButton", "TreeItem",
}


@dataclass
class UIAElement:
    name: str
    control_type: str
    automation_id: str = ""
    class_name: str = ""
    rect: Tuple[int, int, int, int] = (0, 0, 0, 0)   # screen x, y, w, h
    enabled: bool = True
    invoke_supported: bool = False
    value: str = ""
    depth: int = 0

    @property
    def center(self) -> Tuple[int, int]:
        x, y, w, h = self.rect
        return (x + w // 2, y + h // 2)

    @property
    def interactable(self) -> bool:
        return self.enabled and w_pos(self.rect) and (
            self.invoke_supported or self.control_type in _INTERACTIVE_TYPES
        )

    def to_summary(self) -> str:
        label = self.name or self.value or self.automation_id or self.control_type
        iv = "⏎" if self.invoke_supported else ""
        return f"[{self.control_type}{iv}] {label[:60]} @ ({self.rect[0]},{self.rect[1]}) {self.rect[2]}x{self.rect[3]}"

    def to_dict(self) -> dict:
        return {
            "name": self.name, "control_type": self.control_type,
            "automation_id": self.automation_id, "class_name": self.class_name,
            "rect": list(self.rect), "enabled": self.enabled,
            "invoke": self.invoke_supported, "value": self.value,
        }


def w_pos(rect: Tuple[int, int, int, int]) -> bool:
    return rect[2] > 0 and rect[3] > 0


def _com_init_once() -> bool:
    """Initialize comtypes + CoInitialize on this thread once."""
    try:
        import comtypes.client
        import pythoncom
        pythoncom.CoInitialize()
        return True
    except Exception as e:
        log.info(f"UIA unavailable ({e}) — OCR/window rungs remain.")
        return False


class UIATree:
    """Reads and grounds the UIAutomation tree of a top-level window."""

    def __init__(self, max_nodes: int = 150, max_depth: int = 10):
        self.max_nodes = max_nodes
        self.max_depth = max_depth
        self._engine = None
        self._lock = threading.Lock()

    def _get_engine(self) -> Optional[Any]:
        """Create the CUIAutomation COM object (thread-safe, once)."""
        if self._engine is not None:
            return self._engine
        with self._lock:
            if self._engine is not None:
                return self._engine
            try:
                import comtypes.client
                self._engine = comtypes.client.CreateObject("UIAutomationClient.CUIAutomation")
            except Exception as e:
                log.info(f"UIA engine init failed: {e}")
                return None
        return self._engine

    # ------------------------------------------------------------------ observe
    def observe(self, hwnd: int, max_nodes: Optional[int] = None) -> List[UIAElement]:
        """Interactive element list for the given window (prompt-friendly)."""
        engines = self._get_engine()
        if not engines or not _com_init_once():
            return []
        elements: List[UIAElement] = []
        try:
            root = self._engine.ElementFromHandle(hwnd)

            def walk(el, depth):
                if len(elements) >= (max_nodes or self.max_nodes) or depth > self.max_depth:
                    return
                info = UIAElement(name="", control_type="Custom")
                try:
                    info.name = el.CurrentName or ""
                except Exception:
                    pass
                try:
                    info.control_type = _CONTROL_TYPE_NAMES.get(el.CurrentControlType, "Custom")
                except Exception:
                    pass
                try:
                    info.automation_id = el.CurrentAutomationId or ""
                except Exception:
                    pass
                try:
                    info.class_name = el.CurrentClassName or ""
                except Exception:
                    pass
                try:
                    r = el.CurrentBoundingRectangle
                    info.rect = (int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top))
                except Exception:
                    pass
                try:
                    info.enabled = bool(el.CurrentIsEnabled)
                except Exception:
                    pass
                try:
                    pattern = el.GetCurrentPattern(10000)  # UIA_InvokePatternId
                    if pattern:
                        info.invoke_supported = True
                except Exception:
                    pass
                if info.name or info.interactable or info.invoke_supported:
                    info.depth = depth
                    elements.append(info)
                try:
                    children = el.GetChildren()
                except Exception:
                    children = []
                for child in children:
                    walk(child, depth + 1)

            walk(root, 0)
        except Exception as e:
            log.debug(f"UIA observe failed: {e}")
        return elements

    # ------------------------------------------------------------------ find
    def find(self, text: str, hwnd: Optional[int] = None,
             hwnd_fallback_foreground: Optional[int] = None) -> List[UIAElement]:
        """Elements whose name/value matches text (substr, case-insensitive)."""
        if not text:
            return []
        target = text.strip().lower()
        candidates = []
        if hwnd:
            candidates = self.observe(hwnd)
        elif hwnd_fallback_foreground:
            candidates = self.observe(hwnd_fallback_foreground)
        pat = re.compile(r"\b" + re.escape(target), re.IGNORECASE) if len(target) >= 3 else None
        matches = []
        for el in candidates:
            hay = f"{el.name} {el.value}".lower()
            if target == hay.strip():
                matches.append((100, el))
            elif target in hay:
                matches.append((50, el))
            elif pat and pat.search(el.name.lower()):
                matches.append((40, el))
        matches.sort(key=lambda t: -t[0])
        return [el for _, el in matches if el.interactable]

    # ------------------------------------------------------------------ act
    @staticmethod
    def invoke(element: UIAElement) -> bool:
        """Try UIA Invoke pattern... fallback handled by caller (click center)."""
        # comtypes pattern invocation needs the raw COM pointer; we keep M1 to
        # rect-center clicking via the driver, which works universally.
        return False

    def to_context_block(self, hwnd: int, max_items: int = 40) -> str:
        """Compact element list for the brain prompt."""
        elems = self.observe(hwnd)
        interactable = [e for e in elems if e.interactable or e.name]
        lines = [e.to_summary() for e in interactable[:max_items]]
        return "\n".join(lines) if lines else "(no UIA details)"


def foreground_hwnd() -> int:
    import ctypes
    return ctypes.windll.user32.GetForegroundWindow()
