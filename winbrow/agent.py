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
from .router import CHAT, NEW_ACTION, STOP, Route, WinBrowRouter, _strip_conversational_prefix, autocorrect_utterance, _tokens, _fuzzy_in, _SEARCH_VERBS, _WEB_LOCS, _LOCAL_FIND, _LAUNCH_APPS, _COMMON_APPS
from .windows import WindowsContext, get_current_windows_context, run_powershell, set_windows_volume

log = logging.getLogger("winbrow.agent")

# Phrases that indicate browser intent
BROWSER_KEYWORDS = [
    "browser", "chrome", "edge", "firefox", "tab", "open url", "navigate to",
    "go to website", "go to https", "go to http", "incognito", "private window",
    "new tab", "close tab", "reload", "scroll down", "scroll up", "bookmark",
    "devtools", "inspect element", "javascript", "screenshot browser",
    "read page", "click on", "fill form", "find on page", "browser history",
    "ultrafast", "laya-ultrafast", "automate browser", "browser task", "multi-step browser",
    "search", "find results", "in the web", "on the web", "web search", "look up",
    # Interactive / multi-step tasks
    "book", "booking", "flight", "flights", "hotel", "buy", "purchase", "order",
    "sign in", "login", "log in", "sign up", "register", "subscribe",
    "check out", "checkout", "add to cart", "shop", "shopping",
    "fill", "submit", "download from", "open website", "go to",
    "show me", "find me", "get me", "look for", "browse to",
    "amazon", "youtube", "twitter", "instagram", "linkedin", "github",
    "google flights", "booking.com", "airbnb", "expedia",
]


# Task patterns that indicate multi-step interactive work (not just a URL open)
_INTERACTIVE_PATTERNS = [
    "book", "booking", "buy", "purchase", "order", "add to cart",
    "sign in", "login", "log in", "sign up", "register",
    "check out", "checkout", "subscribe",
    "flight", "hotel", "airbnb", "expedia",
    "fill form", "fill out", "submit form",
    "find details", "show details", "get details",
    "search on", "search for", "look for", "find on",
    "open and", "go to and", "navigate to and",
    "download from", "watch on", "play on",
]


def _is_browser_intent(utterance: str) -> bool:
    cleaned = _strip_conversational_prefix(utterance)
    lower = cleaned.lower()
    for kw in BROWSER_KEYWORDS:
        if " " in kw or "://" in kw or "-" in kw or "." in kw:
            # Multi-word phrases / URLs: substring match is safe.
            if kw in lower:
                return True
        else:
            # Single words need boundaries (allow plural): "tab" must not
            # match "tables", "reload" must not match "reloaded", etc.
            if re.search(r"\b" + re.escape(kw) + r"s?\b", lower):
                return True
    return False


def _is_interactive_task(utterance: str) -> bool:
    """Return True when the utterance needs multi-step browser interaction
    (type, click, form fill) rather than a plain URL open."""
    lower = utterance.lower()
    return any(pat in lower for pat in _INTERACTIVE_PATTERNS)


