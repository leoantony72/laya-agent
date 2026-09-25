"""
Laya Desktop Control Agent
==========================
An AI-powered desktop control agent that uses the Laya decision engine
for intelligent intent classification and action routing.
"""

import asyncio
import base64
import io
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import mss
import psutil
import pyautogui
from PIL import Image

# Configure pyautogui safety
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.2

logger = logging.getLogger("laya-agent")

# ---------------------------------------------------------------------------
# Laya Decision Engine Integration
# ---------------------------------------------------------------------------

class LayaDecisionEngine:
    """Wraps the Laya Router to classify user intents and route actions."""

    def __init__(self):
        self._router = None
        self._loaded = False

    def load(self):
        """Lazy-load the Laya router."""
        if self._loaded:
            return
        try:
            from laya import Router
            self._router = Router()
            self._loaded = True
            logger.info("Laya Router loaded successfully.")
        except ImportError:
            logger.warning("Laya not installed. Using fallback keyword classifier.")
            self._loaded = False
        except Exception as e:
            logger.warning(f"Laya failed to load: {e}. Using fallback.")
            self._loaded = False

    def classify_intent(self, user_input: str) -> dict:
        """
        Classify the user's intent using Laya's typed decision engine.
        Returns a dict with action category, urgency, and confidence.
        """
        text_lower = user_input.lower().strip()

        # Check for immediate high-confidence overrides for crisp direct commands
        if any(w in text_lower for w in ["screenshot", "capture screen", "screen grab", "snap screen"]):
            return {
                "action_type": "screenshot",
                "confidence": 0.99,
                "urgency": 0.9,
                "is_destructive": 0.0,
                "routing": {"reason": "Direct screenshot command"},
                "engine": "laya-hybrid"
            }

        if text_lower.startswith(("type ", "write ", "enter text ")):
            return {
                "action_type": "keyboard_type",
                "confidence": 0.99,
                "urgency": 0.8,
                "is_destructive": 0.0,
                "routing": {"reason": "Direct type command"},
                "engine": "laya-hybrid"
            }

        questions = {
            "action_type": {
                "type": "choice",
                "instructions": "What desktop control action does the user want to perform?",
                "criteria": {
                    "screenshot": "take a screenshot or capture the screen image",
                    "mouse": "click somewhere, right click, double click, move cursor, drag",
                    "keyboard_type": "type words, write text, enter characters",
                    "keyboard_shortcut": "hotkey, key shortcut like ctrl+c, alt+tab, press a key",
                    "app_launch": "open, launch, or start an application or program",
                    "app_close": "close, quit, terminate, or kill an application",
                    "system_info": "check cpu, memory, ram, battery, disk, or running processes",
                    "window_manage": "minimize, maximize, switch windows, show desktop, scroll",
                    "file_operation": "list files in a directory or open a specific file",
                    "other": "search or general query or help"
                }
            },
            "urgency": {
                "type": "score",
                "instructions": "How immediate is this action?",
                "criteria": ["can wait", "do it soon", "do it right now"]
            },
            "is_destructive": {
                "type": "noul",
                "instructions": "Could this action cause data loss or terminate important processes?"
            }
        }

        if self._loaded and self._router:
            try:
                result = self._router.predict(user_input, questions)
                choice = result["answers"]["action_type"]["choice"]
                
                # Sub-route mouse into click vs move
                if choice == "mouse":
                    if any(w in text_lower for w in ["move", "hover", "cursor to", "drag"]):
                        choice = "mouse_move"
                    else:
                        choice = "mouse_click"
                        
                # Sub-route window_manage vs scroll
                if choice == "window_manage" and any(w in text_lower for w in ["scroll", "page up", "page down"]):
                    choice = "scroll"

                return {
                    "action_type": choice,
                    "confidence": result["answers"]["action_type"].get("confidence", 0.0),
                    "urgency": result["answers"]["urgency"].get("score", 0.5),
                    "is_destructive": result["answers"]["is_destructive"].get("noul", 0.0),
                    "routing": result.get("routing", {}),
                    "engine": "laya"
                }
            except Exception as e:
                logger.error(f"Laya prediction error: {e}")
                return self._fallback_classify(user_input)
        else:
            return self._fallback_classify(user_input)

    def _fallback_classify(self, user_input: str) -> dict:
        """Simple keyword-based fallback when Laya isn't available."""
        text = user_input.lower().strip()
        mappings = [
            (["screenshot", "capture", "screen grab", "snap"], "screenshot"),
            (["click", "tap", "press button", "left click", "right click", "double click"], "mouse_click"),
            (["move mouse", "hover", "drag", "cursor to"], "mouse_move"),
            (["type", "write", "enter text", "input"], "keyboard_type"),
            (["ctrl+", "alt+", "shortcut", "hotkey", "key combo", "win+", "shift+"], "keyboard_shortcut"),
            (["open ", "launch", "start ", "run "], "app_launch"),
            (["close", "kill", "terminate", "end task", "quit"], "app_close"),
            (["scroll", "page up", "page down"], "scroll"),
            (["minimize", "maximize", "resize", "move window", "switch window", "alt tab"], "window_manage"),
            (["cpu", "memory", "ram", "disk", "battery", "system info", "status", "processes", "task manager"], "system_info"),
            (["file", "folder", "create", "delete", "rename", "copy", "move", "open file", "list file", "dir"], "file_operation"),
            (["search", "find", "look up", "locate"], "search"),
        ]
        for keywords, action in mappings:
            if any(kw in text for kw in keywords):
                return {
                    "action_type": action,
                    "confidence": 0.85,
                    "urgency": 0.5,
                    "is_destructive": 0.1,
                    "routing": {},
                    "engine": "fallback"
                }
        return {
            "action_type": "other",
            "confidence": 0.3,
            "urgency": 0.3,
            "is_destructive": 0.0,
            "routing": {},
            "engine": "fallback"
        }


