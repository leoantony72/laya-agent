"""
Laya Typed Decision Router for WinBrow
=======================================
Equivalent to macbrow's router.py.
Uses Laya's non-autoregressive decision model to choose the exact tool and
speculative arguments in one ultra-fast request (~150-300ms).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .registry import MAX_CHOICE_OPTIONS, Tool, ToolRegistry
from .windows import WindowsContext

log = logging.getLogger("winbrow.router")

CHAT = "chat"
NEW_ACTION = "new_action"
STOP = "stop"

MIN_TOOL_CONFIDENCE = 0.35
NEW_ACTION_MIN_CONFIDENCE = 0.55


@dataclass
class Route:
    kind: str  # "tool" | "chat" | "new_action" | "stop" | "uncertain"
    tool: Optional[Tool] = None
    args: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    probabilities: dict[str, float] = field(default_factory=dict)
    latency_ms: float = 0.0
    reason: str = ""

    @property
    def summary(self) -> str:
        if self.tool:
            arg_str = ", ".join(f"{k}={v!r}" for k, v in self.args.items())
            return f"{self.tool.name}({arg_str})"
        return self.kind


class WinBrowRouter:
    """Orchestrates Laya's typed decision engine to select tools and parameters."""

    def __init__(self, registry: ToolRegistry):
        self.registry = registry
        self._router = None
        self._loaded = False
        self._load_laya()

    def _load_laya(self) -> None:
        try:
            from laya import Router
            self._router = Router()
            self._loaded = True
            log.info("Laya Decision Engine loaded successfully.")
        except Exception as e:
            log.warning(f"Laya could not be loaded: {e}. Fallback enabled.")
            self._loaded = False

    async def route(self, utterance: str, ctx: WindowsContext) -> Route:
        """
        Route user utterance to the best tool or new_action / chat.
        Completes in ~150-300ms using Laya.
        """
        t0 = time.perf_counter()
        text = utterance.strip()
        if not text:
            return Route(kind=CHAT, latency_ms=0, confidence=1.0)

        tools = self.registry.available(ctx)

        # 1. Build Laya choice criteria
        criteria: dict[str, str] = {}
        for t in tools[:MAX_CHOICE_OPTIONS - 3]:
            # Compact description with examples
            ex = f" (e.g. {', '.join(t.examples[:2])})" if t.examples else ""
            criteria[t.name] = f"{t.description}{ex}"

        criteria[CHAT] = "General conversation, greetings, asking what you can do, or questions not involving desktop action."
        criteria[NEW_ACTION] = "The user wants an action or complex Windows automation that none of the listed tools can do."
        criteria[STOP] = "Stop the assistant, cancel listening, or close WinBrow."

        # Questions dictionary for Laya
        questions: dict[str, Any] = {
            "selected_tool": {
                "type": "choice",
                "instructions": (
                    f"The user said '{text}'. The frontmost window is '{ctx.active_title}' ({ctx.active_app}). "
                    f"Which tool best fulfils the request? Prefer an app-scoped tool when focused."
                ),
                "criteria": criteria,
            }
        }

        # Speculative enum arguments for candidate tools
        for t in tools:
            for arg in t.args:
                if arg.kind == "enum" and arg.criteria:
                    questions[f"arg__{t.name}__{arg.name}"] = {
                        "type": "choice",
                        "instructions": arg.instructions,
                        "criteria": arg.criteria,
                    }

        # 2. Run Laya prediction
        if self._loaded and self._router:
            try:
                result = self._router.predict(text, questions)
                answers = result.get("answers", {})

                tool_choice = answers.get("selected_tool", {}).get("choice", NEW_ACTION)
                conf = answers.get("selected_tool", {}).get("confidence", 0.0)
                probs = answers.get("selected_tool", {}).get("probabilities", {})

                elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

                if tool_choice == CHAT:
                    return Route(kind=CHAT, confidence=conf, probabilities=probs, latency_ms=elapsed_ms)
                elif tool_choice == STOP:
                    return Route(kind=STOP, confidence=conf, probabilities=probs, latency_ms=elapsed_ms)
                elif tool_choice == NEW_ACTION or conf < MIN_TOOL_CONFIDENCE:
                    return Route(
                        kind=NEW_ACTION,
                        confidence=conf,
                        probabilities=probs,
                        latency_ms=elapsed_ms,
                        reason="No existing tool matches with high confidence",
                    )

                matched_tool = self.registry.get(tool_choice)
                if not matched_tool:
                    return Route(kind=NEW_ACTION, confidence=conf, latency_ms=elapsed_ms)

                # Collect speculative arguments
                args: dict[str, str] = {}
                for arg in matched_tool.args:
                    if arg.kind == "enum":
                        qid = f"arg__{matched_tool.name}__{arg.name}"
                        ans = answers.get(qid, {})
                        val = ans.get("choice", arg.default)
                        if val:
                            args[arg.name] = val
                    else:
                        # Free-text argument extraction
                        args[arg.name] = self._extract_text_arg(text, matched_tool.name, arg.name)

                return Route(
                    kind="tool",
                    tool=matched_tool,
                    args=args,
                    confidence=conf,
                    probabilities=probs,
                    latency_ms=elapsed_ms,
                )
            except Exception as e:
                log.error(f"Laya routing failed: {e}", exc_info=True)

        # Fallback heuristic router if Laya is warming up
        return self._heuristic_route(text, tools, t0)

    def _extract_text_arg(self, utterance: str, tool_name: str, arg_name: str) -> str:
        """Extract text parameter from user query."""
        text = utterance.strip()
        lower = text.lower()
        if tool_name == "web_search":
            for prefix in ["search google for ", "search for ", "search ", "look up ", "google ", "find ", "open "]:
                if lower.startswith(prefix):
                    return text[len(prefix):].strip()
            return text
        elif tool_name == "app_focus_or_launch":
            for prefix in ["open ", "launch ", "switch to ", "focus ", "bring up ", "start "]:
                if lower.startswith(prefix):
                    return text[len(prefix):].strip()
            return text
        return text

    def _heuristic_route(self, text: str, tools: list[Tool], t0: float) -> Route:
        lower = text.lower()
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

        # Quick volume check
        if any(w in lower for w in ["volume", "louder", "quieter", "mute", "unmute"]):
            t = self.registry.get("system_volume")
            act = "mute" if "mute" in lower else ("down" if "down" in lower or "quieter" in lower else "up")
            return Route(kind="tool", tool=t, args={"action": act}, confidence=0.9, latency_ms=elapsed_ms)

        # Quick media check
        if any(w in lower for w in ["play", "pause", "skip track", "next song", "previous song"]):
            t = self.registry.get("media_playback")
            op = "next" if "next" in lower else ("prev" if "prev" in lower else "play_pause")
            return Route(kind="tool", tool=t, args={"operation": op}, confidence=0.9, latency_ms=elapsed_ms)

        # Dark mode check
        if any(w in lower for w in ["dark mode", "light mode", "theme"]):
            t = self.registry.get("toggle_dark_mode")
            m = "dark" if "dark" in lower else ("light" if "light" in lower else "toggle")
            return Route(kind="tool", tool=t, args={"mode": m}, confidence=0.9, latency_ms=elapsed_ms)

        # Desktop organization check
        if "clean" in lower and "desktop" in lower or "organize desktop" in lower:
            t = self.registry.get("organize_desktop")
            return Route(kind="tool", tool=t, args={}, confidence=0.9, latency_ms=elapsed_ms)

        # Open / launch check
        if any(lower.startswith(p) for p in ["open ", "launch ", "start ", "switch to "]):
            t = self.registry.get("app_focus_or_launch")
            name = self._extract_text_arg(text, "app_focus_or_launch", "app_name")
            return Route(kind="tool", tool=t, args={"app_name": name}, confidence=0.85, latency_ms=elapsed_ms)

        # Web search check
        if any(lower.startswith(p) for p in ["search ", "google ", "look up "]) or "http" in lower or ".com" in lower:
            t = self.registry.get("web_search")
            q = self._extract_text_arg(text, "web_search", "query")
            return Route(kind="tool", tool=t, args={"query": q}, confidence=0.85, latency_ms=elapsed_ms)

        # Window management
        if any(w in lower for w in ["minimize", "maximize", "show desktop", "close window"]):
            t = self.registry.get("window_action")
            act = "minimize" if "minimize" in lower else ("maximize" if "maximize" in lower else ("desktop" if "desktop" in lower else "close"))
            return Route(kind="tool", tool=t, args={"action": act}, confidence=0.85, latency_ms=elapsed_ms)

        return Route(kind=NEW_ACTION, confidence=0.5, latency_ms=elapsed_ms)
