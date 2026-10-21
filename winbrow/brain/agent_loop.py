"""
Agent Brain — Closed-Loop Plan → Act → Observe → Decide Executor (System 2)
==========================================================================
Handles everything the fast-path router can't:
  • contextual follow-ups ("make it louder", "and close it after")
  • multi-step tasks ("open photoshop, desaturate the image, save a copy")
  • dependent tasks (find a file, then move the path it returned)
  • questions about past activity ("what did I open yesterday?")
  • fact capture ("remember my exports folder is D:\\Exports")

Unlike a one-shot planner, the brain is called once per action: each call
receives the original goal plus the full history of steps and their results,
and emits EXACTLY ONE next step (or done/clarify/chat/stop). The executor runs
that step, appends the result to history, and loops. Values discovered by one
step (a file path, a search result) are therefore available verbatim to later
steps instead of both steps being planned blind up front.

Termination: done, clarify/chat, hard permission/policy stops, the same skill
failing twice in a row, provider loss mid-task, or the max_steps action budget.
Clarify pauses interactively when an ask_user_fn callback is wired; otherwise
the question is returned with needs_input=True for the UI to ask.

The gateway is optional: without any provider configured the brain raises
GatewayUnavailable and the orchestrator falls back to the generator tier.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .model_gateway import (
    PLANNER_CALL_CONFIG,
    GatewayError,
    GatewayResponse,
    GatewayUnavailable,
    ModelGateway,
    extract_json_object,
)
from . import prompts
from ..computer import COMPUTER_IMPL
from ..computer.tools import screen_read_text
from ..adapters.manager import ADAPTER_IMPL
from ..shell import SHELL_IMPL
from ..browser_tools import BROWSER_IMPL
from ..policy import PolicyError, validate_script_safety
from ..registry import ArgSpec, Tool
from ..utils.history import (
    count_recent_stuck,
    detect_stuck,
    extract_tried_services,
    format_history,
    last_url,
    tried_domains,
)

log = logging.getLogger("winbrow.brain.loop")

# 10 was too tight for real multi-step work (find a file → open a site →
# upload → verify). Env-tunable for latency-sensitive setups.
MAX_STEPS_DEFAULT = int(os.environ.get("WINBROW_MAX_STEPS", "20"))
# Warn the planner when the action budget is nearly exhausted so it can finish
# or ask the user instead of silently hitting the hard stop.
STEP_WARN_REMAINING = max(1, int(os.environ.get("WINBROW_STEP_WARN", "5")))
MAX_PLANNER_TOOLS = 140
# Backstop: one hung tool must never freeze the whole closed loop.
STEP_TIMEOUT_S = float(os.environ.get("WINBROW_STEP_TIMEOUT", "120"))
# The local Qwen brain runs with an 8192-token context. The planner prompt
# must leave room for the reply (max_tokens), so tool descriptions are clipped
# to one short line; arg names carry the actual calling contract.
TOOL_DESC_LIMIT = 100
META_DESC_LIMIT = 160

# MCP skills-block ranking: the full catalog is 200+ tools but the planner
# only sees MAX_PLANNER_TOOLS lines. Without intent ranking, niche servers
# (arxiv/crossref) crowd out core ones (filesystem) and the planner concludes
# "no skill can move a file". Servers are ranked by keyword intent.
MCP_PRIORITY_PER_SERVER = 8          # max tools any one server contributes
MCP_ALWAYS_SERVERS = ("wikipedia", "fetch", "dictionary", "open-meteo")
_FILE_INTENT_WORDS = ("file", "folder", "directory", "directories", "move",
                      "copy", "copies", "rename", "delete", "desktop",
                      "documents", "downloads", "path")
MCP_SERVER_KEYWORDS: Dict[str, tuple] = {
    "filesystem": _FILE_INTENT_WORDS,
    "wikipedia": ("wiki", "wikipedia", "tell me about"),
    "dictionary": ("define", "definition", "meaning", "dictionary", "spell"),
    "fetch": ("url", "link", "webpage", "website", "http"),
    "open-meteo": ("weather", "forecast", "temperature", "rain", "wind"),
    "arxiv": ("arxiv", "paper"),
    "crossref": ("citation", "doi", "journal", "paper"),
    "open-library": ("book", "author", "isbn"),
    "nominatim": ("map", "location", "address", "coordinates"),
    "rest-countries": ("country", "countries", "capital", "population"),
    "frankfurter": ("currency", "exchange rate", "exchange", "convert money"),
    "usgs-earthquake": ("earthquake", "seismic"),
    "hacker-news": ("hacker news",),
    "github-public": ("github",),
    "mdn-compat": ("mdn", "browser compatibility"),
    "gdelt-news": ("news", "headlines"),
    "reddit": ("reddit",),
    "git": ("git", "commit", "branch", "repository", "repo"),
    "time": ("time", "date", "clock", "timezone", "today", "tomorrow", "yesterday"),
    "memory": ("remember", "memory", "knowledge graph"),
    "pdf": ("pdf",),
    "sequential-thinking": ("reason", "analyze", "think step"),
}

# Meta-skills the brain can call that are not registry tools.
# Implemented in-process against the memory store.
META_SKILLS: dict[str, dict] = {
    "remember_fact": {
        "description": "Save a durable user preference or environment fact for future commands.",
        "args": {"fact": "The fact to remember, phrased as a complete sentence."},
    },
    "recall_memory": {
        "description": "Search past activity/history. Use when the user asks about something they did before.",
        "args": {"query": "What to search for in past activity."},
    },
    "build_tool": {
        "description": (
            "Author a brand-new, small, atomic PowerShell tool for a request that NO listed "
            "skill can do, run it once to verify, and save it for reuse. After it is built "
            "you get a follow-up planning turn to use it on the original request, so do NOT "
            "cram a whole multi-step goal into one build task."
        ),
        "args": {"task": "Plain-language spec of the ONE tool to build: what it must do, inputs, outputs."},
    },
    "repair_tool": {
        "description": (
            "Diagnose a tool that just failed, rewrite its script, register the fix, "
            "and verify it by running it once."
        ),
        "args": {
            "skill_name": "Name of the broken tool/skill.",
            "failure": "What went wrong (error text, wrong result).",
            "task": "Optional restatement of what the user wants the tool to accomplish.",
        },
    },
}

# Computer skills with async implementations. The shared skill executor awaits
# these callables instead of invoking the synchronous computer atom; OCR
# captures screenshots and runs PowerShell, so it must stay off the event loop.
COMPUTER_SKILL_DISPATCH: Dict[str, Callable[[dict], Awaitable[dict]]] = {
    "screen_read_text": lambda args: screen_read_text(
        region=args.get("region", "full")
    ),
}


_ACTION_VERBS = (
    "open", "create", "make", "write", "save", "move", "copy", "rename",
    "delete", "remove", "download", "upload", "launch", "start", "type",
    "set", "close", "send", "install", "uninstall", "print", "search",
    "find", "organize", "edit", "add", "change", "turn", "play", "pause",
    "extract", "zip", "unzip", "convert", "run",
)


def _looks_multi_part(utterance: str) -> bool:
    """Cheap guard for 'open X, create Y and write Z' style requests."""
    low = (utterance or "").lower()
    hits = {
        v for v in _ACTION_VERBS
        if re.search(rf"\b{v}(?:s|es|ed|ing)?\b", low)
    }
    return len(hits) >= 2


_CLARIFY_ANSWER_RE = re.compile(
    r"^\s*(?:option\s+|#)?(?:[1-9]\d?|first|second|third|fourth|fifth|"
    r"the\s+(?:first|second|third|fourth|fifth)|yes|no|both|neither|"
    r"that\s+one|this\s+one)\b",
    re.I,
)

_OPTION_LINE_RE = re.compile(
    r"^\s*(?:[-*•]|\(?[1-9]\d?[.)])\s+(.+?)\s*$"
)


def _option_key(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^\s*(?:[-*•]|\(?[1-9]\d?[.)])\s*", "", text)
    return (text or str(value or "")).strip().casefold()


def _extract_clarify_options(plan: dict, question: str = "") -> List[str]:
    """Normalize explicit options, or recover them from a numbered question."""
    options: List[str] = []
    seen: set[str] = set()
    raw = plan.get("options")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                item = item.get("value") or item.get("label") or item.get("text")
            value = str(item or "").strip()
            key = _option_key(value)
            if value and key not in seen:
                seen.add(key)
                options.append(value)
    if not options and question:
        for line in str(question).splitlines():
            match = _OPTION_LINE_RE.match(line)
            if not match:
                continue
            value = match.group(1).strip()
            key = _option_key(value)
            if value and key not in seen:
                seen.add(key)
                options.append(value)
    return options[:6]


def _merge_clarify_answer(utterance: str, session: Any) -> str:
    """Make a short/clicked answer self-contained by restoring its original goal."""
    pending = getattr(session, "pending_clarification", None)
    if not isinstance(pending, dict):
        return utterance
    text = (utterance or "").strip()
    pending_options = [
        str(option).strip() for option in (pending.get("options") or [])
        if str(option).strip()
    ]
    clicked_option = any(
        _option_key(text) == _option_key(option) for option in pending_options
    )
    if not _CLARIFY_ANSWER_RE.match(text) and not clicked_option:
        return utterance
    original = str(pending.get("goal") or "").strip()
    question = str(pending.get("question") or "").strip()
    if not original:
        return utterance
    merged = (
        f"{original}\n"
        f"User answered the earlier clarification with: {text}"
        + (f"\nClarification question: {question}" if question else "")
    )
    if pending_options and any(
        _option_key(option) not in question.casefold()
        for option in pending_options
    ):
        merged += "\nOptions: " + " | ".join(
            f"{index}) {option}"
            for index, option in enumerate(pending_options, 1)
        )
    return merged


_CODE_ARTIFACT_RE = re.compile(
    r"\b[\w.-]+\.(?:go|py|js|ts|tsx|jsx|html|css|java|c|cpp|cs|rs|sh|ps1|rb|"
    r"php|swift|kt|txt|md|json|csv)\b", re.I)
_CODE_WORD_RE = re.compile(
    r"\b(?:program|code|script|source file|go file|python file|"
    r"javascript file|html file|css file)\b", re.I)
_OUTPUT_WORD_RE = re.compile(
    r"\b(?:run|execute|compile|prints?|output|test|verify)\b", re.I)
_WRITE_SKILL_WORDS = {"write", "save", "type", "paste", "note"}
_WRITE_SKILL_NAMES = {"create_text_file", "create_file", "file_dialog_save"}
_RUN_SKILL_WORDS = {"run", "exec", "execute", "compile", "test", "verify",
                    "terminal", "powershell", "shell"}


def _skill_tokens(name: str) -> set[str]:
    return {t for t in re.split(r"[\W_]+", (name or "").lower()) if t}


def _needs_code_work(utterance: str) -> bool:
    return bool(_CODE_ARTIFACT_RE.search(utterance or "")
                or _CODE_WORD_RE.search(utterance or ""))


def _did_write(history: List[BrainStepResult]) -> bool:
    for step in history:
        if not step.success:
            continue
        if step.skill in _WRITE_SKILL_NAMES:
            return True
        if _skill_tokens(step.skill) & _WRITE_SKILL_WORDS:
            return True
    return False


def _did_run_or_verify(history: List[BrainStepResult]) -> bool:
    return any(step.success and (_skill_tokens(step.skill) & _RUN_SKILL_WORDS)
               for step in history)


def _completion_gap(utterance: str, history: List[BrainStepResult]) -> str:
    """Return why a done/chat wrap-up is not yet backed by HISTORY."""
    ok_steps = [s for s in history if s.success]
    if not ok_steps:
        return ""
    uploads = [i for i, s in enumerate(history)
               if s.success and s.skill == "browser_upload_file"]
    if uploads:
        last_upload = uploads[-1]
        verified = any(
            s.success and s.skill in ("browser_wait", "browser_read_page")
            for s in history[last_upload + 1:]
        )
        if not verified:
            return (
                "HISTORY shows browser_upload_file set the file, but no "
                "browser_wait or browser_read_page followed it. Verify the "
                "upload completed before emitting done."
            )
    if _looks_multi_part(utterance) and len(ok_steps) == 1:
        return (
            "GOAL asks for several things, but HISTORY shows only one "
            "completed step. Re-check EVERY part of GOAL. If anything is "
            "still missing, emit the NEXT required action; only emit done/chat "
            "when all parts are shown complete."
        )
    if _needs_code_work(utterance) and not _did_write(history):
        return (
            "GOAL asks for a code or text artifact, but HISTORY shows no file "
            "written. Create the file with the requested content, then emit the "
            "next action or done."
        )
    if _OUTPUT_WORD_RE.search(utterance or "") and not _did_run_or_verify(history):
        return (
            "GOAL specifies program output. Run or verify the program and "
            "confirm HISTORY shows the expected result before emitting done."
        )
    return ""


def _one_line(text: str, limit: int) -> str:
    """Collapse whitespace and clip to a token budget at a word boundary."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