# ---------------------------------------------------------------------------
# Desktop Controller
# ---------------------------------------------------------------------------

class DesktopController:
    """Handles all desktop control operations."""

    def __init__(self):
        self._sct = mss.mss()

    # --- Screen ---
    def take_screenshot(self, region: Optional[dict] = None) -> str:
        """Capture the screen and return a base64-encoded PNG."""
        if region:
            monitor = {
                "top": region.get("top", 0),
                "left": region.get("left", 0),
                "width": region.get("width", 1920),
                "height": region.get("height", 1080),
            }
        else:
            monitor = self._sct.monitors[1]  # primary monitor

        screenshot = self._sct.grab(monitor)
        img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")

        # Resize for web display (max 1280px wide)
        max_w = 1280
        if img.width > max_w:
            ratio = max_w / img.width
            img = img.resize((max_w, int(img.height * ratio)), Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return base64.b64encode(buf.getvalue()).decode("utf-8")

    def get_screen_size(self) -> dict:
        screen = self._sct.monitors[1]
        return {"width": screen["width"], "height": screen["height"]}

    # --- Mouse ---
    def mouse_click(self, x: int, y: int, button: str = "left", clicks: int = 1):
        pyautogui.click(x, y, button=button, clicks=clicks)
        return {"action": "mouse_click", "x": x, "y": y, "button": button, "clicks": clicks}

    def mouse_move(self, x: int, y: int, duration: float = 0.3):
        pyautogui.moveTo(x, y, duration=duration)
        return {"action": "mouse_move", "x": x, "y": y}

    def mouse_drag(self, start_x, start_y, end_x, end_y, duration=0.5):
        pyautogui.moveTo(start_x, start_y)
        pyautogui.drag(end_x - start_x, end_y - start_y, duration=duration)
        return {"action": "mouse_drag", "from": [start_x, start_y], "to": [end_x, end_y]}

    def get_mouse_position(self) -> dict:
        pos = pyautogui.position()
        return {"x": pos.x, "y": pos.y}

    # --- Keyboard ---
    def type_text(self, text: str, interval: float = 0.02):
        pyautogui.typewrite(text, interval=interval) if text.isascii() else pyautogui.write(text)
        return {"action": "type_text", "text": text}

    def press_key(self, key: str):
        pyautogui.press(key)
        return {"action": "press_key", "key": key}

    def hotkey(self, *keys):
        pyautogui.hotkey(*keys)
        return {"action": "hotkey", "keys": list(keys)}

    # --- Scrolling ---
    def scroll(self, amount: int, x: Optional[int] = None, y: Optional[int] = None):
        if x is not None and y is not None:
            pyautogui.scroll(amount, x, y)
        else:
            pyautogui.scroll(amount)
        return {"action": "scroll", "amount": amount}

    # --- Application management ---
    def launch_app(self, app_name: str) -> dict:
        """Launch an application by name."""
        try:
            if sys.platform == "win32":
                subprocess.Popen(f'start "" "{app_name}"', shell=True)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-a", app_name])
            else:
                subprocess.Popen([app_name])
            return {"action": "app_launch", "app": app_name, "status": "launched"}
        except Exception as e:
            return {"action": "app_launch", "app": app_name, "status": "error", "error": str(e)}

    def close_app(self, process_name: str) -> dict:
        """Close processes by name."""
        killed = []
        for proc in psutil.process_iter(["name", "pid"]):
            if process_name.lower() in proc.info["name"].lower():
                try:
                    proc.terminate()
                    killed.append({"name": proc.info["name"], "pid": proc.info["pid"]})
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        return {"action": "app_close", "process": process_name, "killed": killed}

    def list_processes(self, top_n: int = 20) -> list:
        """List top processes by CPU usage."""
        procs = []
        for proc in psutil.process_iter(["name", "pid", "cpu_percent", "memory_percent"]):
            try:
                info = proc.info
                procs.append({
                    "name": info["name"],
                    "pid": info["pid"],
                    "cpu": round(info["cpu_percent"] or 0, 1),
                    "memory": round(info["memory_percent"] or 0, 1),
                })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        procs.sort(key=lambda p: p["cpu"], reverse=True)
        return procs[:top_n]

    # --- System info ---
    def get_system_info(self) -> dict:
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        battery = psutil.sensors_battery()
        return {
            "cpu_percent": psutil.cpu_percent(interval=0.5),
            "cpu_count": psutil.cpu_count(),
            "memory": {
                "total_gb": round(mem.total / (1024**3), 1),
                "used_gb": round(mem.used / (1024**3), 1),
                "percent": mem.percent,
            },
            "disk": {
                "total_gb": round(disk.total / (1024**3), 1),
                "used_gb": round(disk.used / (1024**3), 1),
                "percent": disk.percent,
            },
            "battery": {
                "percent": battery.percent if battery else None,
                "plugged": battery.power_plugged if battery else None,
            } if battery else None,
            "uptime_hours": round((time.time() - psutil.boot_time()) / 3600, 1),
        }

    # --- File operations ---
    def list_directory(self, path: str = ".") -> list:
        p = Path(path).expanduser().resolve()
        items = []
        if p.is_dir():
            for item in sorted(p.iterdir()):
                try:
                    stat = item.stat()
                    items.append({
                        "name": item.name,
                        "type": "dir" if item.is_dir() else "file",
                        "size": stat.st_size if item.is_file() else None,
                        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    })
                except PermissionError:
                    items.append({"name": item.name, "type": "unknown", "size": None, "modified": None})
        return items

    def open_file(self, filepath: str) -> dict:
        """Open a file with the default system application."""
        try:
            if sys.platform == "win32":
                os.startfile(filepath)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", filepath])
            else:
                subprocess.Popen(["xdg-open", filepath])
            return {"action": "open_file", "file": filepath, "status": "opened"}
        except Exception as e:
            return {"action": "open_file", "file": filepath, "status": "error", "error": str(e)}

    # --- Window management ---
    def window_action(self, action: str) -> dict:
        """Perform window management via keyboard shortcuts."""
        shortcuts = {
            "minimize": (["win", "down"],),
            "maximize": (["win", "up"],),
            "close": (["alt", "F4"],),
            "switch": (["alt", "tab"],),
            "desktop": (["win", "d"],),
            "snap_left": (["win", "left"],),
            "snap_right": (["win", "right"],),
            "task_view": (["win", "tab"],),
        }
        keys = shortcuts.get(action)
        if keys:
            pyautogui.hotkey(*keys[0])
            return {"action": "window_manage", "operation": action, "status": "done"}
        return {"action": "window_manage", "operation": action, "status": "unknown_action"}

    def search_windows(self, query: str) -> dict:
        """Use Windows search."""
        pyautogui.hotkey("win")
        time.sleep(0.5)
        pyautogui.typewrite(query, interval=0.03)
        return {"action": "search", "query": query, "status": "searching"}


# ---------------------------------------------------------------------------
# Agent Orchestrator — ties Laya + Desktop together
# ---------------------------------------------------------------------------

class DesktopAgent:
    """
    The main agent that interprets commands using Laya,
    plans actions, and executes them via the DesktopController.
    """

    def __init__(self):
        self.engine = LayaDecisionEngine()
        self.controller = DesktopController()
        self.history = []

    def initialize(self):
        """Load Laya models."""
        self.engine.load()

    def execute(self, user_input: str) -> dict:
        """
        Process a user command end-to-end:
        1. Classify intent with Laya
        2. Parse parameters
        3. Execute the action
        4. Return results
        """
        start = time.time()

        # 1. Classify
        intent = self.engine.classify_intent(user_input)
        action_type = intent["action_type"]

        # 2. Execute based on classified intent
        result = self._dispatch(action_type, user_input)

        elapsed = round((time.time() - start) * 1000, 1)

        entry = {
            "timestamp": datetime.now().isoformat(),
            "input": user_input,
            "intent": intent,
            "result": result,
            "elapsed_ms": elapsed,
        }
        self.history.append(entry)

        # Keep last 100 entries
        if len(self.history) > 100:
            self.history = self.history[-100:]

        return entry

    def _dispatch(self, action_type: str, user_input: str) -> dict:
        """Route the classified action to the correct controller method."""
        text = user_input.lower()

        try:
            if action_type == "screenshot":
                b64 = self.controller.take_screenshot()
                return {"action": "screenshot", "status": "captured", "image": b64}

            elif action_type == "mouse_click":
                coords = self._extract_coords(text)
                if coords:
                    button = "right" if "right" in text else "left"
                    clicks = 2 if "double" in text else 1
                    return self.controller.mouse_click(coords[0], coords[1], button=button, clicks=clicks)
                return {"action": "mouse_click", "status": "error", "message": "Provide coordinates, e.g. 'click at 500, 300'"}

            elif action_type == "mouse_move":
                coords = self._extract_coords(text)
                if coords:
                    return self.controller.mouse_move(coords[0], coords[1])
                return {"action": "mouse_move", "status": "error", "message": "Provide coordinates, e.g. 'move mouse to 500, 300'"}

            elif action_type == "keyboard_type":
                # Extract text after common keywords
                for prefix in ["type ", "write ", "enter ", "input "]:
                    idx = text.find(prefix)
                    if idx != -1:
                        to_type = user_input[idx + len(prefix):].strip().strip('"').strip("'")
                        return self.controller.type_text(to_type)
                return {"action": "keyboard_type", "status": "error", "message": "What should I type? e.g. 'type Hello World'"}

            elif action_type == "keyboard_shortcut":
                keys = self._extract_hotkey(text)
                if keys:
                    return self.controller.hotkey(*keys)
                return {"action": "keyboard_shortcut", "status": "error", "message": "Specify a shortcut, e.g. 'press ctrl+c'"}

            elif action_type == "app_launch":
                app_name = self._extract_app_name(text, ["open", "launch", "start", "run"])
                if app_name:
                    return self.controller.launch_app(app_name)
                return {"action": "app_launch", "status": "error", "message": "Which app should I open?"}

            elif action_type == "app_close":
                app_name = self._extract_app_name(text, ["close", "kill", "terminate", "quit", "end"])
                if app_name:
                    return self.controller.close_app(app_name)
                return {"action": "app_close", "status": "error", "message": "Which app should I close?"}

            elif action_type == "scroll":
                amount = 5 if "down" in text else -5
                return self.controller.scroll(amount)

            elif action_type == "window_manage":
                for action in ["minimize", "maximize", "close", "switch", "desktop", "snap_left", "snap_right", "task_view"]:
                    if action.replace("_", " ") in text or action in text:
                        return self.controller.window_action(action)
                if "alt tab" in text or "switch" in text:
                    return self.controller.window_action("switch")
                return {"action": "window_manage", "status": "error", "message": "What window action? (minimize, maximize, close, switch, desktop)"}

            elif action_type == "system_info":
                info = self.controller.get_system_info()
                processes = self.controller.list_processes(10)
                return {"action": "system_info", "info": info, "top_processes": processes}

            elif action_type == "file_operation":
                if "list" in text or "show" in text or "files" in text:
                    path = self._extract_path(text) or "~"
                    items = self.controller.list_directory(path)
                    return {"action": "list_directory", "path": path, "items": items}
                elif "open" in text:
                    path = self._extract_path(text)
                    if path:
                        return self.controller.open_file(path)
                return {"action": "file_operation", "status": "info", "message": "Supported: list files, open file"}

            elif action_type == "search":
                query = text
                for prefix in ["search for ", "search ", "find ", "look up ", "locate "]:
                    if text.startswith(prefix):
                        query = user_input[len(prefix):].strip()
                        break
                return self.controller.search_windows(query)

            else:
                return {
                    "action": "info",
                    "message": "I can help with: screenshots, mouse/keyboard control, launching/closing apps, system info, file operations, and more. Try saying something like 'take a screenshot' or 'open notepad'.",
                    "suggestions": [
                        "Take a screenshot",
                        "Open notepad",
                        "Show system info",
                        "Click at 500, 300",
                        "Type Hello World",
                        "Press ctrl+c",
                        "Scroll down",
                        "List files in Desktop",
                    ]
                }
        except Exception as e:
            logger.error(f"Action error: {e}", exc_info=True)
            return {"action": action_type, "status": "error", "message": str(e)}

    # --- Parameter extraction helpers ---

    def _extract_coords(self, text: str) -> Optional[tuple]:
        import re
        patterns = [
            r'(\d+)\s*[,x]\s*(\d+)',
            r'at\s+(\d+)\s+(\d+)',
            r'position\s+(\d+)\s+(\d+)',
        ]
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                return (int(m.group(1)), int(m.group(2)))
        return None

    def _extract_hotkey(self, text: str) -> Optional[list]:
        import re
        # Match patterns like ctrl+c, alt+tab, win+d
        m = re.search(r'(ctrl|alt|shift|win|cmd|super)[\s+]+([\w]+(?:[\s+]+[\w]+)*)', text)
        if m:
            keys = re.split(r'[\s+]+', m.group(0))
            return [k.strip() for k in keys if k.strip()]
        return None

    def _extract_app_name(self, text: str, prefixes: list) -> Optional[str]:
        for prefix in prefixes:
            idx = text.find(prefix)
            if idx != -1:
                name = text[idx + len(prefix):].strip()
                # Remove trailing punctuation
                name = name.rstrip(".,!?;:")
                if name:
                    return name
        return None

    def _extract_path(self, text: str) -> Optional[str]:
        import re
        # Match common path patterns
        m = re.search(r'(?:in|at|from|to)\s+(["\']?)(.+?)\1(?:\s|$)', text)
        if m:
            return m.group(2)
        # Match paths with slashes or backslashes
        m = re.search(r'([A-Za-z]:\\[^\s]+|/[^\s]+|~[^\s]*)', text)
        if m:
            return m.group(1)
        return None
