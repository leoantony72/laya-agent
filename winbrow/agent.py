"""
WinBrow Master Agent
====================
The complete Windows desktop and browser control agent powered by Laya.
Architecture:
  User Utterance + Live Windows Context (active window, running apps)
           │
           ▼
    Laya Choice Router (~200ms)
     ├- Known Tool → Render PowerShell template → Execute → Output
     ├- Browser Task → Drive Chrome/Edge via CDP or WScript
     ├- Complex/New Action → Generator Tier (LLM writes PowerShell) → Policy Check → Execute → Persist in tools/learned.json
     └- Chat/Small Talk → Terse Spoken Response
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Optional

from .browser_task import BrowserController
from .generator import ScriptGenerator
from .last_opened import ContextMemory
from .registry import Tool, ToolRegistry
from .resolvers import resolve_file, resolve_folder
from .router import CHAT, NEW_ACTION, STOP, Route, WinBrowRouter
from .windows import WindowsContext, get_current_windows_context, run_powershell, set_windows_volume

log = logging.getLogger("winbrow.agent")

# Phrases that indicate browser intent
BROWSER_KEYWORDS = [
    "browser", "chrome", "edge", "firefox", "tab", "open url", "navigate to",
    "go to website", "go to https", "go to http", "incognito", "private window",
    "new tab", "close tab", "reload", "scroll down", "scroll up", "bookmark",
    "devtools", "inspect element", "javascript", "screenshot browser",
    "read page", "click on", "fill form", "find on page", "browser history",
]


def _is_browser_intent(utterance: str) -> bool:
    lower = utterance.lower()
    for kw in BROWSER_KEYWORDS:
        if " " in kw or "://" in kw:
            # Multi-word phrases / URLs: substring match is safe.
            if kw in lower:
                return True
        else:
            # Single words need boundaries (allow plural): "tab" must not
            # match "tables", "reload" must not match "reloaded", etc.
            if re.search(r"\b" + re.escape(kw) + r"s?\b", lower):
                return True
    return False


class WinBrowAgent:
    """The central orchestrator for Windows and Browser control."""

    def __init__(self):
        self.registry = ToolRegistry()
        self.router = WinBrowRouter(self.registry)
        self.generator = ScriptGenerator(self.registry)
        self.browser = BrowserController()
        self.memory = ContextMemory()
        self.history: list[dict[str, Any]] = []

    def get_context(self) -> WindowsContext:
        """Capture live Windows environment context."""
        return get_current_windows_context()

    def _record_open_result(self, tool_name: str | None, args: dict[str, str], success: bool) -> None:
        """Record successful opens into context memory (for "that file" follow-ups).

        Re-resolves the argument to a real path so only verified locations
        are remembered. Never raises; never affects execution.
        """
        try:
            if not success or not tool_name:
                return
            if tool_name == "open_file":
                target = (args or {}).get("target", "")
                if target:
                    path = resolve_file(target)
                    if path:
                        self.memory.record_open("file", path, target)
            elif tool_name == "open_folder":
                folder = (args or {}).get("folder", "")
                if folder:
                    path = resolve_folder(folder)
                    if path:
                        self.memory.record_open("folder", path, folder)
        except Exception:
            pass

    async def _dispatch_browser(self, utterance: str) -> dict[str, Any]:
        """
        Dispatch browser-oriented commands.
        Returns an execution result dict.
        """
        lower = utterance.lower()

        # Open new tab
        if any(p in lower for p in ["new tab", "open tab", "open new tab"]):
            out = await self.browser.new_tab()
            return {"type": "browser", "tool": "new_tab", "success": out.get("success", True), "output": out.get("stdout", "Opened new tab")}

        # Close tab
        if any(p in lower for p in ["close tab", "close this tab"]):
            out = await self.browser.close_current_tab()
            return {"type": "browser", "tool": "close_tab", "success": True, "output": out.get("stdout", "Closed tab")}

        # Incognito
        if "incognito" in lower or "private window" in lower or "private tab" in lower:
            import re
            url_match = re.search(r"https?://\S+", utterance)
            out = await self.browser.open_incognito(url_match.group(0) if url_match else "")
            return {"type": "browser", "tool": "incognito", "success": True, "output": out.get("stdout", "Incognito opened")}

        # Reload
        if "reload" in lower or "refresh" in lower:
            hard = "hard" in lower or "force" in lower
            out = await self.browser.reload_page(hard=hard)
            return {"type": "browser", "tool": "reload", "success": True, "output": out.get("stdout", "Page reloaded")}

        # Navigate back
        if "go back" in lower or "navigate back" in lower:
            out = await self.browser.go_back()
            return {"type": "browser", "tool": "go_back", "success": True, "output": "Navigated back"}

        # Navigate forward
        if "go forward" in lower or "navigate forward" in lower:
            out = await self.browser.go_forward()
            return {"type": "browser", "tool": "go_forward", "success": True, "output": "Navigated forward"}

        # Screenshot
        if "screenshot" in lower and ("browser" in lower or "tab" in lower or "page" in lower):
            out = await self.browser.screenshot()
            return {"type": "browser", "tool": "screenshot", "success": out.get("success", False),
                    "output": out.get("stdout") or out.get("path") or str(out)}

        # Scroll
        if "scroll" in lower:
            direction = "up" if "up" in lower else "down"
            import re
            num_match = re.search(r"\d+", utterance)
            amount = int(num_match.group()) if num_match else 3
            out = await self.browser.scroll_page(direction, amount)
            return {"type": "browser", "tool": "scroll", "success": True, "output": out.get("stdout", f"Scrolled {direction}")}

        # Find on page
        if "find" in lower and "page" in lower:
            query = utterance.lower().replace("find on page", "").replace("find", "").strip()
            out = await self.browser.find_on_page(query)
            return {"type": "browser", "tool": "find_on_page", "success": True, "output": out.get("stdout", "Searching page")}

        # List tabs
        if "list tabs" in lower or "show tabs" in lower or "how many tabs" in lower:
            out = await self.browser.list_tabs()
            if out.get("cdp_available") and out.get("tabs"):
                tab_list = "\n".join(f"  [{i+1}] {t['title'][:60]}  -  {t['url'][:60]}" for i, t in enumerate(out["tabs"]))
                return {"type": "browser", "tool": "list_tabs", "success": True,
                        "output": f"Open tabs ({out['tab_count']}):\n{tab_list}"}
            return {"type": "browser", "tool": "list_tabs", "success": True, "output": str(out.get("tabs", "Could not list tabs"))}

        # Read page text
        if "read page" in lower or "what does the page say" in lower or "page content" in lower:
            out = await self.browser.read_page_text()
            return {"type": "browser", "tool": "read_page", "success": out.get("success", False),
                    "output": out.get("text") or out.get("error", "Could not read page")}

        # Get current URL
        if "current url" in lower or "what url" in lower or "what page" in lower:
            out = await self.browser.get_current_url()
            return {"type": "browser", "tool": "get_url", "success": True,
                    "output": f"URL: {out.get('url', 'Unknown')}\nTitle: {out.get('title', '')}"}

        # Launch with CDP
        if "launch with cdp" in lower or "enable cdp" in lower or "remote debugging" in lower:
            out = await self.browser.launch_with_cdp()
            return {"type": "browser", "tool": "launch_cdp", "success": out.get("success", True), "output": out.get("stdout", str(out))}

        # Open URL / Navigate
        import re
        url_match = re.search(r"(https?://\S+|[\w\-]+\.(com|org|net|io|dev|ai|gov|edu|co|me|app)[\S]*)", utterance, re.IGNORECASE)
        if url_match:
            url = url_match.group(0)
            new = "new tab" in lower
            out = await self.browser.navigate_to(url, new_tab=new)
            return {"type": "browser", "tool": "navigate", "success": True, "output": out.get("stdout", f"Opened {url}")}

        # Browser shortcut fallback
        for action in ["bookmark", "history", "downloads", "fullscreen", "devtools", "print"]:
            if action in lower:
                out = await self.browser.browser_shortcut(action)
                return {"type": "browser", "tool": action, "success": True, "output": out.get("stdout", action)}

        # Switch tab by number
        num_match = re.search(r"tab\s*(\d+)|(\d+)\s*(?:st|nd|rd|th)?\s*tab", lower)
        if num_match:
            num = int(num_match.group(1) or num_match.group(2))
            out = await self.browser.switch_tab_by_number(num)
            return {"type": "browser", "tool": "switch_tab", "success": True, "output": out.get("stdout", f"Switched to tab {num}")}

        # Default: ensure browser is open and focused
        out = await self.browser.ensure_browser_open()
        return {"type": "browser", "tool": "focus_browser", "success": True, "output": out.get("stdout", "Browser focused")}

    async def execute(
        self,
        utterance: str,
        api_key: Optional[str] = None,
        provider: str = "auto",
        custom_endpoint: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Process user command end-to-end:
        1. Capture live Windows context
        2. Check for browser intent fast-path
        3. Route with Laya decision engine (~200ms)
        4. Execute or trigger generator tier
        5. Return structured trace
        """
        t0 = time.perf_counter()
        # Context capture does process enumeration + COM reads (15-600ms);
        # keep it off the event loop so routing/execution stay responsive.
        loop = asyncio.get_event_loop()
        ctx = await loop.run_in_executor(None, self.get_context)

        # Fast-path browser intent detection (before Laya routing overhead)
        utterance_lower = utterance.lower()
        browser_fast = _is_browser_intent(utterance)

        # -- Handle direct search queries that go through web_search tool --
        # Keep these in the tool routing path rather than browser fast-path
        is_search = any(utterance_lower.startswith(p) for p in [
            "search ", "google ", "look up ", "find ", "search for "
        ]) and not any(kw in utterance_lower for kw in [
            "tab", "reload", "scroll", "screenshot", "find on page", "incognito"
        ])

        # 1. Laya Routing (context memory lets "that file" resolve)
        route: Route = await self.router.route(utterance, ctx, memory=self.memory)
        log.info(f"Routed '{utterance}' → {route.kind} (tool: {route.tool.name if route.tool else None}, conf: {route.confidence:.2f})")

        result: dict[str, Any] = {
            "utterance": utterance,
            "route": {
                "kind": route.kind,
                "tool_name": route.tool.name if route.tool else None,
                "args": route.args,
                "confidence": round(route.confidence, 3),
                "latency_ms": route.latency_ms,
                "probabilities": route.probabilities,
                "tier": route.tier or route.kind,
            },
            "context": ctx.to_dict(),
            "execution": {},
        }

        # 2. Dispatch
        if route.kind == "tool" and route.tool:
            if route.tool.name == "volume_set_level":
                level_arg = route.args.get("level", "50")
                out_msg = set_windows_volume(level_arg)
                result["execution"] = {
                    "type": "tool_execution",
                    "tool": route.tool.name,
                    "is_learned": False,
                    "script": f"set_windows_volume({level_arg})",
                    "success": "set to" in out_msg.lower(),
                    "output": out_msg,
                    "elapsed_ms": 1.0,
                }
            else:
                # Render script with arguments
                rendered = route.tool.render_script(route.args)
                exec_out = await run_powershell(rendered)
                result["execution"] = {
                    "type": "tool_execution",
                    "tool": route.tool.name,
                    "is_learned": route.tool.is_learned,
                    "script": rendered,
                    "success": exec_out.get("success", False),
                    "output": exec_out.get("stdout") or exec_out.get("stderr") or "Done",
                    "elapsed_ms": exec_out.get("elapsed_ms", 0),
                }

        elif browser_fast and not is_search and route.kind != "tool":
            # Browser fast-path
            browser_out = await self._dispatch_browser(utterance)
            result["execution"] = browser_out
            result["route"]["kind"] = "browser"

        elif route.kind == NEW_ACTION:
            # Try browser dispatch first if heuristic suggests it
            if browser_fast and not is_search:
                browser_out = await self._dispatch_browser(utterance)
                result["execution"] = browser_out
                result["route"]["kind"] = "browser"
            else:
                # Trigger dynamic script generation tier
                gen_out = await self.generator.generate_and_execute(
                    utterance,
                    ctx,
                    api_key=api_key,
                    provider=provider,
                    custom_endpoint=custom_endpoint,
                    project_id=project_id,
                )
                result["execution"] = {
                    "type": "generated_tool",
                    "tool": gen_out.get("tool").name if gen_out.get("tool") else "custom_action",
                    "is_learned": True,
                    "script": gen_out.get("rendered_script", ""),
                    "success": gen_out.get("success", False),
                    "output": gen_out.get("stdout") or gen_out.get("stderr") or gen_out.get("reason", "Executed"),
                    "elapsed_ms": gen_out.get("elapsed_ms", 0),
                }

        elif route.kind == CHAT:
            result["execution"] = {
                "type": "chat",
                "output": (
                    "I'm WinBrow, your desktop and browser control agent. "
                    "I can control Windows (volume, dark mode, app launch, window management, settings, "
                    "desktop cleanup, clipboard), drive Chrome/Edge (tab management, navigation, JS execution, "
                    "screenshots, page reading), and generate new PowerShell tools on the fly for anything else. "
                    "Just say what you need!"
                ),
                "success": True,
            }

        elif route.kind == STOP:
            result["execution"] = {
                "type": "stop",
                "output": "Standing by. Listening paused.",
                "success": True,
            }

        total_ms = round((time.perf_counter() - t0) * 1000, 1)
        result["total_elapsed_ms"] = total_ms

        # Keep history (ring buffer)
        self.history.append(result)
        if len(self.history) > 100:
            self.history = self.history[-100:]

        # Remember successful opens for follow-up references.
        if route.kind == "tool" and route.tool:
            self._record_open_result(
                route.tool.name, route.args,
                result.get("execution", {}).get("success"),
            )

        # M0 instrumentation: persistent JSONL trace (never breaks execution)
        try:
            from .trace import command_event
            command_event(
                utterance=utterance,
                tier=route.tier or route.kind,
                tool_name=route.tool.name if route.tool else None,
                confidence=route.confidence,
                route_latency_ms=route.latency_ms,
                success=result.get("execution", {}).get("success"),
                total_elapsed_ms=total_ms,
            )
        except Exception:
            pass

        return result
