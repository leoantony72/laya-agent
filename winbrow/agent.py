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
import json
import logging
import os
import re
import time
from typing import Any, Optional

from .browser_task import BrowserController, note_browser_from_text
from .browser_tools import BROWSER_IMPL, execute_browser_tool, get_browser_tools
from .brain import AgentBrain, GatewayUnavailable, ModelGateway
from .brain.agent_loop import (
    is_explicit_subagent_request,
    is_subagent_candidate,
    subagents_enabled,
    subagents_explicit_only,
)
from .brain.agent_loop import COMPUTER_SKILL_DISPATCH, _ctx_block
from .brain.model_gateway import extract_json_object
from .brain.prompts import build_chat_messages
from .memory.skills import (
    MissingSkillParameters,
    parameterize_steps,
    suggest_skill_name,
)
from .computer import COMPUTER_IMPL, get_computer_tools
from .computer.tools import execute_computer_tool
from .adapters import ADAPTER_IMPL, get_adapter_tools
from .adapters.manager import execute_adapter_tool
from .installer import INSTALLER_IMPL, INSTALL_OPS, get_installer_tools
from .mcp import MCP_IMPL, get_mcp_tools, execute_mcp_tool, get_mcp_hub
from .shell import SHELL_IMPL, execute_shell_tool, get_shell_tools
from .document_tools import DOCUMENT_IMPL, execute_document_tool, get_document_tools
from .skill_scripts import (
    SKILL_SCRIPTS_IMPL,
    execute_skill_script_tool_async,
    get_skill_script_tools,
)
from .integrations import (
    INTEGRATION_IMPL,
    execute_integration_tool,
    get_integration_tools,
)
from .subagents import (
    SUBAGENT_IMPL,
    SubagentManager,
    get_subagent_tools,
)
from .generator import ScriptGenerator
from .last_opened import ContextMemory
from .memory import MemoryStore, SessionMemory, get_session, get_store
from .registry import Tool, ToolRegistry
from .resolvers import resolve_file, resolve_folder
from .router import CHAT, NEW_ACTION, STOP, Route, WinBrowRouter, _strip_conversational_prefix, autocorrect_utterance, _tokens, _fuzzy_in, _SEARCH_VERBS, _WEB_LOCS, _LOCAL_FIND, _LAUNCH_APPS, _COMMON_APPS, is_multi_step_intent, _is_media_control, _play_browser_goal
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
    "google flights", "booking.com", "airbnb", "expedia", "instagram", "google drive", "facebook",
    "flipkart", "discord", "twitter",
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
    # Multi-step composite intents belong to the brain planner, never browser fast-path
    if is_multi_step_intent(utterance):
        return False
    # M4 scenario-suite finding: loose browser keywords ("show me", "chrome")
    # swallow pure system intents. Those are tool routes, never the browser.
    if _SYSTEM_INTENT_RE.search(lower):
        return False
    if _APP_LAUNCH_RE.search(lower):
        return False
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


# System-status / control intents that loose browser keywords must not capture.
_SYSTEM_INTENT_RE = re.compile(
    r"\b(network status|internet connection|internet status|check internet|"
    r"my ip|ip address|what time|current time|whats the time|whats the date|"
    r"current date|what date|battery|volume|volueme|voluem|mute|unmute|"
    r"louder|quieter|screenshot|screen capture|clipboard|dark mode|"
    r"light mode|toggle theme|switch theme)\b"
)

# Explicit app-launch intents: "open chrome", "open brave", "open telegram", etc.
# These open the designated application (app_focus_or_launch), not the Laya browser.
_APP_LAUNCH_RE = re.compile(
    r"\b(open|launch|start|run)\s+(?:the\s+)?(chrome|google chrome|edge|"
    r"microsoft edge|firefox|brave|brave browser|telegram|telegram desktop|"
    r"whatsapp|spotify|discord|slack|code|vscode|vs code|notepad|calculator|calc)\b"
)


def _is_interactive_task(utterance: str) -> bool:
    """Return True when the utterance needs multi-step browser interaction
    (type, click, form fill) rather than a plain URL open."""
    lower = utterance.lower()
    return any(pat in lower for pat in _INTERACTIVE_PATTERNS)


# City/country → IANA timezone for MCP time shortcuts ("what time is it in X").
_TZ_ALIASES = {
    # India + neighbours
    "delhi": "Asia/Kolkata", "newdelhi": "Asia/Kolkata", "mumbai": "Asia/Kolkata",
    "bombay": "Asia/Kolkata", "bangalore": "Asia/Kolkata", "bengaluru": "Asia/Kolkata",
    "kolkata": "Asia/Kolkata", "calcutta": "Asia/Kolkata", "chennai": "Asia/Kolkata",
    "hyderabad": "Asia/Kolkata", "pune": "Asia/Kolkata", "india": "Asia/Kolkata",
    "karachi": "Asia/Karachi", "dhaka": "Asia/Dhaka", "colombo": "Asia/Colombo",
    "kathmandu": "Asia/Kathmandu",
    # East/SE Asia
    "beijing": "Asia/Shanghai", "shanghai": "Asia/Shanghai", "china": "Asia/Shanghai",
    "hongkong": "Asia/Hong_Kong", "seoul": "Asia/Seoul", "korea": "Asia/Seoul",
    "singapore": "Asia/Singapore", "jakarta": "Asia/Jakarta", "bangkok": "Asia/Bangkok",
    "kualalumpur": "Asia/Kuala_Lumpur", "manila": "Asia/Manila", "hanoi": "Asia/Ho_Chi_Minh",
    "taipei": "Asia/Taipei", "japan": "Asia/Tokyo",
    # Middle East / Africa
    "dubai": "Asia/Dubai", "riyadh": "Asia/Riyadh", "doha": "Asia/Qatar",
    "tehran": "Asia/Tehran", "jerusalem": "Asia/Jerusalem", "istanbul": "Europe/Istanbul",
    "cairo": "Africa/Cairo", "lagos": "Africa/Lagos", "nairobi": "Africa/Nairobi",
    "johannesburg": "Africa/Johannesburg", "capetown": "Africa/Johannesburg",
    # Europe
    "london": "Europe/London", "uk": "Europe/London", "england": "Europe/London",
    "paris": "Europe/Paris", "france": "Europe/Paris", "berlin": "Europe/Berlin",
    "germany": "Europe/Berlin", "rome": "Europe/Rome", "milan": "Europe/Rome",
    "madrid": "Europe/Madrid", "barcelona": "Europe/Madrid", "lisbon": "Europe/Lisbon",
    "amsterdam": "Europe/Amsterdam", "brussels": "Europe/Brussels", "zurich": "Europe/Zurich",
    "geneva": "Europe/Zurich", "vienna": "Europe/Vienna", "prague": "Europe/Prague",
    "warsaw": "Europe/Warsaw", "stockholm": "Europe/Stockholm", "oslo": "Europe/Oslo",
    "helsinki": "Europe/Helsinki", "copenhagen": "Europe/Copenhagen",
    "athens": "Europe/Athens", "dublin": "Europe/Dublin", "moscow": "Europe/Moscow",
    "russia": "Europe/Moscow",
    # Americas / Oceania
    "losangeles": "America/Los_Angeles", "sanfrancisco": "America/Los_Angeles",
    "seattle": "America/Los_Angeles", "chicago": "America/Chicago",
    "washington": "America/New_York", "boston": "America/New_York",
    "toronto": "America/Toronto", "canada": "America/Toronto",
    "saopaulo": "America/Sao_Paulo", "brazil": "America/Sao_Paulo",
    "mexicocity": "America/Mexico_City", "sydney": "Australia/Sydney",
    "melbourne": "Australia/Melbourne", "australia": "Australia/Sydney",
    "auckland": "Pacific/Auckland", "usa": "America/New_York", "us": "America/New_York",
}


