"""
Tool Registry for WinBrow
=========================
Equivalent to macbrow's registry.py. Defines structured tools with
speculative typed arguments, script templates, and support for learned/persisted tools.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from .policy import validate_script_safety
from .windows import WindowsContext

log = logging.getLogger("winbrow.registry")

MAX_CHOICE_OPTIONS = 64
LEARNED_TOOLS_PATH = pathlib.Path(__file__).parent.parent / "tools" / "learned.json"


@dataclass
class ArgSpec:
    name: str
    kind: Literal["enum", "text"]
    instructions: str
    criteria: dict[str, str] = field(default_factory=dict)
    default: Optional[str] = None


@dataclass
class Tool:
    name: str
    description: str
    script: str  # PowerShell or Python template with {{name}} placeholders
    scope: Optional[str] = None  # Specific application (e.g. "Google Chrome") or None
    args: list[ArgSpec] = field(default_factory=list)
    speak: str = "done"  # "done" | "result" | template
    examples: list[str] = field(default_factory=list)
    is_learned: bool = False

    def choice_description(self) -> dict[str, Any]:
        """Description passed to Laya router for tool choice."""
        desc: dict[str, Any] = {
            "what": self.description,
            "examples": self.examples[:4],
        }
        if self.scope:
            desc["scope"] = f"Only when {self.scope} is relevant or focused."
        return desc

    def render_script(self, args: dict[str, str]) -> str:
        """Render script template by substituting {{arg}} with safely escaped values."""
        rendered = self.script
        for arg in self.args:
            val = args.get(arg.name, arg.default or "")
            # Escape quotes in replacement values
            safe_val = str(val).replace('"', '`"').replace("'", "''")
            rendered = rendered.replace(f"{{{{{arg.name}}}}}", safe_val)
        return rendered


class ToolRegistry:
    def __init__(self, learned_path: Optional[pathlib.Path] = None):
        self._tools: dict[str, Tool] = {}
        self.learned_path = learned_path or LEARNED_TOOLS_PATH
        self._register_builtins()
        self.load_learned()

    def register(self, tool: Tool) -> None:
        validate_script_safety(tool.script)
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def available(self, ctx: WindowsContext) -> list[Tool]:
        """Return tools available in the current Windows context."""
        tools = []
        for t in self._tools.values():
            if t.scope is None:
                tools.append(t)
            elif ctx.active_app.lower() in t.scope.lower():
                tools.append(t)
        return tools

    def all_tools(self) -> list[Tool]:
        return list(self._tools.values())

    # ------------------------------------------------------------------ Built-ins
    def _register_builtins(self) -> None:
        """Load the built-in tools from tools/seed.json (validated)."""
        self._load_seed_tools()

    def _load_seed_tools(self) -> None:
        """Load tools/seed.json through policy validation into the registry.

        Every tool passes through register() → validate_script_safety(),
        so a tampered seed file cannot sneak in a destructive script.
        """
        seed_path = pathlib.Path(__file__).parent.parent / "tools" / "seed.json"
        try:
            with open(seed_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            log.warning(f"Could not load tool seed {seed_path}: {e}")
            return
        loaded = 0
        for item in data:
            try:
                args = [
                    ArgSpec(
                        name=a["name"],
                        kind=a.get("kind", "text"),
                        instructions=a.get("instructions", ""),
                        criteria=a.get("criteria", {}),
                        default=a.get("default"),
                    )
                    for a in item.get("args", [])
                ]
                self.register(Tool(
                    name=item["name"],
                    description=item["description"],
                    script=item["script"],
                    scope=item.get("scope"),
                    args=args,
                    speak=item.get("speak", "done"),
                    examples=item.get("examples", [])[:3],
                    is_learned=False,
                ))
                loaded += 1
            except Exception as e:
                log.warning(f"Skipping seed tool {item.get('name')!r}: {e}")
        log.info(f"Loaded {loaded} seed tools from {seed_path}")

    # ------------------------------------------------------------------ Persistence
    def load_learned(self) -> None:
        """Load dynamically generated/learned tools from tools/learned.json."""
        if not self.learned_path.exists():
            return
        try:
            with open(self.learned_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                # Never let a stale learned entry shadow a built-in tool of
                # the same name (learned entries register by dict overwrite).
                if item.get("name") in self._tools and not self._tools[item["name"]].is_learned:
                    log.warning(f"Ignoring learned tool '{item['name']}' — a built-in tool with that name exists.")
                    continue
                # Skip persisted no-op placeholders from older generator runs.
                if "Executed custom task:" in item.get("script", ""):
                    log.warning(f"Ignoring learned tool '{item.get('name')}' — it is a no-op placeholder.")
                    continue
                args = [
                    ArgSpec(
                        name=a["name"],
                        kind=a.get("kind", "text"),
                        instructions=a.get("instructions", ""),
                        criteria=a.get("criteria", {}),
                        default=a.get("default"),
                    )
                    for a in item.get("args", [])
                ]
                tool = Tool(
                    name=item["name"],
                    description=item["description"],
                    script=item["script"],
                    scope=item.get("scope"),
                    args=args,
                    speak=item.get("speak", "done"),
                    examples=item.get("examples", []),
                    is_learned=True,
                )
                self.register(tool)
            log.info(f"Loaded {len(data)} learned tools from {self.learned_path}")
        except Exception as e:
            log.warning(f"Failed to load learned tools: {e}")

    def save_learned(self, tool: Tool) -> None:
        """Save a new dynamically generated tool to tools/learned.json for instant reuse."""
        if tool.name in self._tools and not self._tools[tool.name].is_learned:
            # Never overwrite a built-in tool with a generated one of the same name.
            tool.name = f"{tool.name}_custom"
        tool.is_learned = True
        self.register(tool)
        self.learned_path.parent.mkdir(parents=True, exist_ok=True)

        existing = []
        if self.learned_path.exists():
            try:
                with open(self.learned_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
            except Exception:
                existing = []

        # Filter out existing with same name
        existing = [item for item in existing if item["name"] != tool.name]

        tool_dict = {
            "name": tool.name,
            "description": tool.description,
            "script": tool.script,
            "scope": tool.scope,
            "speak": tool.speak,
            "examples": tool.examples,
            "args": [
                {
                    "name": a.name,
                    "kind": a.kind,
                    "instructions": a.instructions,
                    "criteria": a.criteria,
                    "default": a.default,
                }
                for a in tool.args
            ],
        }
        existing.append(tool_dict)

        with open(self.learned_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        log.info(f"Persisted learned tool '{tool.name}' to {self.learned_path}")