@dataclass
class BrainStepResult:
    skill: str
    args: dict
    success: bool
    output: str
    why: str = ""
    elapsed_ms: float = 0.0
    pending: Optional[dict] = None
    warning: str = ""
    url: str = ""
    title: str = ""

    def to_dict(self) -> dict:
        d = {
            "skill": self.skill, "args": self.args, "success": self.success,
            "output": self.output[:400], "why": self.why, "elapsed_ms": self.elapsed_ms,
        }
        if self.pending:
            d["pending"] = self.pending
        if self.warning:
            d["warning"] = self.warning
        if self.url:
            d["url"] = self.url
        if self.title:
            d["title"] = self.title
        return d


@dataclass
class BrainResult:
    kind: str                       # "action" | "chat" | "stop"
    success: bool = False
    response: str = ""
    steps: List[BrainStepResult] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    plan_elapsed_ms: float = 0.0
    pending: Optional[dict] = None
    complete: bool = False          # explicit done/chat after all goal parts
    evidence: dict = field(default_factory=dict)
    needs_input: bool = False       # response is a question for the user
    question: str = ""
    options: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {
            "type": "brain",
            "kind": self.kind,
            "success": self.success,
            "complete": self.complete,
            "output": self.response,
            "steps": [s.to_dict() for s in self.steps],
            "provider": self.provider,
            "model": self.model,
            "plan_elapsed_ms": self.plan_elapsed_ms,
        }
        if self.pending:
            d["pending"] = self.pending
        if self.evidence:
            d["evidence"] = self.evidence
        if self.needs_input:
            d["needs_input"] = True
            d["question"] = self.question or self.response
            d["status"] = "needs_input"
            if self.options:
                d["options"] = list(self.options)
        return d


