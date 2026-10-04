"""
Laya Typed Decision Router for WinBrow
=======================================
Equivalent to macbrow's router.py.
Uses Laya's non-autoregressive decision model to choose the exact tool and
speculative arguments in one ultra-fast request (~150-300ms).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .registry import MAX_CHOICE_OPTIONS, Tool, ToolRegistry
from .resolvers import resolve_file as _resolve_file
from . import files as _files
from .windows import WindowsContext

log = logging.getLogger("winbrow.router")

CHAT = "chat"
NEW_ACTION = "new_action"
STOP = "stop"

MIN_TOOL_CONFIDENCE = 0.35
NEW_ACTION_MIN_CONFIDENCE = 0.55


def _laya_timeout_default() -> float:
    """Laya per-request budget in seconds; override with LAYA_TIMEOUT_S."""
    try:
        return max(0.5, float(os.environ.get("LAYA_TIMEOUT_S", "5.0")))
    except ValueError:
        return 5.0


LAYA_PREDICT_TIMEOUT_S = _laya_timeout_default()


class LayaWorker:
    """Persistent warm Laya inference thread, loaded once at startup.

    Owns the single `laya.Router` instance: loads it on start, runs one
    warm-up prediction so weights are hot before real traffic, then serves
    predict() calls from a queue so concurrent requests serialize instead
    of each paying a cold load. Daemon thread: never blocks process exit.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._router: Any = None
        self._loaded = False
        self._thread = threading.Thread(target=self._serve, name="laya-worker", daemon=True)

    def start(self) -> None:
        """Load the model in the background; returns immediately."""
        self._thread.start()

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def router(self) -> Any:
        return self._router

    def _serve(self) -> None:
        try:
            from laya import Router
            self._router = Router()
        except Exception as e:
            log.warning(f"Laya could not be loaded: {e}. Fallback enabled.")
            while True:
                fut, _text, _questions = self._queue.get()
                try:
                    if not fut.cancelled():
                        fut.set_exception(RuntimeError(f"Laya unavailable: {e}"))
                except Exception:
                    pass
            return
        self._loaded = True
        log.info("Laya Decision Engine loaded successfully.")
        try:
            self._router.predict("warm up", {
                "selected_tool": {
                    "type": "choice",
                    "instructions": "Warm-up call. Pick any option.",
                    "criteria": {"a": "first option", "b": "second option"},
                }
            })
            log.info("Laya warm-up prediction complete.")
        except Exception as e:
            log.warning(f"Laya warm-up predict failed (non-fatal): {e}")
        while True:
            fut, text, questions = self._queue.get()
            if fut.cancelled():
                continue
            try:
                res = self._router.predict(text, questions)
            except Exception as e:
                try:
                    if not fut.cancelled():
                        fut.set_exception(e)
                except Exception:
                    pass
            else:
                try:
                    if not fut.cancelled():
                        fut.set_result(res)
                except Exception:
                    pass

    async def apredict(self, text: str, questions: dict[str, Any], timeout: float) -> Any:
        """Submit one prediction; raises TimeoutError / RuntimeError on failure.

        Polls instead of blocking an executor so an abandoned inference can
        never stall the event loop or process shutdown.
        """
        if not self._thread.is_alive():
            raise RuntimeError("Laya worker thread is not running")
        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._queue.put((fut, text, questions))
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            if fut.done():
                return fut.result()
            if loop.time() >= deadline:
                fut.cancel()
                raise asyncio.TimeoutError(
                    f"Laya prediction exceeded {timeout}s budget"
                )
            await asyncio.sleep(0.05)

# Leading conversational fluff (voice assistants love "can you ...") that
# must be stripped before trigger matching and argument extraction.
_CONVERSATIONAL_PREFIXES = [
    "can you ", "could you ", "will you ", "would you ",
    "please ", "hey ", "ok ", "okay ", "hi ", "hello ",
    "yeah ", "yeah, ", "yep ", "yes ", "uh ", "um ", "er ",
    "i want you to ", "i'd like you to ", "i need you to ",
    "do me a favor ", "do me a favour ",
]