def _is_known_task(utterance: str) -> bool:
    """
    Check if utterance looks like a known task (search, open, volume, etc.)
    that should be handled by built-in tools, not the generator.
    This prevents the generator from being called for common tasks that
    the heuristic router should have caught.
    """
    cleaned = _strip_conversational_prefix(utterance)
    lower = cleaned.lower()
    tokens = _tokens(lower)
    
    # Search verbs + web locations (same logic as router)
    search_verbs = frozenset({"search", "google", "browse", "lookup", "look up", "find"})
    web_locations = frozenset({"web", "online", "internet", "google", "browser", "chrome", "edge", "firefox", "bing"})
    has_search_verb = _fuzzy_in(tokens, search_verbs) or "look up" in lower or "look for" in lower
    has_web_location = _fuzzy_in(tokens, web_locations) or "in the web" in lower or "on the web" in lower or "in web" in lower or "on web" in lower
    starts_with_search = any(lower.startswith(p) for p in ("search ", "search for ", "google ", "look up ", "look for ", "find ", "browse ", "browse for "))
    
    if (has_search_verb and has_web_location) or starts_with_search:
        return True
    
    # Open/launch/focus/switch + app
    launch_prefixes = ["open ", "launch ", "switch to ", "focus ", "bring up ", "start "]
    for prefix in launch_prefixes:
        if lower.startswith(prefix):
            return True
    
    # Volume control
    if any(w in lower for w in ["volume", "volueme", "voluem", "voume", "vol ", "vol.", "mute", "unmute", "audio", "sound"]):
        return True
    
    # Lock screen
    if "lock" in lower and any(w in lower for w in ["screen", "computer", "pc", "workstation"]):
        return True
    
    # Window actions
    if any(w in lower for w in ["minimize", "maximize", "snap left", "snap right", "show desktop", "close window"]):
        return True
    
    # Settings
    if "settings" in lower or "open settings" in lower:
        return True
    
    # Screenshot
    if "screenshot" in lower or "screen capture" in lower or "capture screen" in lower:
        return True
    
    # Time/date
    if any(p in lower for p in ["what time", "current time", "what is the date", "what date"]):
        return True
    
    # Battery
    if "battery" in lower and ("health" in lower or "status" in lower or "level" in lower or "report" in lower):
        return True
    
    # Dark/light mode
    if "dark mode" in lower or "light mode" in lower or "toggle theme" in lower or "switch theme" in lower:
        return True
    
    # Media playback
    if any(w in lower for w in ["play", "pause", "next song", "previous song", "skip track", "media"]):
        return True
    
    # Direct URL
    if re.search(r"(https?://\S+|[\w\-]+\.(com|org|net|io|dev|ai|gov|edu|co|me|app)[\S]*)", cleaned, re.IGNORECASE):
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

        # For CDP-requiring operations, ensure browser is running with CDP first
        cdp_operations = [
            "new tab", "open tab", "open new tab",
            "close tab", "close this tab",
            "reload", "refresh",
            "go back", "navigate back",
            "go forward", "navigate forward",
            "scroll", "find on page", "list tabs", "show tabs", "how many tabs",
            "read page", "page content", "what does the page say",
            "current url", "what url", "what page",
            "screenshot", "bookmark", "history", "downloads",
            "devtools", "inspect element", "click on", "fill form",
            "switch tab",
        ]
        
        needs_cdp = any(op in lower for op in cdp_operations)
        if needs_cdp:
            cdp_result = await self.browser.ensure_browser_with_cdp()
            if not cdp_result.get("success"):
                return {"type": "browser", "tool": "ensure_cdp", "success": False, 
                        "output": f"Failed to start browser with CDP: {cdp_result.get('output', 'Unknown error')}"}

        # Ultrafast multi-step browser task execution
        # Triggers for: flights, booking, shopping, form fill, interactive search, etc.
        _ULTRAFAST_TRIGGERS = [
            "ultrafast", "laya-ultrafast", "automate browser", "browser task",
            "fill form", "multi-step browser",
            # Travel / booking
            "flight", "flights", "book flight", "book a flight", "booking",
            "hotel", "book hotel", "airbnb", "expedia", "kayak",
            # Shopping
            "buy", "purchase", "order", "add to cart", "checkout", "check out",
            "shop", "shopping",
            # Auth
            "sign in", "login", "log in", "sign up", "register", "subscribe",
            # Site-targeted search ("search for X on amazon" → navigate + type + click)
            "search on", "find on", "look for", "search for",
            # Get/show details from a live site
            "find details", "show me", "get me", "find me",
            # Watch / play on a site
            "watch on", "play on", "open and",
            # Download
            "download from",
        ]
        if any(p in lower for p in _ULTRAFAST_TRIGGERS):
            out = await self.browser.run_ultrafast_task(utterance)
            return {
                "type": "browser",
                "tool": "laya_ultrafast_browser",
                "success": out.get("success", True),
                "output": out.get("final_answer") or str(out.get("steps", [])),
                "steps": out.get("steps", []),
                "total_elapsed_ms": out.get("total_elapsed_ms", 0.0),
                "method": out.get("method", "laya_ultrafast"),
            }


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
        cleaned_utterance = _strip_conversational_prefix(utterance)
        utterance_lower = cleaned_utterance.lower()
        browser_fast = _is_browser_intent(utterance)
        is_interactive = _is_interactive_task(utterance)

        # -- Handle direct search queries that go through web_search tool --
        # Keep these in the tool routing path rather than browser fast-path
        # UNLESS the search is interactive (e.g. "search for X on amazon" needs
        # navigate+type+click, not just a Google URL open).
        is_search = any(utterance_lower.startswith(p) for p in [
            "search ", "google ", "look up ", "find ", "search for ", "find results "
        ]) and not any(kw in utterance_lower for kw in [
            "tab", "reload", "scroll", "screenshot", "find on page", "incognito"
        ]) and not is_interactive  # interactive tasks should NOT be treated as simple search

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
            elif route.tool.name == "laya_ultrafast_browser":
                goal = route.args.get("goal") or utterance
                out = await self.browser.run_ultrafast_task(goal)
                result["execution"] = {
                    "type": "browser",
                    "tool": "laya_ultrafast_browser",
                    "success": out.get("success", True),
                    "output": out.get("final_answer") or str(out.get("steps", [])),
                    "steps": out.get("steps", []),
                    "total_elapsed_ms": out.get("total_elapsed_ms", 0.0),
                    "method": out.get("method", "laya_ultrafast"),
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
            # If this looks like a known task (search, open, volume, etc.) that should
            # have been caught by heuristic routing, don't call the generator.
            # The generator is for novel PowerShell tasks, not common built-in actions.
            if _is_known_task(utterance):
                result["execution"] = {
                    "type": "error",
                    "success": False,
                    "output": (
                        f"I understand you want to {utterance.lower()}, but I couldn't "
                        f"match it to a built-in tool. This might be a routing issue. "
                        f"Try rephrasing (e.g. 'search for phones on google' or 'open chrome')."
                    ),
                    "elapsed_ms": 0,
                }
                result["route"]["tier"] = "heuristic-miss"
            # Try browser dispatch first if heuristic suggests it
            elif browser_fast and not is_search:
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
