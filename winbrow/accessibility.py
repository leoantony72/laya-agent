"""
Accessibility Tree Polling - Windows UIAutomation
==================================================
Polls the Windows UIAutomation tree to get a lightweight JSON map of the screen.
Much faster and more reliable than screenshots for UI interaction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, List, Dict

log = logging.getLogger("winbrow.accessibility")


@dataclass
class UIElement:
    """Represents a UI element from the accessibility tree."""
    automation_id: str = ""
    name: str = ""
    control_type: str = ""
    class_name: str = ""
    bounding_rect: dict = field(default_factory=dict)  # {x, y, width, height}
    is_enabled: bool = True
    is_visible: bool = True
    is_offscreen: bool = False
    children: List["UIElement"] = field(default_factory=list)
    window_handle: int = 0
    process_id: int = 0
    
    def to_dict(self) -> dict:
        return {
            "automation_id": self.automation_id,
            "name": self.name,
            "control_type": self.control_type,
            "class_name": self.class_name,
            "bounding_rect": self.bounding_rect,
            "is_enabled": self.is_enabled,
            "is_visible": self.is_visible,
            "is_offscreen": self.is_offscreen,
            "children": [c.to_dict() for c in self.children],
            "window_handle": self.window_handle,
            "process_id": self.process_id,
        }


class AccessibilityTree:
    """Polls Windows UIAutomation tree for the foreground window."""
    
    def __init__(self, max_nodes: int = 200, max_depth: int = 8):
        self.max_nodes = max_nodes
        self.max_depth = max_depth
        self._uia = None
        self._initialized = False
        
    def _init_uia(self) -> bool:
        """Initialize UIAutomation COM interface."""
        try:
            import comtypes
            import comtypes.client
            from comtypes.gen import UIAutomationClient
            
            # Initialize COM
            import pythoncom
            pythoncom.CoInitialize()
            
            # Create UIAutomation instance
            self._uia = comtypes.client.CreateObject(
                "UIAutomationClient.CUIAutomation"
            )
            return True
        except ImportError:
            log.warning("comtypes not installed. Install with: pip install comtypes")
            return False
        except Exception as e:
            log.error(f"Failed to initialize UIAutomation: {e}")
            return False
    
    async def get_foreground_tree(self, max_nodes: Optional[int] = None, max_depth: Optional[int] = None) -> Optional[UIElement]:
        """Get the accessibility tree for the foreground window."""
        if not self._initialized:
            if not self._init_uia():
                return None
            self._initialized = True
        
        max_nodes = max_nodes or self.max_nodes
        max_depth = max_depth or self.max_depth
        
        try:
            import comtypes.client
            import pythoncom
            
            # Get foreground window
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            
            if not hwnd:
                return None
            
            # Get process ID
            pid = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            
            # Get UIAutomation element for the window
            element = self._uia.ElementFromHandle(hwnd)
            if not element:
                return None
            
            # Build tree
            root = self._build_tree(element, hwnd, 0, max_depth, max_nodes)
            return root
            
        except Exception as e:
            log.error(f"Failed to get accessibility tree: {e}")
            return None
    
    def _build_tree(
        self, 
        element, 
        hwnd: int, 
        depth: int, 
        max_depth: int,
        max_nodes: int,
        node_count: list = None
    ) -> Optional[UIElement]:
        """Recursively build UI element tree."""
        if node_count is None:
            node_count = [0]
        
        if node_count[0] >= max_nodes or depth > max_depth:
            return None
        
        try:
            # Get element properties
            automation_id = ""
            name = ""
            control_type = ""
            class_name = ""
            bounding_rect = {}
            is_enabled = True
            is_visible = True
            is_offscreen = False
            
            try:
                automation_id = element.CurrentAutomationId or ""
            except:
                pass
            
            try:
                name = element.CurrentName or ""
            except:
                pass
            
            try:
                control_type_id = element.CurrentControlType
                # Map control type IDs to names
                control_type_map = {
                    50000: "Button", 50001: "Calendar", 50002: "CheckBox",
                    50003: "ComboBox", 50004: "Edit", 50005: "Hyperlink",
                    50006: "Image", 50007: "ListItem", 50008: "List",
                    50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
                    50012: "ProgressBar", 50011: "RadioButton", 50012: "ScrollBar",
                    50012: "Slider", 50013: "Spinner", 50014: "StatusBar",
                    50015: "Tab", 50016: "TabItem", 50017: "Text",
                    50018: "ToolBar", 50019: "ToolTip", 50020: "Tree",
                    50021: "TreeItem", 50022: "Custom", 50023: "Group",
                    50024: "Thumb", 50025: "DataGrid", 50026: "DataItem",
                    50027: "Document", 50028: "SplitButton", 50029: "Window",
                    50030: "Pane", 50031: "Header", 50032: "HeaderItem",
                    50033: "Table", 50034: "TitleBar", 50035: "Separator",
                }
                control_type = control_type_map.get(control_type_id, f"Unknown({control_type_id})")
            except:
                pass
            
            try:
                class_name = element.CurrentClassName or ""
            except:
                pass
            
            try:
                rect = element.CurrentBoundingRectangle
                bounding_rect = {
                    "x": int(rect.left),
                    "y": int(rect.top),
                    "width": int(rect.right - rect.left),
                    "height": int(rect.bottom - rect.top),
                }
            except:
                pass
            
            try:
                is_enabled = bool(element.CurrentIsEnabled)
            except:
                pass
            
            try:
                is_offscreen = bool(element.CurrentIsOffscreen)
            except:
                pass
            
            # Create UI element
            ui_element = UIElement(
                automation_id=automation_id,
                name=name,
                control_type=control_type,
                class_name=class_name,
                bounding_rect=bounding_rect,
                is_enabled=is_enabled,
                is_visible=not is_offscreen,
                is_offscreen=is_offscreen,
                window_handle=hwnd,
            )
            
            node_count[0] += 1
            
            # Get children
            try:
                children = element.GetChildren()
                if children:
                    for child in children:
                        if node_count[0] >= 200:
                            break
                        child_element = self._build_tree(child, hwnd, depth + 1, max_depth, 200, node_count)
                        if child_element:
                            ui_element.children.append(child_element)
            except:
                pass
            
            return ui_element
            
        except Exception as e:
            log.debug(f"Error building tree node: {e}")
            return None
    
    def get_element_at_point(self, x: int, y: int) -> Optional[UIElement]:
        """Get the UI element at a specific screen coordinate."""
        if not self._initialized:
            if not self._init_uia():
                return None
        
        try:
            import comtypes.client
            element = self._uia.ElementFromPoint(comtypes.client.wintypes.POINT(x, y))
            if element:
                return self._build_tree(element, 0, 0, 2, 10)
        except Exception as e:
            log.debug(f"Failed to get element at point: {e}")
        return None
    
    def find_element_by_name(self, root: UIElement, name: str, control_type: str = "") -> List[UIElement]:
        """Find elements by name (and optionally control type) in the tree."""
        matches = []
        
        def search(element: UIElement):
            if name.lower() in element.name.lower():
                if not control_type or control_type.lower() in element.control_type.lower():
                    matches.append(element)
            for child in element.children:
                search(child)
        
        search(self)
        return matches
    
    def find_element_by_id(self, root: UIElement, automation_id: str) -> Optional[UIElement]:
        """Find element by automation ID."""
        if root.automation_id == automation_id:
            return root
        for child in root.children:
            result = self.find_element_by_id(child, automation_id)
            if result:
                return result
        return None


# Fallback for when UIAutomation is not available
class AccessibilityFallback:
    """Fallback using existing Windows context when UIAutomation unavailable."""
    
    @staticmethod
    async def get_context() -> dict:
        from .windows import get_current_windows_context
        ctx = get_current_windows_context(use_cache=False)
        return ctx.to_dict()
    
    @staticmethod
    async def get_foreground_window() -> dict:
        """Get basic foreground window info using existing Windows context."""
        from .windows import get_current_windows_context
        ctx = get_current_windows_context(use_cache=False)
        return {
            "app": ctx.active_app,
            "title": ctx.active_title,
            "hwnd": 0,
        }


async def get_accessibility_tree(
    max_nodes: int = 200, 
    max_depth: int = 8,
    use_fallback: bool = True
) -> Optional[dict]:
    """Get the accessibility tree for the foreground window.
    
    Tries UIAutomation first, falls back to basic window context.
    """
    tree = AccessibilityTree(max_nodes=max_nodes, max_depth=max_depth)
    root = await tree.get_foreground_tree(max_nodes=max_nodes, max_depth=max_depth)
    
    if root:
        return root.to_dict()
    
    if use_fallback:
        return await AccessibilityFallback.get_context()
    
    return None


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)
    
    async def test():
        tree = await get_accessibility_tree(max_nodes=50, max_depth=4)
        if tree:
            print(json.dumps(tree, indent=2)[:2000])
        else:
            print("No accessibility tree available")
    
    asyncio.run(test())