def _strip_conversational_prefix(text: str) -> str:
    """Remove leading conversational fluff: 'can you open X' -> 'open X'."""
    cleaned = text.strip()
    changed = True
    while changed:
        changed = False
        lowered = cleaned.lower()
        for p in _CONVERSATIONAL_PREFIXES:
            if lowered.startswith(p):
                cleaned = cleaned[len(p):].strip()
                changed = True
                break
    return cleaned


def _download_first_roots() -> list[str]:
    """User-folder search roots with Downloads first.

    A bare filename ("open leoantony.png file") is interpreted as "in the
    downloads folder" first, then the other user folders.
    """
    ordered: list[str] = []
    for nick in ("downloads", "desktop", "documents", "pictures",
                 "videos", "music", "home"):
        path = _files.known_folder_paths().get(nick)
        if path and path not in ordered:
            ordered.append(path)
    return ordered


def _folder_exists_on_disk(name: str) -> Optional[str]:
    """Fast pure-Python check for a top-level directory match.

    Scans fixed-drive roots (two levels) and user folders (one level) with
    os.scandir — milliseconds, no PowerShell spawn. Returns the full path of
    the first exact (case-insensitive) match, else None.
    """
    n = name.strip().strip('"').strip("'")
    if not n or len(n) > 60 or re.search(r'[<>:"|?*]', n):
        return None
    if os.path.isdir(n):
        return os.path.abspath(n)
    roots: list[str] = []
    try:
        home = os.path.expanduser("~")
        roots += [
            os.path.join(home, "Desktop"),
            os.path.join(home, "Documents"),
            os.path.join(home, "Downloads"),
            home,
        ]
        for letter_ord in range(ord("C"), ord("G")):
            drv = f"{chr(letter_ord)}:\\"
            if os.path.isdir(drv):
                roots.append(drv)
    except Exception:
        pass
    low = n.lower()
    second: list[str] = []
    for root in roots:
        try:
            with os.scandir(root) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.lower() == low:
                            return entry.path
                        second.append(entry.path)
        except Exception:
            continue
    for parent in second[:400]:
        try:
            with os.scandir(parent) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False) and entry.name.lower() == low:
                        return entry.path
        except Exception:
            continue
    return None


@dataclass
class Route:
    kind: str  # "tool" | "chat" | "new_action" | "stop" | "uncertain"
    tool: Optional[Tool] = None
    args: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    probabilities: dict[str, float] = field(default_factory=dict)
    latency_ms: float = 0.0
    reason: str = ""
    tier: str = ""  # "heuristic" | "laya" (M0 instrumentation)

    @property
    def tool_name(self) -> Optional[str]:
        return self.tool.name if self.tool else None

    @property
    def summary(self) -> str:
        if self.tool:
            arg_str = ", ".join(f"{k}={v!r}" for k, v in self.args.items())
            return f"{self.tool.name}({arg_str})"
        return self.kind


