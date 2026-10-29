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

Termination: done, clarify/chat, hard permission/policy stops, a repeated
failure that survives the bounded recovery turns, provider loss mid-task, or the
max_steps action budget. Read-only observations are exempt from the
completed-step guard: re-reading a file/page after a save, edit, move or rename
is verification, not a redundant repeat.
Clarify pauses interactively when an ask_user_fn callback is wired; otherwise
the question is returned with needs_input=True for the UI to ask.

The gateway is optional: without any provider configured the brain raises
GatewayUnavailable and the orchestrator falls back to the generator tier.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .model_gateway import (
    GatewayError,
    GatewayResponse,
    GatewayUnavailable,
    ModelGateway,
    extract_json_object,
    get_reasoning_config,
    planner_temperature,
)
from . import prompt as prompts
from .skill_registry import (
    SkillMeta,
    build_skills_metadata,
    build_tool_detail,
    build_tool_index,
    load_skill_body,
    load_skill_registry,
    resolve_skill_key,
)
from .skill_writer import (
    analyse_task_for_skill,
    register_skill_in_registry,
    write_skill_to_disk,
)
from ..computer.tools import screen_read_text
from ..events import (
    bus as event_bus,
    new_step_id,
    step_scope,
)
from ..policy import PolicyError, validate_script_safety
from ..registry import ArgSpec, Tool
from ..trace import log_event
from ..utils.history import (
    count_recent_stuck,
    detect_repeated_failure,
    detect_stuck,
    extract_tried_services,
    format_history,
    history_full_enabled,
    last_url,
    tried_domains,
)

log = logging.getLogger("winbrow.brain.loop")

# Progressive-disclosure skills: metadata loads once at startup, full bodies
# load only after the planner picks a skill. WINBROW_SKILLS_DIR overrides the
# bundled folder for development/testing.
SKILLS_DIR = os.environ.get("WINBROW_SKILLS_DIR") or str(
    Path(__file__).resolve().parent.parent / "skills"
)
SKILL_REGISTRY: dict[str, SkillMeta] = load_skill_registry(SKILLS_DIR)
SKILLS_METADATA: str = build_skills_metadata(SKILL_REGISTRY)

# Background skill-analysis tasks. Keep strong references until they finish:
# asyncio only holds a weak reference to a task, so a fire-and-forget task can
# be garbage-collected before it writes the SKILL.md.
_SKILL_WRITER_TASKS: set[asyncio.Task] = set()

# 10 was too tight for real multi-step work (find a file → open a site →
# upload → verify). Env-tunable for latency-sensitive setups.
MAX_STEPS_DEFAULT = int(os.environ.get("WINBROW_MAX_STEPS", "20"))

# Read-only observations are idempotent: a second identical call is a valid
# "look again" (the page may have loaded since), not a redundant repeat. The
# completed-step guard must not skip these or short-circuit them with the first
# (possibly stale) result.
#
# Filesystem/MCP observations belong here too: after a save, edit, move or
# rename the ONLY way to verify the new state is to re-read. The planner prompt
# (SKILL RULE 17) explicitly requires reading a file back before claiming done,
# so blocking that re-read would make a correct plan impossible to finish.
REPEATABLE_SKILLS = frozenset({
    "browser_read_page",
    "browser_read_full",
    "browser_get_accessibility_tree",
    "browser_wait",
    "screen_read_text",
    "current_time",
    "mcp_time_get_current_time",
    "read_document",
    "find_files",
    "find_large_files",
    "mcp_filesystem_read_file",
    "mcp_filesystem_read_text_file",
    "mcp_filesystem_read_multiple_files",
    "mcp_filesystem_read_media_file",
    "mcp_filesystem_list_directory",
    "mcp_filesystem_list_directory_with_sizes",
    "mcp_filesystem_directory_tree",
    "mcp_filesystem_search_files",
    "mcp_filesystem_get_file_info",
    "mcp_filesystem_list_allowed_directories",
    # Focusing an already-focused window is a harmless no-op. Skipping it used
    # to derail plans that re-focus before typing.
    "focus_window",
})

# Skills whose name contains one of these words are read-only observations and
# may safely run again (a newer version of the same MCP/native/learned tool is
# still a "look again"). Mutating words win: a tool called read_and_write_file
# is NOT repeatable.
_READ_ONLY_SKILL_TOKENS = frozenset({
    "read", "list", "get", "search", "find", "info", "stat", "tree",
    "head", "tail", "wait", "observe", "inspect", "describe", "current",
    "time", "state",
})
_MUTATING_SKILL_TOKENS = frozenset({
    "write", "edit", "move", "copy", "delete", "remove", "create",
    "rename", "type", "paste", "key", "click", "save", "upload",
    "download", "install", "uninstall", "run", "exec", "execute",
    "launch", "kill", "close", "set", "send", "print", "convert",
})

# After this many consecutive re-emissions of the same completed step, stop
# burning planner turns and finalize honestly. Read-only repeats get
# READ_REPEAT_LIMIT free passes first; only a genuine read loop reaches this.
REPEAT_HARD_LIMIT = max(3, int(os.environ.get("WINBROW_REPEAT_LIMIT", "6")))
# A bounded number of identical read re-runs is allowed so the planner can see
# post-save/post-move state. Beyond that, the same signature is treated as a
# loop and skipped instead of burning the whole action budget.
READ_REPEAT_LIMIT = max(1, int(os.environ.get("WINBROW_READ_REPEATS", "3")))
# A repeated failure gets a bounded recovery attempt (diagnose actual state,
# then try a different path/skill) before the loop gives up. This keeps a stale
# path from ending an otherwise recoverable task.
FAILURE_RECOVERIES = max(0, int(os.environ.get("WINBROW_FAILURE_RECOVERIES", "2")))

# Warn the planner when the action budget is nearly exhausted so it can finish
# or ask the user instead of silently hitting the hard stop.
STEP_WARN_REMAINING = max(1, int(os.environ.get("WINBROW_STEP_WARN", "5")))
# Kept for callers/tests that used the old per-turn cap. The compact tool
# index no longer needs a hard cap, but the name remains part of the API.
MAX_PLANNER_TOOLS = 140
# Backstop: one hung tool must never freeze the whole closed loop.
STEP_TIMEOUT_S = float(os.environ.get("WINBROW_STEP_TIMEOUT", "120"))

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