def _city_timezone(place: str) -> Optional[str]:
    """Resolve a city/country phrase to an IANA timezone name.

    Aliases win; otherwise scan zoneinfo for a zone whose last segment
    matches ("tokyo" → Asia/Tokyo, "new york" → America/New_York)."""
    name = re.sub(r"[^a-z]", "", (place or "").lower())
    if not name:
        return None
    if name in ("utc", "gmt", "greenwich"):
        return "UTC"
    if name in _TZ_ALIASES:
        return _TZ_ALIASES[name]
    try:
        from zoneinfo import available_timezones
        for zone in sorted(available_timezones()):
            if re.sub(r"[^a-z]", "", zone.split("/")[-1].lower()) == name:
                return zone
    except Exception:
        pass
    return None


# Windows drive path ending in .pdf inside an utterance.
_PDF_PATH_RE = re.compile(r'([A-Za-z]:[\\/][^"\'\r\n]+?\.pdf)\b', re.I)


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
    
    # Media playback controls, or a play/watch-content browser request.
    if _is_media_control(lower) or _play_browser_goal(cleaned):
        return True
    
    # Direct URL
    if re.search(r"(https?://\S+|[\w\-]+\.(com|org|net|io|dev|ai|gov|edu|co|me|app)[\S]*)", cleaned, re.IGNORECASE):
        return True
    
    return False