class AgentBrain:
    """The System 2 planner/executor loop."""

    def __init__(
        self,
        gateway: ModelGateway,
        registry: Any,                     # ToolRegistry
        store: Any,                        # MemoryStore
        executor: Callable[[str, dict], Awaitable[dict]],
        max_steps: Optional[int] = None,
        tool_builder: Optional[Callable[[str, Any], Awaitable[dict]]] = None,
        ask_user_fn: Optional[Callable[[str], Awaitable[str]]] = None,
    ):
        self.gateway = gateway
        self.registry = registry
        self.store = store
        self.executor = executor           # async (tool_name, args) -> execution dict
        # Read the env at construction time: server.py imports the agent
        # modules before it loads .env, so a module-level default would ignore
        # WINBROW_MAX_STEPS. WinBrowAgent is constructed after the .env load.
        self.max_steps = int(
            max_steps if max_steps is not None
            else os.environ.get("WINBROW_MAX_STEPS", str(MAX_STEPS_DEFAULT))
        )
        self.step_warn_remaining = max(1, int(
            os.environ.get("WINBROW_STEP_WARN", str(STEP_WARN_REMAINING))))
        self.tool_builder = tool_builder   # async (task, ctx) -> build result | None
        # Optional interactive pause: receives a clarify question and returns
        # the user's answer. Without it, clarify is surfaced as needs_input.
        self.ask_user_fn = ask_user_fn

    # ------------------------------------------------------------------ skills block
    def _skills_block(self, ctx: Any, utterance: str = "") -> str:
        lines = []
        available = self.registry.available(ctx)
        u_low = (utterance or "").lower()

        def _kw_hit(kw: str) -> bool:
            if " " in kw:
                return kw in u_low
            return re.search(rf"\b{re.escape(kw)}(?:s|es)?\b", u_low) is not None

        # Group tools by type
        python_backed, mcp_priority, seeded = [], [], []
        per_server: Dict[str, List] = {}
        for t in available:
            impl = getattr(t, "impl", None)
            if impl in (COMPUTER_IMPL, ADAPTER_IMPL, SHELL_IMPL, BROWSER_IMPL):
                python_backed.append(t)
            elif impl == "mcp":
                script = getattr(t, "script", "") or ""
                srv = script[len("mcp://"):].split("/", 1)[0] if script.startswith("mcp://") else ""
                per_server.setdefault(srv, []).append(t)
            else:
                seeded.append(t)

        # Intent-ranked MCP selection: relevant servers first (filesystem for
        # file/folder verbs etc.), capped per server; the rest overflow to the
        # general budget so nothing starves the tail of the catalog entirely.
        mcp_other: List = []
        for srv, tools in per_server.items():
            srv_key = srv.replace("_", "-")
            kws = MCP_SERVER_KEYWORDS.get(srv_key, ())
            relevant = bool(kws) and any(_kw_hit(kw) for kw in kws)
            if relevant or srv_key in MCP_ALWAYS_SERVERS:
                # filesystem matches on file intent → show it whole (14 tools,
                # they cover move/create/list/read/write which the planner needs)
                keep = tools if (relevant and srv_key == "filesystem") \
                    else tools[:MCP_PRIORITY_PER_SERVER]
                mcp_priority.extend(keep)
                mcp_other.extend(tools[len(keep):])
            else:
                mcp_other.extend(tools)

        selected = list(python_backed) + list(mcp_priority)
        remaining_budget = max(MAX_PLANNER_TOOLS - len(selected), 0)
        selected.extend(seeded[:remaining_budget])
        remaining_budget2 = max(MAX_PLANNER_TOOLS - len(selected), 0)
        selected.extend(mcp_other[:remaining_budget2])

        for t in selected:
            args_desc = ", ".join(
                f"{a.name}({a.kind})" for a in t.args
            ) or "no args"
            desc = _one_line(t.description, TOOL_DESC_LIMIT)
            lines.append(f"- {t.name}({args_desc}): {desc}")
        for name, meta in META_SKILLS.items():
            args_desc = ", ".join(f"{k}(text)" for k in meta["args"])
            desc = _one_line(meta["description"], META_DESC_LIMIT)
            lines.append(f"- {name}({args_desc}): {desc}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ main entry
    async def handle(
        self,
        utterance: str,
        ctx: Any,
        session: Any,                       # SessionMemory
        emit: Optional[Callable[[dict], Awaitable[None]]] = None,
        max_steps: Optional[int] = None,
    ) -> BrainResult:
        """Closed-loop Plan → Act → Observe executor.

        Each iteration asks the planner for exactly ONE next action with the
        full step history in context, executes it, appends the result, and
        repeats. This lets later steps consume values discovered by earlier
        ones (e.g. a path found by list_directory feeds move_file) instead of
        planning everything blindly up front.

        Terminates on done/chat/stop/clarify, permission or policy hard stop,
        the same skill failing twice in a row, provider loss mid-task, or the
        max_steps action budget.

        Raises GatewayUnavailable when no provider responds on the first call
        (the orchestrator then falls back to the generator tier).
        """
        utterance = _merge_clarify_answer(utterance, session)
        memory_block = session.context_block()
        # enrich with episodic hits when the user asks about the past
        if any(w in utterance.lower() for w in ("yesterday", "earlier", "last time", "before", "history", "did i")):
            hits = self.store.search_episodic(utterance, limit=5)
            if hits:
                memory_block += "\nRelevant past activity:\n" + "\n".join(
                    f"  {h.label[:120]}" for h in hits
                )

        result = BrainResult(
            kind="action",
            provider=self.gateway.providers[0].name if self.gateway.providers else "",
            model=self.gateway.providers[0].model if self.gateway.providers else "",
        )
        history: List[BrainStepResult] = []
        last_failed_skill = ""
        consecutive_failures = 0
        last_plan_response = ""
        coaching = ""
        repeats: Dict[tuple, int] = {}
        done_challenged = False
        clarify_rounds = 0
        step_budget = max_steps if max_steps is not None else self.max_steps

        while len(result.steps) < step_budget:
            # Rebuild every turn: build_tool/repair_tool register tools
            # mid-task and the next decision must see them.
            skills_block = self._skills_block(ctx, utterance)
            turn_memory = memory_block
            if coaching:
                turn_memory += f"\nPLANNER FEEDBACK: {coaching}"
                coaching = ""
            remaining = step_budget - len(result.steps)
            if remaining <= self.step_warn_remaining:
                turn_memory += (
                    f"\nSTEP BUDGET WARNING: {remaining} action(s) remaining. "
                    "If the task is nearly complete, finish it now. If you are "
                    "stuck or missing information, emit clarify instead of "
                    "retrying another search."
                )
            messages = prompts.build_loop_messages(
                utterance, format_history(history), _ctx_block(ctx),
                turn_memory, skills_block,
                tried_block=extract_tried_services(history),
                steps_remaining=remaining,
            )
            try:
                plan = await self._ask_plan(messages, emit)
            except GatewayUnavailable as e:
                if not history:
                    raise
                log.warning(f"Planner unavailable mid-task ({e}); finalizing.")
                return self._partial_finalize(
                    result, history, last_plan_response, "planner unavailable",
                )
            except GatewayError as e:
                if not history:
                    raise
                # One extra planning attempt rides out transient provider
                # failures (429s, timeouts) instead of silently ending the task.
                log.warning(f"Planner error mid-task ({e}); retrying once.")
                await asyncio.sleep(1.5)
                try:
                    plan = await self._ask_plan(messages, emit)
                except (GatewayUnavailable, GatewayError) as e2:
                    log.warning(f"Planner error mid-task after retry ({e2}); finalizing.")
                    return self._partial_finalize(
                        result, history, last_plan_response, "planner unavailable",
                    )

            kind = str(plan.get("kind", "chat"))
            text = str(plan.get("response") or plan.get("summary")
                       or plan.get("question") or "").strip()
            log.info(f"brain turn {len(result.steps) + 1}: kind={kind}")

            if kind == "stop":
                result.kind = "stop"
                result.success = True
                result.response = text or "Standing by."
                return result

            if kind == "chat":
                if plan.get("_parse_failed"):
                    # Both parse attempts failed. Do not pretend the model
                    # wrapped up successfully — surface the partial outcome.
                    log.warning("Planner returned an unusable response mid-task; stopping honestly.")
                    result.kind = "action"
                    result.success = False
                    result.response = (
                        "The planner returned an unusable response"
                        + (f" after {len(result.steps)} step(s): "
                           f"{self._history_summary(history)}" if history else "")
                        + ". Try rephrasing the request."
                    )
                    return result
                if history:
                    # A model may call a request finished before the goal is
                    # actually evidenced in HISTORY (the reported chicken-go
                    # case). Ask it to verify once; if it still cannot show
                    # the missing artifact/run, finish honestly but mark the
                    # run incomplete so it is never captured as a skill.
                    gap = _completion_gap(utterance, history)
                    if gap and not done_challenged:
                        done_challenged = True
                        coaching = gap
                        log.info("Planner tried to finish early; asking it to verify.")
                        continue
                    # A chat reply AFTER executed steps is the model wrapping
                    # up (some models answer "…done" instead of kind=done).
                    # Keep it an action result so capture/UI stay consistent.
                    result.kind = "action"
                    result.success = True
                    result.complete = not bool(gap)
                    result.evidence = (
                        dict(plan.get("evidence"))
                        if isinstance(plan.get("evidence"), dict) else {}
                    )
                    result.response = text or self._history_summary(history) or "Done."
                    return result
                result.kind = "chat"
                result.success = True
                result.response = text
                return result

            if kind == "clarify":
                question = text or "Could you clarify what you want me to do?"
                result.options = _extract_clarify_options(plan, question)
                # Interactive pause: when a UI callback is wired, block this
                # loop (not the event loop) until the user answers, then let
                # the planner see the reply in HISTORY on the next turn.
                if self.ask_user_fn is not None:
                    try:
                        answer = await self.ask_user_fn(question)
                    except Exception as e:
                        log.warning(f"ask_user callback failed ({e}); surfacing question.")
                        answer = ""
                    answer = str(answer or "").strip()
                    if answer and clarify_rounds < 3:
                        clarify_rounds += 1
                        reply = BrainStepResult(
                            "_user_reply", {"question": question}, True,
                            f"User answered: {answer}",
                        )
                        history.append(reply)
                        if emit:
                            try:
                                await emit({"type": "brain_step", "index": len(result.steps),
                                            "total": len(result.steps), **reply.to_dict()})
                            except Exception:
                                pass
                        continue
                # No UI hooked up (or it timed out): surface the question and
                # stop honestly. The user's next turn becomes a new request.
                result.kind = "chat"
                result.success = True
                result.needs_input = True
                result.question = question
                result.response = question
                return result

            if kind == "done":
                gap = _completion_gap(utterance, history)
                if gap and not done_challenged:
                    done_challenged = True
                    coaching = gap
                    log.info("Planner tried to finish early; asking it to verify.")
                    continue
                result.kind = "action"
                result.success = True
                result.complete = not bool(gap)
                result.evidence = (
                    dict(plan.get("evidence"))
                    if isinstance(plan.get("evidence"), dict) else {}
                )
                result.response = text or self._history_summary(history) or "Done."
                return result

            steps = _extract_steps(plan)
            if not steps:
                result.kind = "chat"
                result.success = True
                result.response = text or "I couldn't plan that. Try rephrasing."
                return result

            log.info(f"brain step chosen: {steps[0].get('skill', '?')}")

            if text:
                last_plan_response = text

            done_sigs = {_step_signature(s.skill, s.args) for s in history if s.success}
            remaining = step_budget - len(result.steps)
            for step in steps[:remaining]:
                skill = str(step.get("skill", ""))
                args = dict(step.get("args") or {})
                why = str(step.get("why", ""))
                sig = _step_signature(skill, args)

                # A small model sometimes re-emits a step that already
                # succeeded (e.g. create_directory). Re-running it wastes the
                # budget and hides the rest of the task; nudge it forward.
                if sig in done_sigs:
                    repeats[sig] = repeats.get(sig, 0) + 1
                    if repeats[sig] >= 3:
                        result.kind = "action"
                        result.success = False
                        result.response = (
                            f"Stopped: the planner kept repeating '{skill}' even after it "
                            f"succeeded. {self._history_summary(history)}"
                        ).strip()
                        return result
                    coaching = (
                        f"'{skill}' with those exact arguments ALREADY SUCCEEDED — "
                        "it is in HISTORY. Do NOT repeat it. Emit the NEXT action needed, "
                        "or done only if every part of GOAL is complete."
                    )
                    prior = next(
                        (s for s in reversed(history)
                         if s.success and _step_signature(s.skill, s.args) == sig),
                        None,
                    )
                    if prior and prior.output:
                        coaching = (
                            f"'{skill}' with those exact arguments ALREADY SUCCEEDED. "
                            f"Its result was: {prior.output[:300]} "
                            "Do NOT repeat it; use this result and emit the NEXT action."
                        )
                    log.info(f"Planner repeated completed step '{skill}'; re-planning with feedback.")
                    break

                step_res = await self._run_step(skill, args, why, ctx=ctx)
                result.steps.append(step_res)
                history.append(step_res)
                if detect_stuck(history):
                    stuck = (
                        "STUCK: You called the same skill twice and got identical "
                        "output. The page has not changed. Do not call this skill "
                        "again. Try a different approach or emit clarify."
                    )
                    step_res.warning = (
                        f"{step_res.warning} {stuck}".strip()
                        if step_res.warning else stuck
                    )
                    log.warning(
                        f"Brain is stuck: '{skill}' returned identical output "
                        f"on consecutive steps."
                    )
                log.info(
                    f"brain step result: {skill} success={step_res.success} "
                    f"output={step_res.output[:140]!r}"
                )

                if emit:
                    try:
                        await emit({
                            "type": "brain_step",
                            "index": len(result.steps),
                            "total": len(result.steps),
                            **step_res.to_dict(),
                        })
                    except Exception:
                        pass

                # Hard stops: approval gates / policy denials — re-planning
                # cannot change the outcome, so surface them immediately.
                if step_res.output.startswith("[permission-required]") or step_res.output.startswith("[denied]"):
                    result.success = False
                    result.response = step_res.output
                    if step_res.pending:
                        result.pending = step_res.pending
                    return result

                # A warning alone does not stop a stubborn planner. After two
                # stuck warnings in the recent window, stop spinning and ask
                # the user instead of burning the remaining action budget.
                recent_stuck = count_recent_stuck(history)
                if recent_stuck >= 2:
                    question = (
                        f"I've been trying to complete '{utterance}' but I'm "
                        f"going in circles. Here's where I am:\n"
                        f"  Current page: {last_url(history)}\n"
                        f"  Services tried: {', '.join(tried_domains(history)) or '(none)'}\n\n"
                        f"How would you like me to proceed? Or can you do this "
                        f"step manually and let me know when to continue?"
                    )
                    log.warning(
                        f"Brain stuck {recent_stuck} times; forcing clarify."
                    )
                    result.kind = "chat"
                    result.success = True
                    result.needs_input = True
                    result.question = question
                    result.response = question
                    return result

                if step_res.success:
                    done_sigs.add(sig)
                    consecutive_failures = 0
                    last_failed_skill = ""
                    continue

                consecutive_failures = (
                    consecutive_failures + 1 if skill == last_failed_skill else 1
                )
                last_failed_skill = skill
                if consecutive_failures >= 2:
                    result.success = False
                    result.response = (
                        f"Couldn't complete: {skill} failed twice. "
                        f"{step_res.output[:200]}"
                    )
                    return result
                # Failure is in HISTORY now — the next turn re-plans with it.
                break

        # Budget exhausted without a terminal decision: say so instead of
        # silently implying the goal finished.
        result = self._finalize(result, history, last_plan_response)
        if len(result.steps) >= step_budget:
            result.response = (
                f"{result.response} (Stopped after {len(result.steps)} steps — "
                "step limit reached; the task may be incomplete.)"
            ).strip()
        return result

    def _finalize(self, result: BrainResult, history: List[BrainStepResult],
                  plan_response: str = "") -> BrainResult:
        """Stop the loop and report honestly from what actually ran."""
        if not history:
            result.success = False
            result.response = plan_response or "I couldn't plan that. Try rephrasing."
            return result
        last = history[-1]
        if not last.success:
            result.success = False
            result.response = (
                f"Couldn't complete: {last.skill} failed. {last.output[:200]}"
            )
            return result
        result.success = True
        result.response = plan_response or self._history_summary(history) or "Done."
        return result

    def _partial_finalize(self, result: BrainResult, history: List[BrainStepResult],
                          plan_response: str, reason: str) -> BrainResult:
        """End mid-task but say why, instead of silently reporting success."""
        res = self._finalize(result, history, plan_response)
        note = f"Stopped early: {reason}."
        res.response = f"{res.response} {note}".strip() if res.response else note
        return res

    @staticmethod
    def _history_summary(history: List[BrainStepResult]) -> str:
        done = [s for s in history if s.success]
        return "; ".join(
            re.sub(r"\s+", " ", s.output or "").strip()[:100]
            for s in done[-2:]
        ) or ""

    # ------------------------------------------------------------------ planning
    async def _ask_plan(self, messages: list, emit: Optional[Callable]) -> dict:
        """One planner call → parsed JSON plan. Retries once on parse failure."""
        t0 = time.perf_counter()
        for attempt in range(2):
            resp = await self.gateway.chat(
                messages,
                json_schema=prompts.PLANNER_JSON_SCHEMA,
                # Some reasoning models ignore reasoning.max_tokens and spend
                # most of max_tokens thinking. A 16k total budget leaves room
                # for the JSON plan even after ~14k reasoning tokens. The local
                # context guard clamps this when prompt + reply would exceed an
                # 8192-token window.
                temperature=PLANNER_CALL_CONFIG["temperature"],
                max_tokens=PLANNER_CALL_CONFIG["max_tokens"],
            )
            plan = extract_json_object(resp.content)
            if plan:
                if isinstance(plan, list):
                    plan = {"kind": "action", "steps": plan}
                elif isinstance(plan, dict):
                    if "plan" in plan and isinstance(plan["plan"], dict):
                        plan = plan["plan"]
                    if "kind" not in plan:
                        if isinstance(plan.get("step"), dict):
                            plan["kind"] = "action"
                        elif isinstance(plan.get("steps"), list):
                            plan["kind"] = "action"
                        elif "summary" in plan:
                            plan["kind"] = "done"
                        elif "question" in plan:
                            plan["kind"] = "clarify"
                        elif "response" in plan:
                            plan["kind"] = "chat"
                        elif isinstance(plan.get("action"), str):
                            plan["kind"] = "action"
                            plan.setdefault(
                                "step",
                                {"skill": plan["action"],
                                 "args": plan.get("args", {})},
                            )

            if plan and "kind" in plan:
                if emit:
                    try:
                        n_steps = len(plan.get("steps", []) or [])
                        if not n_steps and plan.get("step"):
                            n_steps = 1
                        await emit({
                            "type": "brain_plan",
                            "kind": plan.get("kind"),
                            "steps": n_steps,
                            "provider": resp.provider,
                            "model": resp.model,
                            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                        })
                    except Exception:
                        pass
                return plan
            snippet = resp.content[:200].replace("\n", " ") if getattr(resp, "content", None) else "<empty>"
            log.warning(
                f"Plan parse failed (attempt {attempt + 1}); retrying. "
                f"finish_reason={getattr(resp, 'finish_reason', '')!r} "
                f"reasoning_len={len(getattr(resp, 'reasoning', '') or '')} "
                f"Raw snippet: {snippet!r}"
            )
            retry_messages = list(messages)
            # An empty assistant turn is not a useful correction and some
            # providers reject it; only quote a real (malformed) reply.
            if getattr(resp, "content", ""):
                retry_messages.append(
                    {"role": "assistant", "content": resp.content[:500]})
            retry_messages.append(
                {"role": "user",
                 "content": "That was not valid JSON per the schema. Output ONLY the JSON object."})
            messages = retry_messages
        # Last resort: surface as an unusable response, not a successful wrap-up.
        return {"kind": "chat", "_parse_failed": True,
                "response": "I couldn't plan that. Try rephrasing."}

    # ------------------------------------------------------------------ step execution
    async def _run_step(self, skill: str, args: dict, why: str = "", ctx: Any = None) -> BrainStepResult:
        t0 = time.perf_counter()

        def _ms() -> float:
            return round((time.perf_counter() - t0) * 1000, 1)

        try:
            if skill == "remember_fact":
                fact = str(args.get("fact", "")).strip()
                if fact:
                    self.store.add_fact(fact)
                    return BrainStepResult(skill, args, True, f"Remembered: {fact}", why, _ms())
                return BrainStepResult(skill, args, False, "No fact provided", why, _ms())

            if skill == "recall_memory":
                query = str(args.get("query", "")).strip()
                hits = self.store.search_episodic(query or "recent", limit=5)
                if hits:
                    lines = "\n".join(f"- {h.label} ({_fmt_ts(h.timestamp)})" for h in hits)
                    return BrainStepResult(skill, args, True, lines, why, _ms())
                return BrainStepResult(skill, args, True, "No matching past activity found.", why, _ms())

            if skill == "build_tool":
                task = str(args.get("task", args.get("goal", ""))).strip()
                if not task:
                    return BrainStepResult(skill, args, False,
                                           "No task description provided for build_tool.", why, _ms())
                if self.tool_builder is None:
                    return BrainStepResult(skill, args, False,
                                           "Tool building is not available in this session.", why, _ms())
                out = await self.tool_builder(task, ctx)
                return BrainStepResult(
                    skill, args, bool(out.get("success", False)),
                    str(out.get("output") or "Tool build failed."), why, _ms(),
                )

            if skill == "repair_tool":
                sname = str(
                    args.get("skill_name") or args.get("tool_name") or args.get("skill") or ""
                ).strip()
                if not sname:
                    return BrainStepResult(skill, args, False,
                                           "repair_tool needs the name of the tool to fix.", why, _ms())
                failure = str(
                    args.get("failure") or args.get("error") or args.get("problem") or ""
                ).strip()
                return await self._repair_tool_step(sname, dict(args), failure, why)

            tool = self.registry.get(skill)
            if tool is None:
                return BrainStepResult(skill, args, False, f"Unknown skill '{skill}'", why)

            try:
                exec_out = await asyncio.wait_for(
                    self.executor(skill, args), timeout=STEP_TIMEOUT_S)
            except asyncio.TimeoutError:
                return BrainStepResult(
                    skill, args, False,
                    f"Execution timed out after {STEP_TIMEOUT_S:.0f}s.",
                    why, round((time.perf_counter() - t0) * 1000, 1),
                    warning=f"This step timed out after {STEP_TIMEOUT_S:.0f}s.")
            # v3/M3: the permission is a hard stop — no re-plan can bypass it
            if exec_out.get("type") == "permission_required":
                p = exec_out.get("pending") or {}
                return BrainStepResult(
                    skill, args, False,
                    (f"[permission-required] Needs your approval first: '{skill}' "
                     f"(tier {p.get('tier', '?')}). Approve it, then say the command again. "
                     f"confirmation_id={p.get('confirmation_id')}"),
                    why,
                    round((time.perf_counter() - t0) * 1000, 1),
                    pending=p,
                )
            if exec_out.get("type") == "denied":
                return BrainStepResult(
                    skill, args, False,
                    f"[denied] {exec_out.get('output', 'Blocked by policy')}",
                    why,
                    round((time.perf_counter() - t0) * 1000, 1),
                )
            annotated = annotate_result(skill, args, exec_out)
            success = bool(annotated.get("success", False))
            output = str(annotated.get("output") or annotated.get("stdout") or "Done")
            return BrainStepResult(
                skill, args, success, output, why,
                round((time.perf_counter() - t0) * 1000, 1),
                warning=str(annotated.get("_warning") or ""),
                url=str(annotated.get("url") or ""),
                title=str(annotated.get("title") or ""),
            )
        except Exception as e:
            log.warning(f"Brain step '{skill}' raised: {e}")
            return BrainStepResult(
                skill, args, False, f"Execution error: {e}", why,
                round((time.perf_counter() - t0) * 1000, 1),
                warning=f"This step raised: {e}",
            )

    # ------------------------------------------------------------------ self-repair
    async def _repair_tool_step(
        self, sname: str, args: dict, failure: str, why: str,
    ) -> BrainStepResult:
        """Rewrite a broken tool's PowerShell script via the fixer prompt,
        register the fix in memory, verify with one run, then persist."""
        t0 = time.perf_counter()

        def _ms() -> float:
            return round((time.perf_counter() - t0) * 1000, 1)

        try:
            tool = self.registry.get(sname)
            if tool is None:
                return BrainStepResult("repair_tool", args, False,
                                       f"No tool named '{sname}' to repair — build a new tool instead.",
                                       why, _ms())
            script = getattr(tool, "script", "") or ""
            if not script:
                return BrainStepResult("repair_tool", args, False,
                                       f"Tool '{sname}' is implemented in code, not a script — nothing to rewrite.",
                                       why, _ms())
            if not self.gateway.providers:
                return BrainStepResult("repair_tool", args, False,
                                       "No model provider configured for script repair.",
                                       why, _ms())

            args_block = ", ".join(f"{a.name}({a.kind})" for a in tool.args) or "no args"
            user_task = str(args.get("task", "")).strip()
            messages = prompts.build_tool_repair_messages(
                tool.name, tool.description, script, args_block,
                failure or user_task or
                "The tool ran but produced a wrong or empty result.",
            )
            resp = await self.gateway.chat(
                messages,
                json_schema=prompts.TOOL_FIXER_JSON_SCHEMA,
                temperature=0.1,
                max_tokens=2000,
            )
            fix = extract_json_object(resp.content) or {}
            new_script = str(fix.get("script", "")).strip()
            if not new_script:
                log.warning(f"repair_tool: fixer returned no script for '{sname}'")
                return BrainStepResult(
                    "repair_tool", args, False,
                    f"Attempted repair produced no new script for '{sname}'.",
                    why, _ms(),
                )
            try:
                validate_script_safety(new_script)
            except PolicyError as e:
                return BrainStepResult("repair_tool", args, False,
                                       f"Rejected repaired script by safety policy: {e}",
                                       why, _ms())

            fixed = Tool(
                name=str(fix.get("tool_name") or tool.name),
                description=str(fix.get("description") or tool.description),
                script=new_script,
                scope=fix.get("scope") or getattr(tool, "scope", None),
                args=[ArgSpec(
                    name=a["name"],
                    kind=a.get("kind", "text"),
                    instructions=a.get("instructions", ""),
                    criteria=a.get("criteria", {}),
                    default=a.get("default"),
                ) for a in (fix.get("args") or [])] or list(tool.args),
                examples=[str(e) for e in (fix.get("examples") or [])][:3] or list(tool.examples),
                is_learned=True,
            )

            # Register the fix in memory first so the executor can run it;
            # roll back to the original tool if verification fails.
            self.registry.register(fixed)

            defaults = {a.name: (a.default or "") for a in fixed.args}
            extras = {
                k: (str(v) if isinstance(v, (int, float)) else v)
                for k, v in args.items()
                if k in defaults and k not in ("skill_name", "tool_name", "skill",
                                               "failure", "error", "problem", "task")
            }
            verify_args = {**defaults, **extras}
            try:
                exec_out = await asyncio.wait_for(
                    self.executor(fixed.name, verify_args), timeout=STEP_TIMEOUT_S)
            except Exception as e:
                self.registry.register(tool)
                return BrainStepResult("repair_tool", args, False,
                                       f"Repaired '{sname}' errored while verifying: {e}",
                                       why, _ms())
            if not exec_out.get("success", False):
                self.registry.register(tool)
                out_txt = str(exec_out.get("output") or exec_out.get("stderr") or "")
                return BrainStepResult("repair_tool", args, False,
                                       f"Repaired '{sname}' still fails: {out_txt[:200]}",
                                       why, _ms())

            # Persist: learned tools overwrite in place; seed tools get a
            # '<name>_fixed' sibling so the fix survives restarts (learned
            # entries are never allowed to shadow built-ins, by registry
            # policy).
            persisted = fixed.name
            try:
                if tool.is_learned:
                    self.registry.save_learned(fixed)
                else:
                    seed_copy = dataclasses.replace(fixed, name=f"{tool.name}_fixed")
                    self.registry.save_learned(seed_copy)
                    persisted = seed_copy.name
            except Exception as e:
                log.warning(f"repair_tool: could not persist fixed '{sname}': {e}")

            out_txt = str(exec_out.get("output") or exec_out.get("stdout") or "Done")
            return BrainStepResult(
                "repair_tool", args, True,
                (f"Repaired and verified '{fixed.name}' (saved as '{persisted}'). "
                 f"Run result: {out_txt[:160]}"),
                why, _ms(),
            )
        except Exception as e:
            log.warning(f"repair_tool failed for '{sname}': {e}")
            return BrainStepResult("repair_tool", args, False,
                                   f"Repair error: {e}", why, _ms())


def _url_parts(url: str) -> tuple[str, str]:
    """(host, path) identity for navigation checks; tolerates missing schemes."""
    raw = str(url or "").strip()
    if not raw:
        return "", "/"
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return "", "/"
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    return host, path


def annotate_result(skill: str, args: dict, raw_result: dict) -> dict:
    """Attach explicit verification flags before the brain sees a step result.

    This never blocks execution. It surfaces mismatches in HISTORY so the
    planner can correct course instead of assuming the action worked.
    """
    result = dict(raw_result or {})
    warnings: List[str] = []

    if skill == "browser_navigate":
        intended = str((args or {}).get("url") or "")
        actual = str(result.get("url") or "")
        if intended and not actual:
            result["_nav_matched"] = False
            warnings.append(
                f"Navigation to {intended!r} returned no URL. "
                "Verify the page state before proceeding."
            )
        if intended and actual:
            intended_host, intended_path = _url_parts(intended)
            actual_host, actual_path = _url_parts(actual)
            path_matches = bool(
                intended_path == "/"
                or actual_path == intended_path
                or actual_path.startswith(intended_path + "/")
            )
            matched = bool(
                actual.lower().startswith(intended.rstrip("/").lower())
                or (intended_host and intended_host == actual_host and path_matches)
            )
            result["_nav_matched"] = matched
            if not matched:
                warnings.append(
                    f"Navigated to {actual!r} but intended {intended!r}. "
                    "Verify this is the right page before proceeding."
                )

    if skill == "screen_read_text":
        if result.get("success") and not str(result.get("text") or "").strip():
            warnings.append(
                "OCR succeeded but returned no text. The visible content "
                "may be an image, a loading spinner, or an empty screen. "
                "Try waiting and retrying, or use a different skill."
            )
        if not result.get("success"):
            warnings.append(
                "OCR failed. If this is a browser task, use "
                "browser_read_page or browser_get_accessibility_tree "
                "instead — OCR is not needed for browser content."
            )

    if result.get("error"):
        warnings.append(f"This step failed: {result['error']}")
    elif result.get("success") is False:
        detail = (
            result.get("output") or result.get("stderr")
            or result.get("error") or "unknown failure"
        )
        warnings.append(f"This step failed: {str(detail)[:300]}")

    if warnings:
        result["_warning"] = " ".join(warnings)
    return result


def _step_signature(skill: str, args: dict) -> tuple:
    """Stable identity for a step, used to detect repeats of completed work."""
    try:
        args_key = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        args_key = str(args)
    return (skill, args_key)


def _extract_steps(plan: dict) -> List[dict]:
    """Closed-loop plans carry ONE `step`; legacy plans carry a `steps` array."""
    step = plan.get("step")
    if isinstance(step, dict) and step.get("skill"):
        return [step]
    steps = plan.get("steps")
    if isinstance(steps, list):
        return [s for s in steps if isinstance(s, dict) and s.get("skill")]
    action = plan.get("action")
    if isinstance(action, str) and action:
        return [{"skill": action, "args": dict(plan.get("args") or {})}]
    return []


def _history_block(history: List[BrainStepResult]) -> str:
    """Token-bounded history: old steps compressed, last 5 shown in detail."""
    return format_history(history)


def _ctx_block(ctx: Any) -> str:
    try:
        d = ctx.to_dict()
    except Exception:
        d = {}
    active = d.get("active_title") or d.get("active_app") or "Desktop"
    apps = ", ".join((d.get("running_apps") or [])[:10])
    vol = d.get("volume_level", "?")
    return f"Frontmost: {active}\nRunning apps: {apps}\nVolume: {vol}"


def _fmt_ts(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