# Read-only skills that are safe to execute concurrently when the planner
# emits a batch. Browser skills are excluded: the controller already batches
# reads internally and shares one page state.
PARALLEL_READ_SKILLS = frozenset({
    "web_search", "web_fetch", "read_document", "find_files",
    "recall_memory", "current_time",
})


def parallel_reads_enabled() -> bool:
    return os.environ.get("WINBROW_PARALLEL_READS", "0").strip().lower() in {
        "1", "true", "yes", "on",
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


# Delegation is opt-in. These phrases are deliberately conservative: generic
# "agents" in a sentence should not turn on subagents, but an explicit request
# to use/delegate/spawn/run agents should.
_SUBAGENT_REQUEST_RE = re.compile(
    r"\bsub[ -]?agents?\b"
    r"|\bagent team\b"
    r"|\b(?:parallel|multiple|separate)\s+agents?\b"
    r"|\bdelegat(?:e|ion|ing)\b[^.?!]{0,60}\b"
    r"(?:task|work|research|analysis|review|verif\w*|sub[ -]?agent|"
    r"agent|researcher|analyst|writer|reviewer)\b"
    r"|\b(?:use|spawn|run|launch|create|ask|have|with)\b[^.?!]{0,40}?"
    r"\b(?:sub[ -]?agents?|worker agents?|agents?|researcher|analyst|writer|reviewer|verifier)\b",
    re.I,
)


def is_explicit_subagent_request(text: str) -> bool:
    """True only when the user explicitly asks for subagents/delegation."""
    return bool(_SUBAGENT_REQUEST_RE.search(text or ""))


def subagents_enabled() -> bool:
    return os.environ.get("WINBROW_SUBAGENTS", "1").strip().lower() not in {
        "0", "false", "off", "no",
    }


def subagents_explicit_only() -> bool:
    return os.environ.get("WINBROW_SUBAGENTS_EXPLICIT", "0").strip().lower() in {
        "1", "true", "yes", "on",
    }


def delegation_allowed_for(text: str) -> bool:
    """Whether this turn may use delegate_task.

    Default: automatic delegation is allowed. Set
    WINBROW_SUBAGENTS_EXPLICIT=1 to require an explicit user request.
    """
    if not subagents_enabled():
        return False
    if subagents_explicit_only():
        return is_explicit_subagent_request(text)
    return True


def is_subagent_candidate(text: str) -> bool:
    """Conservative auto-delegation hint for independent multi-part work.

    This is not a decision to delegate; it only ensures the brain (which can
    choose delegate_task) sees research/compare/review tasks instead of letting
    the browser fast path swallow them.
    """
    low = (text or "").lower()
    patterns = (
        r"\bresearch\b.*\b(?:competitors?|market|sources?|papers?|literature|"
        r"vendors?|companies|landscape|due diligence|options?)\b",
        r"\bcompare\b.*\b(?:files?|documents?|versions?|contracts?|reports?|"
        r"competitors?|vendors?|options?|pdfs?|spreadsheets?)\b",
        r"\b(?:review|verify|audit|analy[sz]e)\b.*\b(?:code|diff|patch|pr|"
        r"pull request|report|claim|artifact|file|document|dataset|tests?)\b",
        r"\b(?:these|several|multiple|three|four|five|\d+)\b.*\b(?:files?|"
        r"documents?|pages?|sources?|competitors?|papers?|reports?|pdfs?|"
        r"spreadsheets?|sites?)\b",
        r"\b(?:in parallel|parallel)\b.*\b(?:files?|sources?|pages?|tasks?|agents?)\b",
        r"\b(?:summari[sz]e|read)\b.*\b(?:these|multiple|several|\d+)\b.*\b"
        r"(?:files?|documents?|pages?|pdfs?|reports?)\b",
        r"\b(?:ask|have|use|spawn|run|launch|create|delegate)\b.*\b(?:agents?|"
        r"researcher|analyst|writer|reviewer|verifier)\b",
    )
    return any(re.search(pattern, low) for pattern in patterns)


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


def _is_repeatable_skill(skill: str) -> bool:
    """True when re-running ``skill`` with the same args is a safe observation.

    Explicit names win. For everything else (MCP/native/learned tools whose
    names we cannot enumerate), classify by name tokens: read/list/search/...
    are observations, while write/edit/move/... are mutations and must keep
    their completed-step guard.
    """
    if not skill:
        return False
    if skill in REPEATABLE_SKILLS:
        return True
    # Browser/web steps keep their dedicated repeat policy: the browser may
    # need a fresh read, but duplicate searches/fetches are handled explicitly
    # elsewhere (_is_fetch_escalation) and should not become free re-runs.
    if skill.startswith(("browser_", "web_")):
        return False
    tokens = _skill_tokens(skill)
    if tokens & _MUTATING_SKILL_TOKENS:
        return False
    return bool(tokens & _READ_ONLY_SKILL_TOKENS)


def _needs_code_work(utterance: str) -> bool:
    return bool(_CODE_ARTIFACT_RE.search(utterance or "")
                or _CODE_WORD_RE.search(utterance or ""))


_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.I)
_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\"'<>]+")


def novelty_nudge_enabled() -> bool:
    return os.environ.get("WINBROW_NOVELTY_NUDGE", "1").strip().lower() not in {
        "0", "false", "off", "no",
    }


def _step_information(step: BrainStepResult) -> set[str]:
    """URLs and file paths a step contributed, used by the novelty nudge."""
    text = str(getattr(step, "output", "") or "")
    args = getattr(step, "args", {}) or {}
    found = set(_URL_RE.findall(text)) | set(_PATH_RE.findall(text))
    url = args.get("url")
    if isinstance(url, str) and url.strip():
        found.add(url.strip())
    for key in ("path", "target", "file", "folder"):
        value = args.get(key)
        if isinstance(value, str) and ("\\" in value or "/" in value):
            found.add(value.strip())
    return found