class WinBrowRouter:
    """Orchestrates Laya's typed decision engine to select tools and parameters."""

    def __init__(self, registry: ToolRegistry, timeout: Optional[float] = None):
        self.registry = registry
        # Read env dynamically (not the import-time constant) so tests and
        # host config can override LAYA_TIMEOUT_S per process start.
        self.timeout = timeout if timeout is not None else _laya_timeout_default()
        self._worker = LayaWorker()
        self._worker.start()

    @property
    def _loaded(self) -> bool:
        """Whether the decision model is ready (kept for /api/system)."""
        return self._worker.loaded

    @property
    def _router(self) -> Any:
        """The underlying laya Router, or None while loading/unavailable."""
        return self._worker.router

    async def route(self, utterance: str, ctx: WindowsContext, memory=None) -> Route:
        """
        Route a user utterance. Laya (the decision model) chooses first;
        exact-intent heuristics are only a fallback when Laya errors,
        times out, or is unavailable.
        """
        t0 = time.perf_counter()
        text = utterance.strip()
        if not text:
            return Route(kind=CHAT, latency_ms=0, confidence=1.0)

        tools = self.registry.available(ctx)

        # 1. Laya chooses the tool (and enum args) first.
        try:
            questions = self._build_questions(text, tools, ctx)
            result = await self._worker.apredict(text, questions, self.timeout)
            return self._route_from_answers(result, text, t0)
        except asyncio.TimeoutError:
            log.warning(f"Laya routing timed out after {self.timeout}s; using exact-intent fallback.")
        except Exception as e:
            log.warning(f"Laya routing failed ({e}); using exact-intent fallback.")

        # 2. Fallback: only intents that must never fail.
        return self._fallback_route(text, tools, t0, ctx, memory=memory)

    def _build_questions(self, text: str, tools: list[Tool], ctx: WindowsContext) -> dict[str, Any]:
        """Build the Laya tool-choice question over the real registry options."""
        criteria: dict[str, str] = {}
        for t in tools[:MAX_CHOICE_OPTIONS - 3]:
            ex = f" (e.g. {', '.join(t.examples[:2])})" if t.examples else ""
            criteria[t.name] = f"{t.description}{ex}"

        criteria[CHAT] = "General conversation, greetings, asking what you can do, or questions not involving desktop action."
        criteria[NEW_ACTION] = "The user wants an action or complex Windows automation that none of the listed tools can do."
        criteria[STOP] = "Stop the assistant, cancel listening, or close WinBrow."

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

        for t in tools:
            for arg in t.args:
                if arg.kind == "enum" and arg.criteria:
                    questions[f"arg__{t.name}__{arg.name}"] = {
                        "type": "choice",
                        "instructions": arg.instructions,
                        "criteria": arg.criteria,
                    }
        return questions

    def _route_from_answers(self, result: dict[str, Any], text: str, t0: float) -> Route:
        """Validate the model's answer and turn it into a Route.

        Code only validates: unknown tool names become NEW_ACTION, and text
        arguments are extracted from the utterance (the model answers choice
        questions only, so it cannot supply free text itself).
        """
        answers = result.get("answers", {})

        tool_choice = answers.get("selected_tool", {}).get("choice", NEW_ACTION)
        conf = answers.get("selected_tool", {}).get("confidence", 0.0)
        probs = answers.get("selected_tool", {}).get("probabilities", {})

        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

        if tool_choice == CHAT:
            return Route(kind=CHAT, confidence=conf, probabilities=probs, latency_ms=elapsed_ms, tier="laya")
        elif tool_choice == STOP:
            return Route(kind=STOP, confidence=conf, probabilities=probs, latency_ms=elapsed_ms, tier="laya")
        elif tool_choice == NEW_ACTION or conf < MIN_TOOL_CONFIDENCE:
            return Route(
                kind=NEW_ACTION,
                confidence=conf,
                probabilities=probs,
                latency_ms=elapsed_ms,
                reason="No existing tool matches with high confidence",
                tier="laya",
            )

        matched_tool = self.registry.get(tool_choice)
        if not matched_tool:
            return Route(kind=NEW_ACTION, confidence=conf, latency_ms=elapsed_ms, tier="laya")

        args: dict[str, str] = {}
        for arg in matched_tool.args:
            if arg.kind == "enum":
                qid = f"arg__{matched_tool.name}__{arg.name}"
                ans = answers.get(qid, {})
                val = ans.get("choice", arg.default)
                if val:
                    args[arg.name] = val
            else:
                args[arg.name] = self._extract_text_arg(text, matched_tool.name, arg.name)

        return Route(
            kind="tool",
            tool=matched_tool,
            args=args,
            confidence=conf,
            probabilities=probs,
            latency_ms=elapsed_ms,
            tier="laya",
        )

    def _extract_text_arg(self, utterance: str, tool_name: str, arg_name: str) -> str:
        text = _strip_conversational_prefix(utterance.strip())
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
        elif tool_name == "volume_set_level":
            # Keyword-first parsing with word boundaries: bare "0" must not
            # match inside "20"/"30", and "max" must win over digits.
            if re.search(r"\b(max|maximum|full|all the way up|full blast)\b", lower) or "100%" in lower:
                return "100"
            if re.search(r"\b(min|minimum|zero|off|mute|muted|silence|silent)\b", lower):
                return "0"
            m = re.search(r"(\d{1,3})\s*%?", lower)
            if m:
                return str(max(0, min(100, int(m.group(1)))))
            return text
        elif tool_name == "open_folder":
            for prefix in ["open folder ", "open the folder ", "open ", "open the ", "go to folder ", "go to ", "show folder ", "show "]:
                if lower.startswith(prefix):
                    clean = text[len(prefix):].strip().strip('"').strip("'").strip()
                    clean = re.sub(r"\s+(please|thanks|thank\s+you)\s*$", "", clean, flags=re.IGNORECASE).strip()
                    if clean.lower().endswith(" folder"):
                        clean = clean[:-7].strip().strip('"').strip("'").strip()
                    if clean.lower().startswith("my "):
                        clean = clean[3:].strip()
                    if clean.lower().startswith("the "):
                        clean = clean[4:].strip()
                    return clean
            return text
        elif tool_name == "open_file":
            # Fill the text args the model cannot supply itself: strip the
            # command verb, an "in <app>" qualifier (returned as `app`), and
            # location/format qualifiers from the target filename.
            work = lower
            if arg_name == "app":
                m_app = re.search(r"\bin\s+(chrome|edge|browser)\b", work)
                if m_app:
                    return "edge" if m_app.group(1) == "edge" else "chrome"
                return ""
            work = re.sub(r"\bin\s+(chrome|edge|browser)\b", " ", work).strip()
            m = re.search(r"\bopen\s+(?:the\s+|my\s+)?(?:file\s+(?:named\s+|called\s+)?)?(.+)", work)
            tgt = m.group(1).strip().strip('"').strip("'").strip() if m else work
            tgt = re.sub(r"\s+(for\s+me|please)$", "", tgt).strip()
            tgt = re.sub(r"\s+(from|in)\s+(the\s+|my\s+)?(downloads?|documents?|desktop|pictures?)(?:\s+folder)?\s*$", "", tgt).strip()
            tgt = re.sub(r"\s+file\s*$", "", tgt).strip()
            tgt = re.sub(r"\s+(pdf|docx?|xlsx?|pptx?|txt|csv|jpe?g|png|gif|bmp|mp3|mp4|mkv|avi|zip)$", r".\1", tgt).strip()
            if tgt in ("that file", "this file", "that", "this", "it", ""):
                return ""
            return tgt
        return text

    def _resolve_open_file(self, lower: str, memory=None) -> Optional[tuple[str, str]]:
        """Filename-like token validated against disk, Downloads first.

        Returns (target, app) ONLY when the file exists; otherwise None.
        Word order and filler do not matter: the decision is driven by what
        is actually on disk, so "open leoantony.png file" is treated as
        "open the leoantony.png file in the downloads folder".
        """
        app = ""
        m_app = re.search(r"\bin\s+(chrome|edge|browser)\b", lower)
        if m_app:
            app = "edge" if m_app.group(1) == "edge" else "chrome"
        # "that file" / "this file" → last opened file from memory.
        if re.search(r"\b(that|this)\s+file\b", lower) and not re.search(
                r"[A-Za-z0-9_][\w\-.]*\.[A-Za-z0-9]{2,4}", lower):
            if memory is not None:
                ref = memory.resolve_reference(lower)
                if ref and os.path.isfile(ref):
                    return (os.path.basename(ref), app)
            return None
        candidates = re.findall(r"[A-Za-z0-9_][\w\-.]*\.[A-Za-z0-9]{2,4}", lower)
        roots = _download_first_roots()
        for cand in candidates:
            name = cand.strip().strip("\"'.,;:!?()[]")
            if not name or "explorer" in name.lower():
                continue
            found = _resolve_file(name, search_roots=roots)
            if not found:
                continue
            # An explicit extension must match exactly: "leoantony.png"
            # may not open "Leoantony.pdf".
            if "." in os.path.basename(name):
                if os.path.basename(found).lower() != name.lower():
                    continue
            return (os.path.basename(found), app)
        return None

    def _fallback_route(self, text: str, tools: list[Tool], t0: float, ctx: WindowsContext, memory=None) -> Route:
        """Exact-intent fallback, used ONLY when Laya errors, times out, or is unavailable.

        Covers the intents that must never fail: volume level, mute, lock
        screen, open_folder gated on a real directory on disk, and open_file
        gated on a real file on disk (Downloads searched first). Anything
        else returns NEW_ACTION so the generator tier can try. This method
        never maps phrases to tools beyond these exact intents.
        """
        _ = tools  # tool choice belongs to Laya; the fallback picks no tools by keywords.
        _ = ctx
        cleaned = _strip_conversational_prefix(text.strip())
        lower = cleaned.lower()
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

        # Volume level (exact): max/min keywords or an explicit number.
        if ("setting" not in lower and any(w in lower for w in ["volume", "volueme", "voluem", "voume", "vol ", "vol.", "mute", "unmute", "audio", "sound"])):
            if re.search(r"\b(max|maximum|full|all the way up|full blast)\b", lower) or "100%" in lower:
                t = self.registry.get("volume_set_level")
                return Route(kind="tool", tool=t, args={"level": "100"}, confidence=0.98, latency_ms=elapsed_ms, tier="heuristic")
            if re.search(r"\b(min|minimum|zero)\b", lower):
                t = self.registry.get("volume_set_level")
                return Route(kind="tool", tool=t, args={"level": "0"}, confidence=0.98, latency_ms=elapsed_ms, tier="heuristic")

            level_match = re.search(r"(?:volume|volueme|voluem|voume|vol|sound|audio)\s*(?:to\s*|at\s*|=\s*)?(\d{1,3})\s*%?", lower)
            if level_match:
                lvl = str(max(0, min(100, int(level_match.group(1)))))
                t = self.registry.get("volume_set_level")
                return Route(kind="tool", tool=t, args={"level": lvl}, confidence=0.95, latency_ms=elapsed_ms, tier="heuristic")

            if "unmute" in lower:
                t = self.registry.get("system_volume")
                return Route(kind="tool", tool=t, args={"action": "unmute"}, confidence=0.9, latency_ms=elapsed_ms, tier="heuristic")
            if re.search(r"\bmute\b", lower):
                t = self.registry.get("system_volume")
                return Route(kind="tool", tool=t, args={"action": "mute"}, confidence=0.9, latency_ms=elapsed_ms, tier="heuristic")

        # Lock screen (exact).
        if "lock" in lower and any(w in lower for w in ["screen", "computer", "pc", "workstation"]):
            t = self.registry.get("lock_screen")
            return Route(kind="tool", tool=t, args={}, confidence=0.95, latency_ms=elapsed_ms, tier="heuristic")

        # Open folder — ONLY when the name resolves to a real directory.
        # No "open anything" fallback: unresolved names go to NEW_ACTION.
        folder_arg = self._extract_text_arg(text, "open_folder", "folder")
        if folder_arg:
            known_system = {"downloads", "documents", "desktop", "pictures", "music",
                            "videos", "home", "appdata", "temp", "startup", "recycle"}
            if folder_arg.lower() in known_system or _folder_exists_on_disk(folder_arg):
                t = self.registry.get("open_folder")
                if t:
                    return Route(kind="tool", tool=t, args={"folder": folder_arg}, confidence=0.92, latency_ms=elapsed_ms, tier="heuristic")

        # Open a file by name — ONLY when it resolves to a real file on
        # disk (Downloads searched first). Unresolvable names go to
        # NEW_ACTION; never guess.
        resolved = self._resolve_open_file(lower, memory=memory)
        if resolved:
            target, app = resolved
            t = self.registry.get("open_file")
            if t:
                return Route(kind="tool", tool=t, args={"target": target, "app": app}, confidence=0.9, latency_ms=elapsed_ms, tier="heuristic")

        return Route(kind=NEW_ACTION, confidence=0.5, latency_ms=elapsed_ms, tier="heuristic")