class WinBrowAgent:
    """The central orchestrator for Windows and Browser control."""

    def __init__(
        self,
        gateway: Optional[ModelGateway] = None,
        registry: Optional[ToolRegistry] = None,
        session: Optional[SessionMemory] = None,
        store: Optional[MemoryStore] = None,
        ask_user_fn: Optional[Any] = None,
    ):
        self.registry = registry or ToolRegistry()
        # register Python-backed computer atoms + app adapter ops once
        for _t in (get_computer_tools() + get_adapter_tools()
                   + get_installer_tools() + get_shell_tools()
                   + get_browser_tools() + get_document_tools()
                   + get_skill_script_tools() + get_integration_tools()
                   + get_subagent_tools()):
            if self.registry.get(_t.name) is None:
                self.registry.register(_t)
        # v3/M6: MCP bridge — cached catalogs offline-register; fresh
        # connects happen later in server startup / first use
        try:
            get_mcp_tools(self.registry)
        except Exception as _e:
            log.debug(f"MCP tool bootstrap skipped: {_e}")
        # v3/M2: procedural memory (captured plans → instant replay)
        from .memory.skills import get_skills
        self.skills = get_skills()
        self.generator = ScriptGenerator(self.registry)
        self.browser = BrowserController()
        self.memory = ContextMemory()          # legacy 3-slot memory (fast-path refs)
        self.history: list[dict[str, Any]] = []
        # --- v3: session working memory + episodic store + brain ---
        self.store = store or get_store()
        self.session = session or get_session()
        self.gateway = gateway or ModelGateway()
        self.router = WinBrowRouter(self.registry, store=self.store)
        # Optional UI/CLI callback: async (question) -> answer string. When
        # absent, a clarify stops the loop and is surfaced as needs_input.
        self.ask_user_fn = ask_user_fn
        self.brain = AgentBrain(
            gateway=self.gateway,
            registry=self.registry,
            store=self.store,
            executor=self.run_skill,
            tool_builder=self.build_new_tool,
            ask_user_fn=self.ask_user_fn,
        )
        self.subagents = SubagentManager(self)

    def get_context(self) -> WindowsContext:
        """Capture live Windows environment context."""
        return get_current_windows_context()

    def rebuild_gateway(self) -> None:
        """Rebuild the model gateway + brain (after env/settings change)."""
        from .brain import reset_gateway
        reset_gateway()
        self.gateway = ModelGateway()
        self.brain = AgentBrain(
            gateway=self.gateway,
            registry=self.registry,
            store=self.store,
            executor=self.run_skill,
            tool_builder=self.build_new_tool,
            ask_user_fn=self.ask_user_fn,
        )

    # ------------------------------------------------------------------ shared skill executor
    async def _open_requested_browser(self, app_name: str) -> dict[str, Any]:
        """Open/focus a browser the user named, keeping it under agent control.

        Launching Chrome via Start-Process and then browsing in the default
        browser (Brave) is what made "open chrome and go to ..." use the wrong
        window. Remember the requested browser and bring it up with CDP when it
        is not already running; return {} when the name is not a browser.
        """
        from .browser_task import canonical_browser, set_preferred_browser

        requested = canonical_browser(app_name)
        if not requested:
            return {}
        set_preferred_browser(requested)
        try:
            launched = await self.browser.launch_with_cdp(url="", guide=False)
        except Exception as e:
            log.debug(f"Requested-browser launch failed for {app_name}: {e}")
            return {}
        if not launched.get("cdp_backend"):
            # The requested browser is already running without remote
            # debugging. Fall back to normal focus/launch behavior.
            return {}
        return {
            "success": True,
            "output": (
                launched.get("message")
                or launched.get("stdout")
                or f"Opened {app_name} with browser control"
            ),
            "method": launched.get("method", ""),
            "cdp_backend": launched.get("cdp_backend"),
            "elapsed_ms": 20.0,
        }

    async def run_skill(self, tool_name: str, args: dict[str, str],
                        utterance: str = "", session_id: Optional[str] = None) -> dict[str, Any]:
        """Execute one registry tool by name — the single dispatch point shared
        by the fast-path and the brain (volume bypass, browser tools, templates).
        v3/M3: every execution passes through the tiered permission gate."""
        tool = self.registry.get(tool_name)
        if tool is None:
            return {"success": False, "output": f"Unknown tool '{tool_name}'"}

        # Scripted templates (registry + learned tools): render once up front so
        # the gate can tier by CONTENT (system-class content ⇒ T3).
        pre_rendered = tool.render_script(args) if getattr(tool, "impl", None) is None else ""

        # ── M3: tiered permission gate (single funnel, no exceptions) ──
        from .permissions import get_permissions
        perm = get_permissions()
        allowed, deny_reason, pending = await perm.gate(
            tool_name, args,
            utterance=utterance or "", session_id=session_id or "default",
            tool=tool, script=pre_rendered,
        )
        if not allowed:
            if pending is not None:
                return {
                    "type": "permission_required",
                    "tool": tool_name,
                    "success": False,
                    "pending": pending,
                    "output": (f"Approval needed for tier '{pending['tier']}': {tool_name}. "
                               f"confirmation_id={pending['confirmation_id']}"),
                }
            return {
                "type": "denied",
                "tool": tool_name,
                "success": False,
                "output": f"Action denied: {deny_reason or 'blocked by policy'}",
            }

        if tool_name == "create_text_file":
            from .files import write_named_text_file
            out = write_named_text_file(args)
            out.update({
                "type": "tool_execution",
                "tool": tool_name,
                "is_learned": False,
                "script": "",
                "elapsed_ms": 1.0,
            })
            return out
        if tool_name == "volume_set_level":
            level_arg = str(args.get("level", "50"))
            out_msg = set_windows_volume(level_arg)
            return {
                "type": "tool_execution",
                "tool": tool_name,
                "script": f"set_windows_volume({level_arg})",
                "success": "set to" in out_msg.lower(),
                "output": out_msg,
                "elapsed_ms": 1.0,
            }
        if tool_name in ("laya_ultrafast_browser", "web_search"):
            goal = str(args.get("goal") or args.get("query") or "")
            out = await self.browser.run_ultrafast_task(goal)
            return {
                "type": "browser",
                "tool": "laya_ultrafast_browser",
                "success": out.get("success", True),
                "output": out.get("final_answer") or str(out.get("steps", [])),
                "steps": out.get("steps", []),
                "total_elapsed_ms": out.get("total_elapsed_ms", 0.0),
                "method": out.get("method", "laya_ultrafast"),
            }
        if tool_name == "app_focus_or_launch":
            app_name = str(args.get("app_name") or args.get("app") or "").strip()
            if not app_name:
                # Never guess an app: the old "notepad" default launched an
                # unrelated window when the planner omitted the argument.
                return {
                    "type": "tool_execution",
                    "tool": tool_name,
                    "success": False,
                    "output": (
                        "No application was named. Say which app to open "
                        "(for example 'open Chrome' or 'open Notepad')."
                    ),
                    "elapsed_ms": 0.0,
                }
            browser_out = await self._open_requested_browser(app_name)
            if browser_out:
                browser_out.update({
                    "type": "tool_execution",
                    "tool": tool_name,
                })
                return browser_out
            from .windows import focus_or_launch_app
            out = await focus_or_launch_app(app_name)
            return {
                "type": "tool_execution",
                "tool": tool_name,
                "success": bool(out.get("success", True)),
                "output": out.get("output", f"Focused {app_name}"),
                "elapsed_ms": 15.0,
            }
        if tool_name == "open_common_app":
            app_name = str(args.get("app") or "").strip()
            browser_out = await self._open_requested_browser(app_name)
            if browser_out:
                browser_out.update({
                    "type": "tool_execution",
                    "tool": tool_name,
                    "is_learned": False,
                    "script": "",
                })
                return browser_out
        if tool_name == "type_text":
            text = str(args.get("text", ""))
            # Ensure target application is focused before typing
            target_app = str(args.get("app") or "").strip().lower()
            u_low = (utterance or "").lower()
            if not target_app:
                for app in ("notepad", "word", "code", "calc"):
                    if app in u_low:
                        target_app = app
                        break
            if target_app:
                from .windows import focus_or_launch_app
                await focus_or_launch_app(target_app)
                await asyncio.sleep(0.2)
            from .computer.driver import ComputerDriver
            driver = ComputerDriver(guard_screen=False)
            out = driver.type_text(text, via_clipboard=True)
            return {
                "type": "tool_execution",
                "tool": tool_name,
                "success": bool(out.get("success", True)),
                "output": f"Typed text into active window ({len(text)} chars)",
                "elapsed_ms": 20.0,
            }
        # v3/M1: Python-backed native computer atoms (no script template)
        if getattr(tool, "impl", None) == COMPUTER_IMPL:
            dispatch = COMPUTER_SKILL_DISPATCH.get(tool_name)
            if dispatch is not None:
                # Async atoms (OCR) run off the event loop via the brain's
                # dispatcher. This branch is still behind the permission gate.
                out = await dispatch(args)
            elif tool_name == "screen_read_text":
                # Safety net if the dispatcher entry is ever removed.
                out = await asyncio.to_thread(execute_computer_tool, tool_name, args)
            else:
                out = execute_computer_tool(tool_name, args)
            out.update({
                "type": "computer",
                "tool": tool_name,
            })
            return out
        # v3/M2: app adapter ops (CLI/COM/JSX packaged ops)
        if getattr(tool, "impl", None) == ADAPTER_IMPL:
            out = await execute_adapter_tool(tool_name, args)
            out.update({
                "type": "adapter",
                "tool": tool_name,
            })
            return out
        # build/test/verify commands in a project folder
        if getattr(tool, "impl", None) == SHELL_IMPL:
            out = await execute_shell_tool(tool_name, args)
            out.update({
                "type": "shell",
                "tool": tool_name,
            })
            return out
        # document extraction (PDF/PPTX/DOCX/XLSX/CSV/Markdown/...)
        if getattr(tool, "impl", None) == DOCUMENT_IMPL:
            out = await asyncio.to_thread(execute_document_tool, tool_name, args)
            out.update({
                "type": "document",
                "tool": tool_name,
            })
            return out
        # converted-skill bundled Python helpers (allowlisted, JSON spec)
        if getattr(tool, "impl", None) == SKILL_SCRIPTS_IMPL:
            out = await execute_skill_script_tool_async(tool_name, args)
            out.update({
                "type": "skill_script",
                "tool": tool_name,
            })
            return out
        # optional integration status (T0, read-only)
        if getattr(tool, "impl", None) == INTEGRATION_IMPL:
            out = await asyncio.to_thread(execute_integration_tool, tool_name, args)
            out.update({
                "type": "integration",
                "tool": tool_name,
            })
            return out
        # scoped child planners (delegate_task)
        if getattr(tool, "impl", None) == SUBAGENT_IMPL:
            out = await self.subagents.execute(tool_name, args)
            out.update({
                "type": "subagent",
                "tool": tool_name,
            })
            return out
        # System-2 browser primitives (navigate/read/interact/wait)
        if getattr(tool, "impl", None) == BROWSER_IMPL:
            out = await execute_browser_tool(tool_name, args, browser=self.browser)
            out.update({
                "type": "browser_tool",
                "tool": tool_name,
            })
            return out
        # v3/M3: installer ops (winget-first, T3-gated above)
        if getattr(tool, "impl", None) == INSTALLER_IMPL:
            op = INSTALL_OPS.get(tool_name)
            if op is None:
                return {"success": False, "output": f"Unknown installer op '{tool_name}'"}
            out = await op(args)
            out.update({"type": "installer", "tool": tool_name})
            # one-shot usage of the approved confirmation
            get_permissions().consume(tool_name, args)
            return out
        # v3/M6: MCP bridge tools (mcp://{server}/{remote-tool} carrier)
        if getattr(tool, "impl", None) == MCP_IMPL:
            out = await execute_mcp_tool(tool_name, tool, args)
            return out
        rendered = pre_rendered or tool.render_script(args)
        exec_out = await run_powershell(rendered)
        return {
            "type": "tool_execution",
            "tool": tool_name,
            "is_learned": tool.is_learned,
            "script": rendered,
            "success": exec_out.get("success", False),
            "output": exec_out.get("stdout") or exec_out.get("stderr") or "Done",
            "elapsed_ms": exec_out.get("elapsed_ms", 0),
        }

    # ------------------------------------------------------------------ brain helpers
    async def brain_chat(self, utterance: str, ctx: WindowsContext, session: SessionMemory) -> str:
        """Conversational answer via the gateway, grounded in session + episodic memory."""
        messages = build_chat_messages(utterance, _ctx_block(ctx), session.context_block())
        resp = await self.gateway.chat(messages, temperature=0.5, max_tokens=300)
        return resp.content.strip() or "Nothing to say."

    async def handle_browser_task(
        self, utterance: str, ctx: WindowsContext, session: SessionMemory,
        emit: Optional[Any] = None,
    ) -> tuple[Any, dict]:
        """Run every browser task through the same observe → act → verify loop.

        The loop is the router: simple goals finish in one or two steps, complex
        ones use more, and no task-type tier can misclassify the request.
        """
        contextualized, resolutions = session.resolve_references(utterance)
        if resolutions:
            log.info(f"Session resolved references: {resolutions}")
        try:
            max_steps = int(
                os.environ.get(
                    "WINBROW_BROWSER_MAX_STEPS",
                    os.environ.get("WINBROW_MAX_STEPS", "20"),
                )
            )
        except ValueError:
            max_steps = 20
        # Each task gets its own tab: the first navigation opens a fresh one so
        # the page the user is working in is never hijacked.
        self.browser.reset_session()
        note_browser_from_text(utterance)
        try:
            result = await self.brain.handle(
                contextualized, ctx, session, emit=emit, max_steps=max_steps,
            )
        finally:
            self.browser.end_task()
        return result, resolutions

    async def build_new_tool(self, task: str, ctx: Optional[WindowsContext] = None) -> dict[str, Any]:
        """Brain meta-skill 'build_tool': author a NEW PowerShell tool from a
        plain-language spec, run it once through the tiered permission gate,
        and persist it to tools/learned.json on success. Returns a dict whose
        'output' is fed to the brain as the step result."""
        try:
            gen = await self.generator.generate_and_execute(
                task, ctx or self.get_context(), tiered=True,
            )
        except Exception as e:
            return {"success": False, "output": f"Tool build failed: {e}"}
        tool = gen.get("tool")
        tool_name = getattr(tool, "name", "custom_action") if tool else "custom_action"
        if gen.get("success"):
            stdout = str(gen.get("stdout") or gen.get("stderr") or "ran cleanly")
            return {
                "success": True,
                "tool_name": tool_name,
                "output": f"Built and saved new tool '{tool_name}'. Run result: {stdout[:200]}",
            }
        if gen.get("permission") is not None:               # pending approval
            p = gen["permission"]
            return {
                "success": False,
                "output": (f"[permission-required] Built tool '{tool_name}' needs approval "
                           f"(tier {p.get('tier', '?')}). confirmation_id={p.get('confirmation_id')}"),
            }
        reason = gen.get("reason") or gen.get("error") or "generationfailed"
        return {"success": False, "output": f"Could not build tool: {reason}"}

    async def _generator_fallback(
        self, utterance: str, ctx: WindowsContext,
        api_key: Optional[str], provider: str,
        custom_endpoint: Optional[str], project_id: Optional[str],
    ) -> dict[str, Any]:
        """Dynamic PowerShell generation tier (pre-v3 fallback when no brain).
        v3/M3: generated scripts are tiered by policy.script_tier() — never
        below T2, T3 for system-class content — and must pass the permission
        gate before execution."""
        gen_out = await self.generator.generate_tiered(
            utterance, ctx,
            api_key=api_key, provider=provider,
            custom_endpoint=custom_endpoint, project_id=project_id,
        )
        return {
            "type": "generated_tool",
            "tool": gen_out.get("tool").name if gen_out.get("tool") else "custom_action",
            "is_learned": True,
            "script": gen_out.get("rendered_script", ""),
            "success": gen_out.get("success", False),
            "output": gen_out.get("stdout") or gen_out.get("stderr") or gen_out.get("reason", "Executed"),
            "elapsed_ms": gen_out.get("elapsed_ms", 0),
            "permission": gen_out.get("permission"),
        }

    def _capture_skill_candidate(self, utterance: str, brain_res: Any) -> None:
        """Turn a fully verified brain plan into a skill proposal (non-fatal).

        Only a run that ended with an explicit done/chat after all steps
        succeeded is eligible. Partial runs (budget exhausted, planner lost,
        a failed step, or a goal the loop knows is still missing) are never
        promoted. Captures start as "proposed" and must be approved before
        they can replay.
        """
        try:
            NON_CAPTURE_SKILLS = {
                "remember_fact", "recall_memory", "type_text", "computer_type",
                "computer_paste", "paste", "quick_note", "file_dialog_save",
                "build_tool", "repair_tool",
            }
            all_steps = getattr(brain_res, "steps", [])
            if not getattr(brain_res, "complete", False):
                log.debug("Skill capture skipped: run did not complete cleanly")
                return
            if len(all_steps) < 2 or not all(getattr(s, "success", False) for s in all_steps):
                log.debug("Skill capture skipped: partial or unsuccessful plan")
                return
            # If the plan contains transient text/payload steps, do not capture as static replayable macro
            if any(getattr(s, "skill", "") in NON_CAPTURE_SKILLS for s in all_steps):
                log.debug("Skill capture skipped: plan contains dynamic payload skill")
                return
            steps = [
                {"skill": s.skill, "args": dict(getattr(s, "args", {}) or {})}
                for s in all_steps
            ]
            steps, params = parameterize_steps(steps, utterance)
            description = (
                "Replays a verified workflow: "
                + " -> ".join(s["skill"] for s in steps)
                + f". Use when the request resembles: {utterance[:160]}"
            )
            self.skills.save(
                name=suggest_skill_name(steps, utterance),
                utterance=utterance, steps=steps,
                source="brain", description=description,
                parameters=[p.to_dict() for p in params],
                status="proposed",
            )
        except Exception as e:
            log.debug(f"Skill capture skipped ({e})")

    async def _fill_skill_parameters(self, skill: Any, utterance: str) -> dict[str, str]:
        """Map a new request onto a parameterized skill's placeholders.

        Parameterized skills cost one small LLM call; fully literal skills
        still replay with no model. Defaults fill anything the request leaves
        out. On gateway failure we keep defaults and let render_steps decide
        whether the skill is safe to run.
        """
        params = list(getattr(skill, "parameters", []) or [])
        values: dict[str, str] = {}
        if not params:
            return values
        for p in params:
            if p.default:
                values[p.name] = p.default
        if not self.gateway.available:
            return values

        specs = "\n".join(
            f"- {p.name}: {p.description or 'value from the new request'}"
            f"{' (required)' if p.required else ''}"
            f"{f' (default: {p.default})' if p.default else ''}"
            for p in params
        )
        messages = [
            {"role": "system", "content": (
                "Extract parameter values for a saved desktop workflow from a "
                "new request. Output ONLY one raw JSON object mapping each "
                "parameter name to a string value. Omit a parameter only when "
                "the request does not mention it and it has a default."
            )},
            {"role": "user", "content": (
                f"NEW REQUEST: {utterance}\n\n"
                f"WORKFLOW: {getattr(skill, 'description', '') or skill.name}\n"
                f"ORIGINAL REQUEST: {skill.utterance}\n\n"
                f"PARAMETERS:\n{specs}\n\nJSON:"
            )},
        ]
        try:
            resp = await self.gateway.chat(messages, temperature=0.0, max_tokens=400)
            data = extract_json_object(resp.content) or {}
            for p in params:
                value = data.get(p.name)
                if value is not None and str(value).strip():
                    values[p.name] = str(value).strip()
        except Exception as e:
            log.debug(f"Parameter fill failed for skill '{skill.id}': {e}")
        return values

    def _canned_chat(self) -> dict[str, Any]:
        return {
            "type": "chat",
            "output": (
                "I'm WinBrow, your desktop and browser control agent. "
                "I can control Windows (volume, dark mode, app launch, window management, settings, "
                "desktop cleanup, clipboard), drive Chrome/Edge (tab management, navigation, JS execution, "
                "screenshots, page reading), and plan multi-step tasks. Set OPENROUTER_API_KEY to unlock "
                "full conversational intelligence."
            ),
            "success": True,
        }

    async def _run_browser_goal(
        self,
        goal: str,
        ctx: Optional[WindowsContext] = None,
        session: Optional[SessionMemory] = None,
        emit: Optional[Any] = None,
    ) -> dict[str, Any]:
        """Run a browser goal through the brain first, ultrafast only as fallback."""
        ctx = ctx or self.get_context()
        session = session or self.session
        if self.gateway.available:
            try:
                brain_res, _resolutions = await self.handle_browser_task(
                    goal, ctx, session, emit=emit,
                )
                return brain_res.to_dict()
            except GatewayUnavailable:
                log.warning("Browser brain unavailable; using ultrafast fallback.")
            except Exception as e:
                log.warning(f"Browser brain failed ({e}); using ultrafast fallback.")
        out = await self.browser.run_ultrafast_task(goal)
        return {
            "type": "browser",
            "tool": "laya_ultrafast_browser",
            "success": out.get("success", True),
            "output": out.get("final_answer") or str(out.get("steps", [])),
            "steps": out.get("steps", []),
            "total_elapsed_ms": out.get("total_elapsed_ms", 0.0),
            "method": out.get("method", "laya_ultrafast"),
        }

    async def _dispatch_tool_execution(
        self,
        route: "Route",
        utterance: str,
        session_id: str,
        ctx: Optional[WindowsContext] = None,
        session: Optional[SessionMemory] = None,
        emit: Optional[Any] = None,
    ) -> dict:
        """Shared tool dispatch: fast specials first, single gated funnel else."""
        if route.tool.name == "volume_set_level":
            level_arg = route.args.get("level", "50")
            out_msg = set_windows_volume(level_arg)
            return {
                "type": "tool_execution",
                "tool": route.tool.name,
                "is_learned": False,
                "script": f"set_windows_volume({level_arg})",
                "success": "set to" in out_msg.lower(),
                "output": out_msg,
                "elapsed_ms": 1.0,
            }
        if route.tool.name == "laya_ultrafast_browser":
            goal = route.args.get("goal") or route.args.get("query") or utterance
            return await self._run_browser_goal(
                goal, ctx=ctx, session=session, emit=emit,
            )
        if route.tool.name == "web_search":
            goal = route.args.get("goal") or route.args.get("query") or utterance
            out = await self.browser.run_ultrafast_task(goal)
            return {
                "type": "browser",
                "tool": "laya_ultrafast_browser",
                "success": out.get("success", True),
                "output": out.get("final_answer") or str(out.get("steps", [])),
                "steps": out.get("steps", []),
                "total_elapsed_ms": out.get("total_elapsed_ms", 0.0),
                "method": out.get("method", "laya_ultrafast"),
            }
        # v3/M3: everything else (templates, atoms, adapters,
        # installers, learned tools) goes through the single
        # permission-gated executor — no direct paths.
        return await self.run_skill(
            route.tool.name, route.args,
            utterance=utterance, session_id=session_id,
        )

    async def _normalize_reroute(self, utterance: str, ctx, session, session_id: str):
        """v3/M4 Stage 2 (plan §12.3): one cheap gateway normalize rewrite,
        then ONE re-route. Returns (route, meta); route is None when the
        rewrite did not unlock an executable tool.
        """
        try:
            from .brain.normalize import normalize_utterance
            text, meta = await normalize_utterance(
                self.gateway, utterance, ctx, session, self.store,
                low_confidence=True,
            )
            if not meta.get("changed"):
                return None, meta
            route2 = await self.router.route(text, ctx, memory=self.memory)
            if route2.kind != "tool" or route2.tool is None or route2.confidence < 0.65:
                return None, meta
            meta["rerouted"] = True
            route2.latency_ms = route2.latency_ms or 0
            route2.tier = f"{route2.tier or route2.kind}+normalize"
            return route2, meta
        except Exception as e:
            log.debug(f"Normalize stage skipped: {e}")
            return None, None

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
                        "output": f"Failed to start browser with CDP: {cdp_result.get('output') or cdp_result.get('stdout', 'Unknown error')}"}
            if cdp_result.get("setup") == "enable_remote_debugging":
                # One-time browser opt-in not done yet: keep going (operations
                # fall back to keyboard shortcuts) but surface the hint.
                self._cdp_setup_hint = cdp_result.get("stdout")
                log.warning(f"CDP not enabled yet: {self._cdp_setup_hint}")

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
            # New tab by default (never hijack the tab holding the prompt);
            # only reuse the current tab when explicitly asked.
            new = not any(phrase in lower for phrase in ("same tab", "this tab", "current tab"))
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

    async def _execute_subagent_goal(
        self,
        utterance: str,
        ctx: WindowsContext,
        session: SessionMemory,
        session_id: str,
        emit: Optional[Any],
        started: float,
    ) -> dict[str, Any]:
        """Force an explicit or auto-detected subagent goal into the brain.

        The router's browser fast path would otherwise turn "research these
        three competitors" into one browser task, so the brain never gets to
        choose delegate_task. This path is deliberately narrow: explicit
        subagent wording or an independent-research/review candidate only.
        """
        if not self.gateway.available:
            execution = {
                "type": "error",
                "success": False,
                "output": (
                    "Subagents need a configured model provider. Add an "
                    "OpenRouter/OpenAI key in Settings, then try again."
                ),
            }
            resolutions = {}
        else:
            contextualized, resolutions = session.resolve_references(utterance)
            self.browser.reset_session()
            note_browser_from_text(utterance)
            try:
                brain_res = await self.brain.handle(
                    contextualized, ctx, session, emit=emit,
                )
                execution = brain_res.to_dict()
            except GatewayUnavailable:
                execution = {
                    "type": "error",
                    "success": False,
                    "output": "Subagents need a configured model provider.",
                }
            except Exception as exc:
                log.warning(f"Explicit subagent goal failed: {exc}")
                execution = {
                    "type": "error",
                    "success": False,
                    "output": f"Subagent request failed before planning: {exc}",
                }

        total_ms = round((time.perf_counter() - started) * 1000, 1)
        result: dict[str, Any] = {
            "utterance": utterance,
            "route": {
                "kind": "brain",
                "tool_name": "brain",
                "args": {"goal": utterance},
                "confidence": 1.0,
                "latency_ms": 0,
                "probabilities": {},
                "tier": "explicit_subagent",
            },
            "context": ctx.to_dict(),
            "execution": execution,
            "total_elapsed_ms": total_ms,
        }
        if resolutions:
            result["session_resolutions"] = resolutions
        self.history.append(result)
        if len(self.history) > 100:
            self.history = self.history[-100:]
        try:
            session.record_turn(utterance, result["route"], execution)
            self.store.record_command(utterance, result["route"], execution, total_ms)
        except Exception as exc:
            log.debug(f"Memory recording failed (non-fatal): {exc}")
        return result

    async def execute(
        self,
        utterance: str,
        api_key: Optional[str] = None,
        provider: str = "auto",
        custom_endpoint: Optional[str] = None,
        project_id: Optional[str] = None,
        *,
        emit: Optional[Any] = None,
        session_id: str = "default",
    ) -> dict[str, Any]:
        """
        Process user command end-to-end:
        1. Capture live Windows context
        2. Check for browser intent fast-path
        3. Route with Laya decision engine (~200ms)
        4. Execute or trigger brain / generator tier
        5. Record turn in session + episodic memory; return structured trace
        """
        t0 = time.perf_counter()
        session = self.session if session_id == "default" else get_session(session_id)

        # Ensure gateway is ready if keys became available in env
        if not self.gateway.available and (os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPENAI_API_KEY")):
            self.rebuild_gateway()

        # Subagents are automatic by default. Detect explicit requests and
        # independent-work candidates before replay/browser routing so they are
        # not hijacked by the browser fast path.
        explicit_subagents = is_explicit_subagent_request(utterance)
        auto_subagents = (
            subagents_enabled()
            and not subagents_explicit_only()
            and is_subagent_candidate(utterance)
        )

        # ── Check if user is approving or denying a pending permission action ──
        clean_words = (utterance or "").strip().lower().rstrip(".!?,")
        if clean_words in ("yes", "approve", "confirm", "allow", "proceed", "go ahead", "do it", "sure", "yep", "ok do it", "accept"):
            from .permissions import get_permissions
            pm = get_permissions()
            pending_list = pm.pending_list()
            if pending_list:
                last_pending = pending_list[-1]
                remember = last_pending.get("tier") == "t2"
                pm.resolve_pending(last_pending["confirmation_id"], approve=True, remember=remember)
                pm.consume(last_pending["tool"], last_pending["args"])
                log.info(f"User approved pending action '{last_pending['tool']}' via chat: re-running '{last_pending['utterance']}'")
                return await self.execute(last_pending["utterance"], session_id=session_id)
        elif clean_words in ("no", "deny", "cancel", "reject", "don't do it", "dont"):
            from .permissions import get_permissions
            pm = get_permissions()
            pending_list = pm.pending_list()
            if pending_list:
                last_pending = pending_list[-1]
                pm.resolve_pending(last_pending["confirmation_id"], approve=False)
                pm.consume(last_pending["tool"], last_pending["args"])
                return {
                    "utterance": utterance,
                    "route": {"kind": "stop", "tier": "permission"},
                    "execution": {"type": "denied", "success": False, "output": f"Action '{last_pending['tool']}' was denied by user."},
                    "total_elapsed_ms": 1.0,
                }

        # ── v3/M2: procedural replay ──
        # A captured skill matching this utterance replays its exact steps
        # locally (CLI/atom adapters ≈ tens of ms per step) before any routing
        # or LLM cost. Failures fall through to normal routing.
        try:
            replay_hit = self.skills.find_match(utterance)
        except Exception:
            replay_hit = None
        # Live web state changes between runs. A recorded browser workflow can
        # land on the wrong page and then "succeed" against stale assumptions,
        # so browser goals always go through the observe → act → verify loop.
        if replay_hit is not None and (
            _is_browser_intent(utterance) or explicit_subagents or auto_subagents
        ):
            log.info(f"Skill '{replay_hit[0].id}' skipped for browser/subagent task; using the brain loop.")
            replay_hit = None
        replay_steps: list[dict[str, Any]] = []
        if replay_hit is not None:
            skill, score = replay_hit
            try:
                values = await self._fill_skill_parameters(skill, utterance)
                replay_steps = skill.render_steps(values)
            except MissingSkillParameters as e:
                log.info(f"Skill '{skill.id}' not replayed: {e}; planning instead.")
                replay_hit = None
        if replay_hit is not None:
            skill, score = replay_hit
            from .memory.skills import MAX_STEPS as _SKILL_MAX
            started = time.perf_counter()
            steps_out: list[dict[str, Any]] = []
            all_ok = True
            for step in replay_steps[:_SKILL_MAX]:
                try:
                    r = await self.run_skill(step["skill"], step["args"],
                                             utterance=utterance, session_id=session_id)
                except Exception as e:
                    r = {"success": False, "output": str(e)}
                ok = bool(r.get("success"))
                all_ok = all_ok and ok
                steps_out.append({
                    "skill": step["skill"],
                    "args": step["args"],
                    "success": ok,
                    "output": str(r.get("output"))[:300],
                })
                if not ok:
                    break
            replay_ms = round((time.perf_counter() - started) * 1000, 1)
            if all_ok:
                try:
                    self.skills.mark_used(skill.id)
                except Exception:
                    pass
            result: dict[str, Any] = {
                "utterance": utterance,
                "route": {
                    "kind": "tool",
                    "tool_name": f"skill:{skill.id}",
                    "args": {},
                    "confidence": 1.0 if all_ok else 0.6,
                    "latency_ms": 0.5,
                    "probabilities": {},
                    "tier": "replay",
                },
                "context": {},
                "execution": {
                    "type": "replayed_skill",
                    "tool": f"skill:{skill.id}",
                    "success": all_ok,
                    "output": (f"Replayed skill '{skill.name}' ({len(steps_out)} steps, "
                               f"{replay_ms}ms, match {score:.2f})"),
                    "steps": steps_out,
                    "elapsed_ms": replay_ms,
                },
                "total_elapsed_ms": replay_ms,
            }
            log.info(f"Replayed skill '{skill.id}' ({replay_ms}ms, match {score:.2f})")
            self.history.append(result)
            if len(self.history) > 100:
                self.history = self.history[-100:]
            session.record_turn(utterance, result["route"], result["execution"])
            self.store.record_command(utterance, result["route"], result["execution"], replay_ms)
            return result

        # Context capture does process enumeration + COM reads (15-600ms);
        # keep it off the event loop so routing/execution stay responsive.
        loop = asyncio.get_event_loop()
        ctx = await loop.run_in_executor(None, self.get_context)

        # Explicit subagent wording, or an independent-research/review
        # candidate, must reach the brain. Otherwise the browser fast path can
        # swallow it before the planner ever sees delegate_task.
        if explicit_subagents or (auto_subagents and self.gateway.available):
            return await self._execute_subagent_goal(
                utterance, ctx, session, session_id, emit, t0,
            )

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

        # Multi-step command check: if the user wants multiple steps (e.g. search + save to desktop),
        # do NOT hijack with browser fast-path; let it route to the multi-step brain planner.
        is_multi_step = is_multi_step_intent(utterance)

        # MCP direct shortcuts for high-confidence lookups and local reads —
        # these MUST run before the browser/is_search fast-path so "search
        # wikipedia for X" reaches the wiki server instead of Google, and
        # before router/brain so git/time/pdf/local files work brainless.
        mcp_shortcut_tool = None
        mcp_shortcut_args: dict[str, Any] = {}
        if not is_multi_step and not is_interactive:
            wiki_tool = self.registry.get("mcp_wikipedia_get_summary") or self.registry.get("mcp_wikipedia_search_wikipedia")
            dict_tool = self.registry.get("mcp_dictionary_define")
            git_tools = {verb: self.registry.get(f"mcp_git_git_{verb}")
                         for verb in ("status", "log", "diff", "branch", "show")}
            time_tool = self.registry.get("mcp_time_get_current_time")
            pdf_extract = self.registry.get("mcp_pdf_pdf_extract_text")
            pdf_info = self.registry.get("mcp_pdf_pdf_info")
            pdf_search = self.registry.get("mcp_pdf_pdf_search")
            read_file = self.registry.get("mcp_filesystem_read_text_file")
            if dict_tool and (utterance_lower.startswith("define ") or utterance_lower.startswith("meaning of ")):
                word = re.sub(r"^(?:define|meaning of)\s+", "", cleaned_utterance, flags=re.I).strip().strip("\"'")
                if word:
                    mcp_shortcut_tool = dict_tool.name
                    mcp_shortcut_args = {"word": word}
            elif wiki_tool:
                topic = None
                explicit_search = False
                m_search = (re.match(r"^(?:search|look up|find)\s+(?:for\s+)?(?:wikipedia|wiki)\s+(?:for\s+)?(.+)$", cleaned_utterance, re.I)
                            or re.match(r"^search\s+for\s+(.+?)\s+(?:on|in)\s+(?:wikipedia|wiki)$", cleaned_utterance, re.I)
                            or re.match(r"^look\s+up\s+(.+?)\s+on\s+(?:wikipedia|wiki)$", cleaned_utterance, re.I))
                if m_search:
                    topic, explicit_search = m_search.group(1), True
                else:
                    m_wiki = re.match(r"^(?:wikipedia|wiki|what is|what are|who is|who was|details of|info on|tell me about)\s+(?:the\s+)?(.+)$", cleaned_utterance, re.I)
                    if m_wiki:
                        topic = m_wiki.group(1)
                if topic:
                    topic = topic.strip().strip("?.!\"'")
                    if topic and not _SYSTEM_INTENT_RE.search(utterance_lower) and not any(k in topic.lower() for k in ("volume", "time", "battery", "my ip", "network")):
                        chosen = wiki_tool
                        if explicit_search:
                            chosen = self.registry.get("mcp_wikipedia_search_wikipedia") or wiki_tool
                        mcp_shortcut_tool = chosen.name
                        arg_key = "title" if "get_summary" in chosen.name else "query"
                        mcp_shortcut_args = {arg_key: topic}
            if mcp_shortcut_tool is None and any(git_tools.values()):
                m_git = re.match(r"^(?:show|run|get|check|display)?\s*(?:me\s+)?(?:the\s+)?git\s+(status|log|diff|branch|show)\b(.*)$", cleaned_utterance, re.I)
                if m_git and git_tools.get(m_git.group(1).lower()):
                    mcp_shortcut_tool = git_tools[m_git.group(1).lower()].name
                    mcp_shortcut_args = {"repo_path": "."}
            if mcp_shortcut_tool is None and time_tool:
                m_time = re.match(r"^(?:what(?:'s| is)?\s+(?:the\s+)?)?(?:current\s+)?time(?:\s+is\s+it|\s+now)?\s+(?:in|at)\s+(.+)$", cleaned_utterance, re.I)
                if m_time:
                    zone = _city_timezone(m_time.group(1))
                    if zone:
                        mcp_shortcut_tool = time_tool.name
                        mcp_shortcut_args = {"timezone": zone}
            if mcp_shortcut_tool is None and (pdf_extract or pdf_info or pdf_search):
                m_pdf = _PDF_PATH_RE.search(cleaned_utterance)
                if m_pdf:
                    path = m_pdf.group(1).strip().strip("\"'")
                    if pdf_info and re.search(r"\b(info|metadata|details|pages|page count)\b", utterance_lower):
                        mcp_shortcut_tool = pdf_info.name
                        mcp_shortcut_args = {"path": path}
                    elif pdf_search and re.search(r"\b(search|find|look for|mention|mentions|containing)\b", utterance_lower):
                        tail = cleaned_utterance[m_pdf.end():]
                        query = re.sub(r"^(?:\s*(?:for|about|containing|mentions?|with)\s*)", "", tail, flags=re.I).strip(" ?.!\"'")
                        if query:
                            mcp_shortcut_tool = pdf_search.name
                            mcp_shortcut_args = {"path": path, "query": query}
                    if mcp_shortcut_tool is None and pdf_extract:
                        mcp_shortcut_tool = pdf_extract.name
                        mcp_shortcut_args = {"path": path}
            if mcp_shortcut_tool is None and read_file:
                m_rf = re.match(r"^(?:read|show)\s+(?:me\s+)?(?:the\s+)?(?:contents\s+of\s+|file\s+)?([A-Za-z]:[\\/].+)$", cleaned_utterance, re.I)
                if m_rf:
                    mcp_shortcut_tool = read_file.name
                    mcp_shortcut_args = {"path": m_rf.group(1).strip().strip("\"'")}

        if mcp_shortcut_tool:
            log.info(f"Direct MCP shortcut: {mcp_shortcut_tool} args={mcp_shortcut_args}")
            out = await self.run_skill(mcp_shortcut_tool, mcp_shortcut_args, utterance=utterance, session_id=session_id)
            # wikipedia search has a cold-start flake: the first calls after
            # the stdio server boots can return slow/empty results — one
            # bounded retry recovers most of those (warmup probes in mcp.py
            # make this rare)
            if mcp_shortcut_tool == "mcp_wikipedia_search_wikipedia" and out.get("success", True):
                try:
                    parsed = json.loads(str(out.get("output") or ""))
                    if isinstance(parsed, dict) and not parsed.get("results"):
                        await asyncio.sleep(0.7)
                        retry = await self.run_skill(
                            mcp_shortcut_tool, mcp_shortcut_args,
                            utterance=utterance, session_id=session_id)
                        try:
                            rp = json.loads(str(retry.get("output") or ""))
                            if isinstance(rp, dict) and rp.get("results"):
                                out = retry
                        except Exception:
                            pass
                except Exception:
                    pass
            total_ms = round((time.perf_counter() - t0) * 1000, 1)
            display_out = out.get("output") or "Done."
            try:
                parsed_out = json.loads(display_out)
                if isinstance(parsed_out, dict):
                    if "summary" in parsed_out:
                        display_out = f"**{parsed_out.get('title', '')}**\n\n{parsed_out['summary']}"
                    elif "meaning" in parsed_out:
                        meanings = parsed_out.get("meaning", [])
                        display_out = f"**{parsed_out.get('word', '')}**\n\n" + "\n".join(meanings)
            except Exception:
                pass
            result = {
                "utterance": utterance,
                "route": {
                    "kind": "tool",
                    "tool_name": mcp_shortcut_tool,
                    "args": mcp_shortcut_args,
                    "confidence": 0.95,
                    "latency_ms": 0,
                    "probabilities": {},
                    "tier": "mcp_shortcut",
                },
                "context": ctx.to_dict(),
                "execution": {
                    "type": "mcp",
                    "tool": mcp_shortcut_tool,
                    "success": out.get("success", True),
                    "output": display_out,
                    "elapsed_ms": out.get("elapsed_ms", 0.0),
                },
                "total_elapsed_ms": total_ms,
            }
            self.history.append(result)
            if len(self.history) > 100:
                self.history = self.history[-100:]
            session.record_turn(utterance, result["route"], result["execution"])
            self.store.record_command(utterance, result["route"], result["execution"], total_ms)
            return result

        # ── ALL BROWSER TASKS USE THE SAME OBSERVE → ACT → VERIFY LOOP ──
        # No task-type tiers. The brain decides how many steps this specific
        # goal needs: a simple task finishes in one or two, a complex one uses
        # more, and a bad earlier step is visible before the next decision.
        if not is_multi_step and (browser_fast or is_search) and self.gateway.available:
            try:
                brain_res, resolutions = await self.handle_browser_task(
                    utterance, ctx, session, emit=emit,
                )
                total_ms = round((time.perf_counter() - t0) * 1000, 1)
                result = {
                    "utterance": utterance,
                    "route": {
                        "kind": "browser",
                        "tool_name": "brain",
                        "args": {"goal": utterance},
                        "confidence": 1.0,
                        "latency_ms": 0,
                        "probabilities": {},
                        "tier": "browser_brain",
                    },
                    "context": ctx.to_dict(),
                    "execution": brain_res.to_dict(),
                    "total_elapsed_ms": total_ms,
                }
                if resolutions:
                    result["session_resolutions"] = resolutions
                self.history.append(result)
                if len(self.history) > 100:
                    self.history = self.history[-100:]
                session.record_turn(utterance, result["route"], result["execution"])
                self.store.record_command(
                    utterance, result["route"], result["execution"], total_ms)
                return result
            except GatewayUnavailable:
                log.warning("Browser brain unavailable; using one-shot fallback.")
            except Exception as e:
                log.warning(f"Browser brain failed ({e}); using one-shot fallback.")

        # One-shot fallback only when no model provider is usable.
        if not is_multi_step and (browser_fast or is_search):
            out = await self.browser.run_ultrafast_task(utterance)
            total_ms = round((time.perf_counter() - t0) * 1000, 1)
            result = {
                "utterance": utterance,
                "route": {
                    "kind": "tool",
                    "tool_name": "laya_ultrafast_browser",
                    "args": {"goal": utterance},
                    "confidence": 0.98,
                    "latency_ms": 0,
                    "probabilities": {},
                    "tier": "laya_ultrafast",
                },
                "context": ctx.to_dict(),
                "execution": {
                    "type": "browser",
                    "tool": "laya_ultrafast_browser",
                    "success": out.get("success", True),
                    "output": out.get("final_answer") or str(out.get("steps", [])),
                    "steps": out.get("steps", []),
                    "total_elapsed_ms": out.get("total_elapsed_ms", 0.0),
                    "method": out.get("method", "laya_ultrafast"),
                },
                "total_elapsed_ms": total_ms,
            }
            self.history.append(result)
            if len(self.history) > 100:
                self.history = self.history[-100:]
            session.record_turn(utterance, result["route"], result["execution"])
            self.store.record_command(utterance, result["route"], result["execution"], total_ms)
            return result

        route = await self.router.route(utterance, ctx, memory=self.memory)
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

        # ── v3/M4 autocorrect Stage 2: low-confidence NEW_ACTION → one cheap
        # gateway "normalize" rewrite against the live vocabulary block. ──
        # Fires as a FALLBACK after the brain tier fails (plan §12.3: rewrite
        # is for commands that "reach the brain"); unscripted-path cost is one
        # extra round-trip only when the brain had no plan.
        normalize_meta: Optional[dict] = None

        # 2. Dispatch
        if route.kind == "tool" and route.tool:
            result["execution"] = await self._dispatch_tool_execution(
                route, utterance, session_id, ctx=ctx, session=session, emit=emit,
            )
            if route.tool.name == "laya_ultrafast_browser" and \
                    result["execution"].get("type") == "brain":
                result["route"]["kind"] = "browser"
                result["route"]["tool_name"] = "browser_brain"
                result["route"]["tier"] = "browser_brain"
                route.tier = "browser_brain"

        elif route.kind == NEW_ACTION:
            # Brain first (context-aware multi-step + follow-ups). Falls back to
            # the generator tier when no LLM provider is configured.
            if self.gateway.available:
                try:
                    contextualized, resolutions = session.resolve_references(utterance)
                    if resolutions:
                        log.info(f"Session resolved references: {resolutions}")
                    # Every brain run starts a fresh browser session tab.
                    self.browser.reset_session()
                    note_browser_from_text(utterance)
                    brain_res = await self.brain.handle(
                        contextualized, ctx, session, emit=emit,
                    )
                    result["execution"] = brain_res.to_dict()
                    # v3/M2: procedural capture — successful brain plans become
                    # replayable skills (memory/skills.py), exact steps + args.
                    if brain_res.success and brain_res.kind == "action":
                        self._capture_skill_candidate(utterance, brain_res)
                    if resolutions:
                        result["session_resolutions"] = resolutions
                except GatewayUnavailable:
                    result["execution"] = await self._generator_fallback(
                        utterance, ctx, api_key, provider, custom_endpoint, project_id,
                    )
                except Exception as e:
                    # Stage-2 fallback (plan §12.3): one gateway rewrite
                    # against the vocabulary block, then one re-route. A
                    # tool route here skips the generator entirely.
                    route2, meta = await self._normalize_reroute(
                        utterance, ctx, session, session_id,
                    )
                    if route2 is not None:
                        normalize_meta = meta
                        result["normalize"] = meta
                        result["utterance"] = str(meta.get("text") or utterance)
                        result["route"] = {
                            "kind": route2.kind,
                            "tool_name": route2.tool.name if route2.tool else None,
                            "args": route2.args,
                            "confidence": round(route2.confidence, 3),
                            "latency_ms": route2.latency_ms,
                            "probabilities": route2.probabilities,
                            "tier": route2.tier or route2.kind,
                        }
                        route = route2
                        log.info(f"Post-normalize re-route → {route2.kind} "
                                 f"({route2.tool.name if route2.tool else None})")
                        result["execution"] = await self._dispatch_tool_execution(
                            route2, result["utterance"], session_id,
                            ctx=ctx, session=session, emit=emit,
                        )
                        if route2.tool and route2.tool.name == "laya_ultrafast_browser" and \
                                result["execution"].get("type") == "brain":
                            result["route"]["kind"] = "browser"
                            result["route"]["tool_name"] = "browser_brain"
                            result["route"]["tier"] = "browser_brain"
                            route2.tier = "browser_brain"
                    else:
                        log.warning(f"Brain failed ({e}); trying generator tier.")
                        result["execution"] = await self._generator_fallback(
                            utterance, ctx, api_key, provider, custom_endpoint, project_id,
                        )
            # If this looks like a known task (search, open, volume, etc.) that should
            # have been caught by heuristic routing, don't call the generator.
            elif _is_known_task(utterance):
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
            else:
                result["execution"] = await self._generator_fallback(
                    utterance, ctx, api_key, provider, custom_endpoint, project_id,
                )

        elif route.kind == CHAT:
            if self.gateway.available:
                try:
                    answer = await self.brain_chat(utterance, ctx, session)
                    result["execution"] = {
                        "type": "chat",
                        "output": answer,
                        "success": True,
                    }
                except Exception as e:
                    log.warning(f"Brain chat failed ({e}); canned reply.")
                    result["execution"] = self._canned_chat()
            else:
                result["execution"] = self._canned_chat()

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

        # v3: record the turn in session working memory + episodic store.
        try:
            session.record_turn(utterance, result["route"], result.get("execution", {}))
            self.store.record_command(utterance, result["route"], result.get("execution", {}), total_ms)
        except Exception as e:
            log.debug(f"Memory recording failed (non-fatal): {e}")

        # v3/M4 Stage 3: successful execution of a normalized rewrite →
        # persist learned corrections so Stage 1 handles it next time.
        try:
            if normalize_meta is not None and normalize_meta.get("changed") and \
                    result.get("execution", {}).get("success"):
                from .brain.normalize import learn_corrections
                changes = normalize_meta.get("changes") or []
                if changes:
                    learn_corrections(changes, self.store)
        except Exception as e:
            log.debug(f"Correction learning skipped (non-fatal): {e}")

        # M0 instrumentation: persistent JSONL trace (never breaks execution)
        # v3/M4: per-stage breakdown (normalize rewires, brain step latencies)
        try:
            from .trace import command_event
            extra: dict[str, Any] = {}
            if normalize_meta is not None:
                extra["normalize"] = normalize_meta
            brain_out = result.get("execution", {})
            if brain_out.get("type") == "brain":
                steps = brain_out.get("steps") or []
                extra["plan_elapsed_ms"] = brain_out.get("plan_elapsed_ms")
                extra["step_count"] = len(steps)
                extra["step_lats_ms"] = [
                    round(float(s.get("elapsed_ms", 0) or 0)) for s in steps
                ]
            command_event(
                utterance=utterance,
                tier=route.tier or route.kind,
                tool_name=route.tool.name if route.tool else None,
                confidence=route.confidence,
                route_latency_ms=route.latency_ms,
                success=result.get("execution", {}).get("success"),
                total_elapsed_ms=total_ms,
                extra=extra,
            )
        except Exception:
            pass

        return result