def no_new_information(history: List[BrainStepResult], window: int = 5) -> bool:
    """True when the last ``window`` successful steps added no new URL/path.

    Deliberately conservative: five reads of the same page, or five waits on
    one page, qualify. Repeated identical outputs are already caught earlier
    by ``detect_stuck``.
    """
    recent = list(history[-max(2, window):])
    if len(recent) < max(2, window):
        return False
    if not all(getattr(step, "success", False) for step in recent):
        return False
    signals: set[str] = set()
    for step in recent:
        signals |= _step_information(step)
    return len(signals) <= 1


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
            s.success and s.skill in (
                "browser_wait", "browser_read_page", "browser_read_full",
            )
            for s in history[last_upload + 1:]
        )
        if not verified:
            return (
                "HISTORY shows browser_upload_file set the file, but no "
                "browser_wait or browser_read_page/read_full followed it. "
                "Verify the upload completed before emitting done."
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
    step_id: str = ""

    def to_dict(self) -> dict:
        # The UI/trace copy used to be cut to 400 chars, which also dropped the
        # artifact paths that later turns resolve follow-ups against. Keep the
        # whole result unless an operator asks for a smaller stream.
        try:
            output_limit = max(0, int(os.environ.get(
                "WINBROW_STEP_OUTPUT_CHARS", "4000")))
        except ValueError:
            output_limit = 4000
        d = {
            "skill": self.skill, "args": self.args, "success": self.success,
            "output": self.output if output_limit == 0
            else self.output[:output_limit],
            "why": self.why, "elapsed_ms": self.elapsed_ms,
        }
        if self.pending:
            d["pending"] = self.pending
        if self.warning:
            d["warning"] = self.warning
        if self.url:
            d["url"] = self.url
        if self.title:
            d["title"] = self.title
        if self.step_id:
            d["step_id"] = self.step_id
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
    run_id: str = ""                # trace correlation, not part of the UI dict

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
        allowed_skills: Optional[Callable[[str], bool]] = None,
        system_prompt: Optional[str] = None,
        enable_skill_writer: bool = True,
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
        # Subagent scope: a callable filter on dispatcher keys, a replacement
        # system prompt, and no background skill capture. The parent brain
        # leaves all three unset.
        self.allowed_skills = allowed_skills
        self.system_prompt = system_prompt
        self.enable_skill_writer = bool(enable_skill_writer)
        # Delegation is opt-in per user turn. handle() recomputes this from the
        # actual utterance; direct _run_step tests default to False.
        self.subagents_allowed = False
        # Load after .env so WINBROW_SKILLS_DIR is honored even when the
        # server imports this module before loading environment variables.
        self.skills_dir = os.environ.get("WINBROW_SKILLS_DIR") or SKILLS_DIR
        self.skill_registry: dict[str, SkillMeta] = load_skill_registry(
            self.skills_dir
        )
        if self.allowed_skills is not None:
            self.skill_registry = {
                key: meta for key, meta in self.skill_registry.items()
                if self.allowed_skills(key)
            }
        self.skills_metadata = build_skills_metadata(self.skill_registry)
        self._tool_index = ""
        self._tool_index_key: tuple = ()

    # ------------------------------------------------------------------ tool index
    def _registry_tools(self, ctx: Any) -> list:
        """Every registered tool, including ones scoped to an inactive app."""
        all_tools = getattr(self.registry, "all_tools", None)
        if callable(all_tools):
            try:
                return list(all_tools())
            except Exception:
                pass
        try:
            return list(self.registry.available(ctx))
        except Exception:
            return []

    def _tool_catalog(self, ctx: Any) -> str:
        """Name-only index of non-skill tools, cached until the registry grows.

        MCP, native and learned tools stay discoverable without paying their
        full descriptions every turn. The chosen tool's schema is injected as
        SKILL DETAIL on the next planning turn.
        """
        tools = self._registry_tools(ctx)
        key = tuple(sorted(str(getattr(tool, "name", "")) for tool in tools))
        if key != self._tool_index_key:
            self._tool_index = build_tool_index(
                tools, exclude=set(self.skill_registry)
            )
            self._tool_index_key = key
        return self._tool_index

    def _skills_block(self, ctx: Any, utterance: str = "") -> str:
        """Legacy full name index (kept for older callers/tests)."""
        return build_tool_index(
            self._registry_tools(ctx), full_mcp_names=True
        )

    # ------------------------------------------------------- skill persistence
    def _fire_skill_writer(
        self, goal: str, history: List[BrainStepResult],
    ) -> None:
        """Start background skill analysis without delaying the task result.

        The user sees the completed answer first; the writer runs concurrently.
        Only runs with at least three successful executed steps are eligible,
        which keeps trivial commands from spending a model call.
        """
        if os.environ.get("WINBROW_SKILL_WRITER", "1").strip() == "0":
            return
        if not getattr(self, "enable_skill_writer", True):
            return
        successful = [
            step for step in history
            if step.success and not step.skill.startswith("_")
        ]
        if len(successful) < 3:
            return
        # A converted playbook already documents this workflow. Capturing the
        # same run again would create a duplicate, task-specific skill.
        if any(str(step.skill).startswith("playbook_") for step in successful):
            return
        try:
            task = asyncio.create_task(
                self._maybe_save_skill(goal, list(history))
            )
        except RuntimeError:
            # No running event loop (rare direct/CLI caller): skip silently.
            return
        _SKILL_WRITER_TASKS.add(task)
        task.add_done_callback(_SKILL_WRITER_TASKS.discard)

    async def _maybe_save_skill(
        self, goal: str, history: List[BrainStepResult],
    ) -> None:
        """Background task: analyse a completed run and persist a skill.

        Catches and logs every exception so a failed analysis can never crash
        the brain loop or surface an error to the user.
        """
        global SKILLS_METADATA
        try:
            existing = sorted(
                set(self.skill_registry)
                | {meta.name for meta in self.skill_registry.values()}
            )
            candidate = await analyse_task_for_skill(
                goal=goal,
                history=history,
                existing_skill_names=existing,
                gateway=self.gateway,
            )
            if not candidate.should_save:
                log.info(f"skill_writer: skipped — {candidate.reason}")
                return

            path = await write_skill_to_disk(candidate, self.skills_dir)
            await register_skill_in_registry(
                candidate.skill_key, self.skill_registry, self.skills_dir,
            )
            # Refresh the live cached block immediately so the next planner
            # turn can choose the new skill without restarting WinBrow.
            self.skills_metadata = build_skills_metadata(self.skill_registry)
            SKILLS_METADATA = self.skills_metadata
            log.info(
                f"skill_writer: saved '{candidate.name}' → {path}"
            )
        except Exception as exc:
            log.warning(f"skill_writer: failed silently — {exc}")

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
        a repeated failure after bounded recovery turns, provider loss
        mid-task, or the max_steps action budget. Idempotent observations may
        re-run so the planner can verify a change instead of trusting a stale
        result.

        Raises GatewayUnavailable when no provider responds on the first call
        (the orchestrator then falls back to the generator tier).
        """
        utterance = _merge_clarify_answer(utterance, session)
        self.subagents_allowed = delegation_allowed_for(utterance)
        memory_block = session.context_block()
        # Bind short follow-ups ("where is the pdf?", "open it") to the artifact
        # from the previous turn. Without this the planner sees only a terse
        # snippet and falls back to a disk-wide search.
        hint_fn = getattr(session, "reference_hint", None)
        if callable(hint_fn):
            try:
                reference_hint = hint_fn(utterance)
            except Exception as exc:
                log.debug(f"session reference hint failed (non-fatal): {exc}")
                reference_hint = ""
            if reference_hint:
                memory_block = f"{memory_block}\n\n{reference_hint}"
                log.info(
                    f"session reference hint attached ({len(reference_hint)} chars)."
                )
        # enrich with episodic hits when the user asks about the past
        if any(w in utterance.lower() for w in ("yesterday", "earlier", "last time", "before", "history", "did i")):
            hits = self.store.search_episodic(utterance, limit=5)
            if hits:
                memory_block += "\nRelevant past activity:\n" + "\n".join(
                    f"  {h.label[:120]}" for h in hits
                )

        run_id = f"br_{uuid.uuid4().hex[:10]}"
        self._active_run_id = run_id
        result = BrainResult(
            kind="action",
            provider=self.gateway.providers[0].name if self.gateway.providers else "",
            model=self.gateway.providers[0].model if self.gateway.providers else "",
            run_id=run_id,
        )
        history: List[BrainStepResult] = []
        last_failed_skill = ""
        consecutive_failures = 0
        last_plan_response = ""
        coaching = ""
        repeats: Dict[tuple, int] = {}
        read_repeats: Dict[tuple, int] = {}
        blocked_skills: set[str] = set()
        blocked_sigs: set[tuple] = set()
        loaded_playbooks: set[str] = set()
        done_challenged = False
        novelty_fired = False
        clarify_rounds = 0
        failure_recoveries = 0
        step_budget = max_steps if max_steps is not None else self.max_steps
        skill_detail: str = ""

        while len(result.steps) < step_budget:
            # The tool index is rebuilt only when the registry grows (e.g. a
            # build_tool step succeeds), so the cached system blocks stay
            # byte-identical turn to turn.
            tool_catalog = self._tool_catalog(ctx)
            system_catalog = (
                f"{self.skills_metadata}\n\n{tool_catalog}"
                if tool_catalog else self.skills_metadata
            )
            turn_memory = memory_block
            volatile_parts: List[str] = []
            if loaded_playbooks:
                volatile_parts.append(
                    "PLAYBOOKS ALREADY LOADED (do NOT call these again): "
                    + ", ".join(sorted(loaded_playbooks))
                )
            if coaching:
                volatile_parts.append(f"PLANNER FEEDBACK: {coaching}")
                coaching = ""
            blocked_names = sorted(blocked_skills) + sorted(
                {sig[0] for sig in blocked_sigs if sig and sig[0]}
            )
            if blocked_names:
                volatile_parts.append(
                    "DISABLED THIS TURN — repeating these calls cannot help "
                    "(they already succeeded or just failed with the same "
                    "result). Do NOT call: "
                    + ", ".join(blocked_names)
                    + ". Choose a DIFFERENT action that advances GOAL, or emit "
                    "done/clarify with what is still missing."
                )
            recent_why = [
                f"  Step {len(history) - len(history[-3:]) + i + 1}: {step.why}"
                for i, step in enumerate(history[-3:])
                if getattr(step, "why", "")
            ]
            if recent_why:
                volatile_parts.append("PLAN SO FAR:\n" + "\n".join(recent_why))
            remaining = step_budget - len(result.steps)
            if remaining <= self.step_warn_remaining:
                volatile_parts.append(
                    f"STEP BUDGET WARNING: {remaining} action(s) remaining. "
                    "If the task is nearly complete, finish it now. If you are "
                    "stuck or missing information, emit clarify instead of "
                    "retrying another search."
                )
            history_metrics: Dict[str, Any] = {}
            messages = prompts.build_loop_messages(
                utterance,
                format_history(
                    history,
                    full=history_full_enabled(),
                    run_id=run_id,
                    metrics=history_metrics,
                ),
                _ctx_block(ctx),
                turn_memory, "",
                tried_block=extract_tried_services(history),
                steps_remaining=remaining,
                skills_metadata=system_catalog,
                skill_detail=skill_detail,
                browser_turn=prompts.is_browser_turn(history),
                system_prompt=getattr(self, "system_prompt", None),
                volatile_block="\n".join(volatile_parts),
            )
            try:
                plan = await self._ask_plan(
                    messages, emit, history=history,
                    step_index=len(result.steps),
                    history_metrics=history_metrics,
                )
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
                    plan = await self._ask_plan(
                        messages, emit, history=history,
                        step_index=len(result.steps),
                        history_metrics=history_metrics,
                    )
                except (GatewayUnavailable, GatewayError) as e2:
                    log.warning(f"Planner error mid-task after retry ({e2}); finalizing.")
                    return self._partial_finalize(
                        result, history, last_plan_response, "planner unavailable",
                    )

            kind = str(plan.get("kind", "chat"))
            text = str(plan.get("response") or plan.get("summary")
                       or plan.get("question") or "").strip()
            log.info(f"brain turn {len(result.steps) + 1}: kind={kind}")
            _emit_thinking(plan, kind)

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
                    if result.complete:
                        self._fire_skill_writer(utterance, list(history))
                    _stream_answer(result.response)
                    return result
                result.kind = "chat"
                result.success = True
                result.response = text
                _stream_answer(result.response)
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
                if result.complete:
                    self._fire_skill_writer(utterance, list(history))
                _stream_answer(result.response)
                return result

            steps = _extract_steps(plan)
            if not steps:
                result.kind = "chat"
                result.success = True
                result.response = text or "I couldn't plan that. Try rephrasing."
                return result

            log.info(f"brain step chosen: {steps[0].get('skill', '?')}")

            # Progressive disclosure: the turn that CHOSE the skill had only
            # metadata. Load the full body now so the NEXT planning turn sees
            # SKILL DETAIL and can execute with complete instructions.
            detail = inject_skill_detail(
                plan, self.skill_registry, self.registry
            )
            if detail:
                skill_detail = detail
                log.info(
                    f"skill detail loaded for '{steps[0].get('skill', '')}' "
                    f"({len(detail)} chars)"
                )

            if text:
                last_plan_response = text

            done_sigs = {_step_signature(s.skill, s.args) for s in history if s.success}
            remaining = step_budget - len(result.steps)
            batch = steps[:remaining]
            precomputed: Dict[int, BrainStepResult] = {}
            if parallel_reads_enabled() and len(batch) > 1:
                batch_sigs = [
                    _step_signature(
                        str(step.get("skill", "")),
                        dict(step.get("args") or {}),
                    )
                    for step in batch
                ]
                if (
                    all(str(step.get("skill", "")) in PARALLEL_READ_SKILLS
                        for step in batch)
                    and len(set(batch_sigs)) == len(batch_sigs)
                    and not any(sig in done_sigs for sig in batch_sigs)
                ):
                    results = await asyncio.gather(*(
                        self._run_step(
                            str(step.get("skill", "")),
                            dict(step.get("args") or {}),
                            str(step.get("why", "")),
                            ctx=ctx,
                        )
                        for step in batch
                    ))
                    precomputed = dict(enumerate(results))
                    log.info(
                        f"Running {len(results)} read-only steps in parallel."
                    )
            batch_failure_appended = False
            for plan_index, step in enumerate(batch):
                skill = str(step.get("skill", ""))
                args = dict(step.get("args") or {})
                why = str(step.get("why", ""))
                sig = _step_signature(skill, args)

                # A small model sometimes re-emits a step that already
                # succeeded (e.g. create_directory). Re-running it wastes the
                # budget and hides the rest of the task; nudge it forward.
                # Reads/waits/file observations are exempt: after a save, edit
                # or move, re-reading is REQUIRED verification, not a repeat.
                escalate_fetch = (
                    sig in done_sigs
                    and skill == "web_fetch"
                    and _is_fetch_escalation(history, args)
                )
                repeatable = _is_repeatable_skill(skill)
                skip_reason = ""
                read_count = 0
                if (
                    (skill in blocked_skills or sig in blocked_sigs)
                    and not escalate_fetch
                ):
                    # The planner ignored the disable list. Enforce it instead
                    # of burning another step on the same failed/redundant call.
                    skip_reason = "blocked_skill"
                elif sig in done_sigs and not escalate_fetch:
                    if repeatable:
                        # Allow a bounded number of genuine "look again"
                        # calls: after a save/edit/move the state may have
                        # changed. Only an identical read repeated past the
                        # limit is treated as a loop.
                        read_count = read_repeats.get(sig, 0) + 1
                        if read_count <= READ_REPEAT_LIMIT:
                            read_repeats[sig] = read_count
                            log.info(
                                f"Planner re-running read-only step '{skill}' "
                                f"({read_count}/{READ_REPEAT_LIMIT}; "
                                "verification, not a repeat)."
                            )
                            log_event({
                                "type": "brain_repeat_read",
                                "run_id": run_id,
                                "step_index": len(result.steps),
                                "skill": skill,
                                "args_hash": _args_hash(skill, args),
                                "reason": "repeatable_observation",
                                "attempt": read_count,
                            })
                        else:
                            skip_reason = "repeatable_read_loop"
                    else:
                        skip_reason = "already_succeeded"
                if skip_reason:
                    repeats[sig] = repeats.get(sig, 0) + 1
                    count = repeats[sig]
                    if count >= REPEAT_HARD_LIMIT:
                        log.warning(
                            f"Planner repeated completed step '{skill}' "
                            f"{count} times; finalizing with a recovery report."
                        )
                        result = self._partial_finalize(
                            result, history, last_plan_response,
                            f"the planner kept choosing '{skill}' after it had "
                            f"already succeeded ({count} times); it was skipped "
                            "each time",
                        )
                        result.kind = "action"
                        result.complete = False
                        return result
                    prior = next(
                        (s for s in reversed(history)
                         if s.success and _step_signature(s.skill, s.args) == sig),
                        None,
                    )
                    prior_step = next(
                        (i + 1 for i in range(len(history) - 1, -1, -1)
                         if history[i].success
                         and _step_signature(history[i].skill, history[i].args) == sig),
                        0,
                    )
                    pointer = f"Step {prior_step}" if prior_step else "an earlier step"
                    if skill.startswith("playbook_"):
                        coaching = (
                            f"The {skill} playbook is already loaded; its "
                            "instructions are in SKILL DETAIL. Do NOT call it "
                            "again. Execute the concrete tool it names."
                        )
                    elif skill == "web_fetch":
                        coaching = (
                            f"You already fetched {_fetch_url_of(args)} successfully "
                            f"in {pointer}; its text is in HISTORY. Do NOT fetch that "
                            "URL again. Use the text you already have, fetch a "
                            "DIFFERENT source, or answer if the evidence is sufficient."
                        )
                    elif skill in blocked_skills or sig in blocked_sigs:
                        coaching = (
                            f"'{skill}' is DISABLED for this turn because its "
                            "last call already completed or failed with the same "
                            "result. Do NOT call it again. Diagnose the actual "
                            "state with a different observation, choose another "
                            "skill, or use repair_tool if the tool is broken."
                        )
                    elif repeatable:
                        coaching = (
                            f"You have already re-read '{skill}' "
                            f"{read_count - 1} time(s) in {pointer} and the "
                            "state is unchanged. Do NOT read it again. Use the "
                            "result already in HISTORY and emit the NEXT action, "
                            "or done if every part of GOAL is verified."
                        )
                    else:
                        coaching = (
                            f"'{skill}' with those exact arguments ALREADY SUCCEEDED "
                            f"in {pointer} — it is in HISTORY. Do NOT repeat it. "
                            "Emit the NEXT action needed, or done only if every part "
                            "of GOAL is complete."
                        )
                    if (
                        prior and prior.output
                        and skill != "web_fetch"
                        and not skill.startswith("playbook_")
                        and skip_reason != "repeatable_read_loop"
                    ):
                        coaching = (
                            f"'{skill}' with those exact arguments ALREADY SUCCEEDED "
                            f"in {pointer}. Its result was: {prior.output[:300]} "
                            "Do NOT repeat it; use this result and emit the NEXT action."
                        )
                    if count >= 2:
                        # Escalate: this same choice has now been rejected
                        # twice. Disable it for the next planning turn so the
                        # model must pick a different action instead of looping.
                        # Read loops block only their exact args (another file
                        # or page remains readable); writes/failures block the
                        # skill for one turn.
                        if repeatable:
                            blocked_sigs.add(sig)
                        else:
                            blocked_skills.add(skill)
                        coaching = (
                            f"STOP repeating '{skill}'. It was rejected {count} "
                            "times because repeating it cannot advance GOAL. It "
                            "is disabled for this turn. "
                            + (f"Its last result was: {prior.output[:300]}. "
                               if prior and prior.output else "")
                            + "Choose a DIFFERENT action that advances the "
                            "remaining GOAL, or emit done if every part is verified."
                        )
                    log_event({
                        "type": "brain_skip",
                        "run_id": run_id,
                        "step_index": len(result.steps),
                        "skill": skill,
                        "args_hash": _args_hash(skill, args),
                        "reason": skip_reason,
                    })
                    log.info(f"Planner repeated completed step '{skill}'; re-planning with feedback.")
                    break
                if escalate_fetch:
                    log.info(f"Allowing one larger re-fetch of {_fetch_url_of(args)} (truncated).")

                step_res = precomputed.get(plan_index)
                if step_res is None:
                    step_res = await self._run_step(skill, args, why, ctx=ctx)
                result.steps.append(step_res)
                history.append(step_res)
                log_event({
                    "type": "brain_step",
                    "run_id": run_id,
                    "step_index": len(result.steps),
                    "skill": skill,
                    "args_hash": _args_hash(skill, args),
                    "output_chars": len(step_res.output or ""),
                    "success": bool(step_res.success),
                    "elapsed_ms": step_res.elapsed_ms,
                    "timed_out": "timed out after" in (step_res.output or "").lower(),
                    "has_warning": bool(step_res.warning),
                })
                # A precomputed read-only batch has already executed every
                # call. Record the remaining results even when an earlier one
                # failed, then apply the single-step failure guard once.
                if not step_res.success and precomputed:
                    batch_failure_appended = True
                    for rest_index in range(plan_index + 1, len(batch)):
                        rest = precomputed.get(rest_index)
                        if rest is None:
                            continue
                        rest_step = batch[rest_index]
                        rest_skill = str(rest_step.get("skill", ""))
                        rest_args = dict(rest_step.get("args") or {})
                        result.steps.append(rest)
                        history.append(rest)
                        if rest.success:
                            done_sigs.add(_step_signature(rest_skill, rest_args))
                        log_event({
                            "type": "brain_step",
                            "run_id": run_id,
                            "step_index": len(result.steps),
                            "skill": rest_skill,
                            "args_hash": _args_hash(rest_skill, rest_args),
                            "output_chars": len(rest.output or ""),
                            "success": bool(rest.success),
                            "elapsed_ms": rest.elapsed_ms,
                            "timed_out": "timed out after" in (rest.output or "").lower(),
                            "has_warning": bool(rest.warning),
                            "batch_after_failure": True,
                        })
                    precomputed = {}
                if detect_stuck(history) and not batch_failure_appended:
                    if _is_repeatable_skill(skill):
                        # A repeated observation is a verification, not a
                        # loop: the state simply has not changed. Tell the
                        # planner it is confirmed and let it move on. Wording
                        # avoids "stuck"/"identical" so this does not trip the
                        # count_recent_stuck escalation.
                        note = (
                            "This observation was repeated and the result is "
                            "unchanged, so the current state is confirmed. "
                            "Proceed with the next action instead of reading again."
                        )
                        step_res.warning = (
                            f"{step_res.warning} {note}".strip()
                            if step_res.warning else note
                        )
                        log.info(
                            f"Repeat observation of '{skill}' confirmed an "
                            "unchanged state; continuing."
                        )
                    else:
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

                # A successful pip install between two failed script calls
                # resets the consecutive-failure counter. Stop when the exact
                # same step produces the exact same error twice instead of
                # letting the planner reinstall and retry forever.
                repeated_error = detect_repeated_failure(history)
                if not step_res.success and repeated_error:
                    if failure_recoveries < FAILURE_RECOVERIES:
                        failure_recoveries += 1
                        blocked_skills.add(skill)
                        coaching = (
                            f"'{skill}' failed twice with the same error: "
                            f"{repeated_error[:300]} Do NOT retry that call. "
                            "Diagnose the ACTUAL state first: list the parent "
                            "folder, get file info, or read the current path; "
                            "then continue from the path or value you find. If "
                            "the tool is genuinely broken, use repair_tool."
                        )
                        log.warning(
                            f"Brain repeated failure for '{skill}'; attempting "
                            f"recovery {failure_recoveries}/{FAILURE_RECOVERIES}."
                        )
                        log_event({
                            "type": "brain_recover",
                            "run_id": run_id,
                            "step_index": len(result.steps),
                            "skill": skill,
                            "args_hash": _args_hash(skill, args),
                            "reason": "repeated_failure",
                            "attempt": failure_recoveries,
                        })
                        break
                    log.warning(
                        f"Brain repeated failure for '{skill}'; stopping instead "
                        "of re-planning the same error."
                    )
                    result.kind = "action"
                    result.success = False
                    result.response = (
                        f"Stopped: '{skill}' failed twice with the same error. "
                        f"{repeated_error[:400]}"
                    )
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
                    if skill.startswith("playbook_"):
                        loaded_playbooks.add(skill)
                    done_sigs.add(sig)
                    consecutive_failures = 0
                    last_failed_skill = ""
                    # A success is progress: give later failures a fresh
                    # recovery budget instead of exhausting it for the run.
                    failure_recoveries = 0
                    # Only consecutive re-emissions of a completed step should
                    # trip the repeat guard. A real step in between is progress.
                    # A bounded re-read is verification, not progress: keep its
                    # counter so an endless read loop is still cut off.
                    if read_count == 0:
                        repeats.clear()
                        read_repeats.clear()
                        # Progress may make a previously-blocked step relevant
                        # again, so lift the one-turn disable list.
                        blocked_skills.clear()
                        blocked_sigs.clear()
                    if (
                        not novelty_fired
                        and novelty_nudge_enabled()
                        and no_new_information(history, 5)
                    ):
                        novelty_fired = True
                        coaching = (
                            "The last 5 steps added no new URL, file path, or "
                            "result. Stop repeating the same observation; try "
                            "a different source/approach or emit clarify/done."
                        )
                    continue

                consecutive_failures = (
                    consecutive_failures + 1 if skill == last_failed_skill else 1
                )
                last_failed_skill = skill
                if consecutive_failures >= 2:
                    if failure_recoveries < FAILURE_RECOVERIES:
                        failure_recoveries += 1
                        blocked_skills.add(skill)
                        coaching = (
                            f"'{skill}' has failed twice in a row. Do NOT emit "
                            "the same call again. Diagnose the actual state "
                            "(list the folder, get file info, read the current "
                            "path) and continue from what you find, choose a "
                            "different skill, or use repair_tool if the tool "
                            "itself is broken."
                        )
                        log.warning(
                            f"Brain consecutive failure for '{skill}'; "
                            f"attempting recovery {failure_recoveries}/"
                            f"{FAILURE_RECOVERIES}."
                        )
                        log_event({
                            "type": "brain_recover",
                            "run_id": run_id,
                            "step_index": len(result.steps),
                            "skill": skill,
                            "args_hash": _args_hash(skill, args),
                            "reason": "consecutive_failure",
                            "attempt": failure_recoveries,
                        })
                        break
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
    async def _ask_plan(self, messages: list, emit: Optional[Callable],
                        history: Optional[List[BrainStepResult]] = None,
                        step_index: int = 0,
                        history_metrics: Optional[dict] = None) -> dict:
        """One planner call → parsed JSON plan. Retries once on parse failure."""
        t0 = time.perf_counter()
        reasoning_config = get_reasoning_config(history or [])
        reasoning = (
            {"max_tokens": reasoning_config["reasoning_max"]}
            if reasoning_config.get("reasoning_max", 0) > 0
            else {"enabled": False}
        )
        for attempt in range(2):
            chat_kwargs = {
                "json_schema": prompts.PLANNER_JSON_SCHEMA,
                # Reasoning models spend max_tokens on hidden thinking first.
                # Browser turns get a tighter budget (see get_reasoning_config)
                # so the JSON plan is never starved by over-thinking.
                "temperature": planner_temperature(),
                "max_tokens": reasoning_config["max_tokens"],
                "reasoning": reasoning,
            }
            if attempt == 0:
                chat_kwargs["on_delta"] = _planner_delta
            for _attempt in range(3):
                try:
                    resp = await self.gateway.chat(messages, **chat_kwargs)
                    break
                except TypeError as exc:
                    # Older/third-party gateways may not accept the reasoning
                    # policy or the streaming callback; drop one and retry.
                    message = str(exc)
                    dropped = False
                    for key in ("on_delta", "reasoning"):
                        if key in message and key in chat_kwargs:
                            chat_kwargs.pop(key, None)
                            dropped = True
                            break
                    if not dropped:
                        raise
            else:
                raise
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

            parse_ok = bool(plan and "kind" in plan)
            prompt_chars = _prompt_chars(messages)
            input_tokens = int(getattr(resp, "input_tokens", 0) or 0)
            estimated = input_tokens == 0 and prompt_chars > 0
            if estimated:
                input_tokens = prompt_chars // 4
            hm = history_metrics or {}
            log_event({
                "type": "brain_plan",
                "run_id": getattr(self, "_active_run_id", ""),
                "step_index": step_index,
                "attempt": attempt + 1,
                "provider": getattr(resp, "provider", ""),
                "model": getattr(resp, "model", ""),
                "input_tokens": input_tokens,
                "output_tokens": int(getattr(resp, "output_tokens", 0) or 0),
                "cache_read": int(
                    getattr(resp, "cache_read_input_tokens", 0) or 0),
                "estimated": estimated,
                "finish_reason": getattr(resp, "finish_reason", ""),
                "reasoning_len": getattr(resp, "reasoning_length", 0),
                "prompt_chars": prompt_chars,
                "history_chars": int(hm.get("chars") or 0),
                "history_budget": int(hm.get("budget") or 0),
                "history_stubbed": int(hm.get("stubbed") or 0),
                "history_recent_clipped": int(
                    hm.get("recent_clipped") or 0),
                "history_overflow": bool(hm.get("overflow")),
                "plan_chars": len(getattr(resp, "content", "") or ""),
                "parse_ok": parse_ok,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
            })
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
        """Execute one step and stream tool.started/tool.result to the UI.

        The event wrapper lives here (not in each branch of the implementation)
        so every exit path — playbook ack, permission pending, early failure —
        produces a matching started/result pair.
        """
        step_id = new_step_id(skill)
        try:
            event_bus().emit(
                "tool.started",
                {
                    "skill": str(skill or ""),
                    "args": _clip_args(args),
                    "why": str(why or "")[:500],
                },
                step_id=step_id,
            )
        except Exception:
            pass
        with step_scope(step_id):
            result = await self._run_step_inner(skill, args, why, ctx)
        result.step_id = step_id
        try:
            event_bus().emit(
                "tool.result",
                {
                    "skill": result.skill,
                    "success": bool(result.success),
                    "output": str(result.output or "")[:4000],
                    "warning": str(result.warning or "")[:800],
                    "elapsed_ms": result.elapsed_ms,
                },
                step_id=step_id,
            )
        except Exception:
            pass
        return result

    async def _run_step_inner(self, skill: str, args: dict, why: str = "", ctx: Any = None) -> BrainStepResult:
        t0 = time.perf_counter()

        def _ms() -> float:
            return round((time.perf_counter() - t0) * 1000, 1)

        try:
            skill = resolve_skill_key(skill, self.skill_registry)
            allowed = getattr(self, "allowed_skills", None)
            if allowed is not None and not allowed(skill):
                return BrainStepResult(
                    skill, args, False,
                    f"Skill '{skill}' is not available to this subagent.",
                    why, _ms(),
                )
            if skill == "delegate_task" and not getattr(self, "subagents_allowed", False):
                return BrainStepResult(
                    skill, args, False,
                    "Subagents are disabled or explicit-only for this turn; do "
                    "the work in the main loop.",
                    why, _ms(),
                )
            meta = self.skill_registry.get(skill)
            if meta is not None and getattr(meta, "kind", "tool") == "playbook":
                # A playbook is guidance, not an executable tool. Its body is
                # injected as SKILL DETAIL on the next planning turn; this
                # acknowledgement closes the current step without pretending
                # the user's task is complete.
                return BrainStepResult(
                    skill, args, True,
                    "Playbook loaded. Follow its SKILL DETAIL on the next turn "
                    "and execute the concrete tools it names. This call is not "
                    "task completion.",
                    why, _ms(),
                )
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

    if skill in ("browser_navigate", "browser_read_page",
                 "browser_read_full", "browser_wait"):
        if (result.get("success")
                and not str(result.get("title") or "").strip()
                and not str(result.get("text") or "").strip()
                and not result.get("links")):
            warnings.append(
                "The page returned no title and no text, so it is probably "
                "still loading. Wait 2 seconds and read it again before "
                "clicking or typing."
            )

    if skill == "browser_get_accessibility_tree":
        if result.get("_truncated"):
            warnings.append(
                "Accessibility tree was truncated. "
                "Retry with a more specific target= value "
                "to find the element you need."
            )

    if skill == "browser_read_full":
        if result.get("_truncated_tree"):
            warnings.append(
                "Interactive element list was truncated. Retry with "
                "browser_get_accessibility_tree and a more specific target= "
                "value to find the element you need."
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
                "browser_read_full or browser_get_accessibility_tree "
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
        existing = str(result.get("_warning") or "").strip()
        if existing:
            warnings = [existing] + [w for w in warnings if w not in existing]
        result["_warning"] = " ".join(warnings)
    return result


def inject_skill_detail(
    response: Any,
    registry: dict[str, SkillMeta],
    tool_registry: Any = None,
) -> Optional[str]:
    """Return the full body for the skill chosen in ``response``.

    Handles both the dict plan produced by the loop and an object with a
    ``step``/``steps`` attribute. Folder skills use their SKILL.md body;
    MCP/native/learned tools get a synthesized schema from the registry.
    Returns None when the response is not an action or the skill is unknown.
    """
    if isinstance(response, dict):
        steps = _extract_steps(response)
    else:
        step = getattr(response, "step", None)
        steps = [step] if isinstance(step, dict) and step.get("skill") else []
        if not steps:
            raw_steps = getattr(response, "steps", None)
            if isinstance(raw_steps, list):
                steps = [s for s in raw_steps if isinstance(s, dict) and s.get("skill")]
    if not steps:
        return None
    skill = resolve_skill_key(str(steps[0].get("skill", "")), registry)
    meta = registry.get(skill)
    if meta is not None:
        body = load_skill_body(meta)
        if body:
            return body
    if tool_registry is not None:
        try:
            tool = tool_registry.get(skill)
        except Exception:
            tool = None
        if tool is not None:
            return build_tool_detail(tool)
    return None


def _fetch_url_of(args: dict) -> str:
    return str((args or {}).get("url") or "").strip()


def _is_fetch_escalation(history: List[BrainStepResult], args: dict) -> bool:
    """Allow ONE re-fetch of a truncated URL with a larger max_chars.

    Any other repeat of the same URL is redundant: the earlier text is already
    in HISTORY, so refetching only burns steps and context.
    """
    url = _fetch_url_of(args)
    if not url:
        return False
    priors = [
        step for step in history
        if step.success and step.skill == "web_fetch"
        and _fetch_url_of(step.args) == url
    ]
    if not priors or len(priors) >= 2:
        return False
    prior = priors[-1]
    if "TRUNCATED" not in (prior.output or ""):
        return False
    try:
        prior_limit = int(str(prior.args.get("max_chars") or 40000))
        new_limit = int(str(args.get("max_chars") or 40000))
    except (TypeError, ValueError):
        return False
    return new_limit > prior_limit


_ENUM_ARG_KEYS = frozenset({
    "action", "mode", "direction", "operation", "op", "type", "kind",
    "state", "align", "layout", "orientation", "unit", "format",
})


def _normalise_arg_value(name: str, value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if name.lower() in _ENUM_ARG_KEYS:
        text = text.casefold()
    return text


def _prompt_chars(messages: list) -> int:
    total = 0
    for message in messages or []:
        content = message.get("content", "") if isinstance(message, dict) else ""
        if isinstance(content, list):
            total += sum(
                len(str(block.get("text", "")))
                for block in content if isinstance(block, dict)
            )
        else:
            total += len(str(content))
    return total


def _args_hash(skill: str, args: dict) -> str:
    """Short, non-reversible identity for trace metrics (never raw output)."""
    try:
        raw = json.dumps(
            {"skill": skill, "args": args or {}},
            sort_keys=True, ensure_ascii=False, default=str,
        )
    except Exception:
        raw = str(args)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:12]


def _step_signature(skill: str, args: dict) -> tuple:
    """Stable identity for a step, used to detect repeats of completed work."""
    args = dict(args or {})
    if skill == "web_fetch":
        # The same URL is the same work regardless of max_chars; escalating a
        # truncated fetch is allowed separately by _is_fetch_escalation.
        args = {"url": _fetch_url_of(args)}
    else:
        args = {
            str(key): _normalise_arg_value(str(key), value)
            for key, value in args.items()
        }
    try:
        args_key = json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)
    except Exception:
        args_key = str(args)
    return (skill, args_key)


def _clip_args(args: Any, limit: int = 1200) -> dict:
    """Small, UI-safe copy of step args (avoid shipping whole file bodies)."""
    out: dict = {}
    for key, value in dict(args or {}).items():
        name = str(key)
        if isinstance(value, str):
            out[name] = value if len(value) <= limit else (
                value[:limit] + f"… <{len(value)} chars>")
        elif isinstance(value, (dict, list)):
            try:
                blob = json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                blob = str(value)
            out[name] = blob if len(blob) <= limit else blob[:limit] + "…"
        else:
            out[name] = value
    return out


def _emit_thinking(plan: dict, kind: str) -> None:
    """Publish a short plan summary for action turns.

    Chat/done replies already stream as the answer; using their text as a
    thinking summary showed the same paragraph twice in the trace.
    """
    try:
        if kind != "action":
            return
        steps = _extract_steps(plan)
        skill = str(steps[0].get("skill") or "") if steps else ""
        summary = str(plan.get("why") or "").strip()
        if not summary and skill:
            summary = f"Next action: {skill}"
        if not summary:
            return
        event_bus().emit("thinking.summary", {
            "kind": kind,
            "summary": summary[:1000],
            "skill": skill,
        })
    except Exception:
        pass


def _planner_delta(kind: str, text: str) -> None:
    """Forward provider reasoning chunks to the UI (never the JSON payload)."""
    if kind != "reasoning" or not text:
        return
    try:
        event_bus().emit("thinking.delta", {"text": str(text)})
    except Exception:
        pass


def _stream_answer(text: str) -> None:
    try:
        event_bus().stream_answer(str(text or ""))
    except Exception:
        pass


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


def _history_block(history: List[BrainStepResult], full: bool = False) -> str:
    """Token-bounded history; legacy compressed unless ``full`` is set."""
    return format_history(history, full=full)


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
