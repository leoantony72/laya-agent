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
from . import small_llm
from .windows import WindowsContext

log = logging.getLogger("winbrow.router")

CHAT = "chat"
NEW_ACTION = "new_action"
STOP = "stop"

MIN_TOOL_CONFIDENCE = 0.35
NEW_ACTION_MIN_CONFIDENCE = 0.55

# Nicknames the bundled folder tools understand. Kept in one place so the
# open/close fallback branches agree on what a "folder" can be.
_SYSTEM_FOLDERS = frozenset({
    "downloads", "documents", "desktop", "pictures", "music",
    "videos", "home", "appdata", "temp", "startup", "recycle",
})


def _laya_timeout_default() -> float:
    """Laya per-request budget in seconds; override with LAYA_TIMEOUT_S."""
    try:
        return max(0.5, float(os.environ.get("LAYA_TIMEOUT_S", "2.0")))
    except ValueError:
        return 2.0


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


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            ins, delete, sub = cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb)
            cur.append(min(ins, delete, sub))
        prev = cur
    return prev[-1]


_COMMAND_VOCAB = frozenset({
    "search", "google", "browse", "open", "close", "launch", "start", "find",
    "look", "volume", "mute", "unmute", "lock", "screen", "folder", "file",
    "downloads", "documents", "desktop", "pictures", "settings", "browser",
    "chrome", "edge", "firefox", "tab", "tabs", "web", "online", "internet",
    "phone", "phones", "screenshot", "reload", "scroll", "incognito",
    "calculator", "notepad", "paint", "terminal", "explorer", "time",
    "dark", "light", "mode", "theme", "please", "the", "for", "in", "on",
})

_SEARCH_VERBS = frozenset({"search", "google", "browse", "lookup"})
_WEB_LOCS = frozenset({
    "web", "online", "internet", "google", "browser", "chrome", "edge",
    "firefox", "bing",
})
_LOCAL_FIND = frozenset({
    "file", "files", "folder", "folders", "disk", "desktop", "downloads",
    "documents", "page", "window",
})
_COMMON_APPS = {
    "calculator": "calculator", "calc": "calculator",
    "paint": "paint", "mspaint": "paint",
    "snipping": "snip", "snip": "snip",
    "terminal": "terminal", "powershell": "terminal",
    "cmd": "cmd", "command": "cmd",
    "notepad": "notepad",
    "wordpad": "wordpad",
    "control": "control",
    "taskmgr": "taskmgr", "task": "taskmgr",
    "settings": "settings",
    "explorer": "explorer",
    "magnifier": "magnifier",
    "charmap": "charmap",
    "regedit": "regedit",
    "msconfig": "msconfig",
}
_LAUNCH_APPS = {
    "chrome": "chrome", "google chrome": "chrome",
    "edge": "msedge", "microsoft edge": "msedge", "msedge": "msedge",
    "firefox": "firefox",
    "spotify": "spotify", "discord": "discord", "slack": "slack",
    "code": "code", "vscode": "code", "vs code": "code",
    "notepad": "notepad", "explorer": "explorer",
}


def _closest_word(word: str, vocab: set[str] | frozenset[str]) -> Optional[str]:
    w = word.lower().strip(".,!?;:\"'()")
    if not w or w.isdigit() or any(ch.isdigit() for ch in w) or "_" in w:
        return None
    if w in vocab:
        return w
    budget = 1 if len(w) <= 4 else 2
    best, best_d = None, budget + 1
    for v in vocab:
        if abs(len(v) - len(w)) > budget:
            continue
        d = _levenshtein(w, v)
        if d < best_d:
            best, best_d = v, d
            if d == 0:
                break
    return best if best is not None and best_d <= budget else None


def autocorrect_utterance(text: str, extra: Optional[list[str]] = None) -> str:
    """Repair STT/typo tokens against command vocabulary. Never touches paths."""
    vocab = set(_COMMAND_VOCAB)
    for item in extra or []:
        for tok in re.split(r"\W+", item.lower()):
            if len(tok) >= 3:
                vocab.add(tok)
    parts = text.split()
    out: list[str] = []
    for part in parts:
        stripped = part.strip(".,!?;:\"'")
        if not stripped:
            out.append(part)
            continue
        hit = _closest_word(stripped, vocab)
        if hit:
            out.append(hit)
            continue
        if len(stripped) >= 8:
            merged = None
            for i in range(3, len(stripped) - 2):
                a, b = stripped[:i], stripped[i:]
                ca, cb = _closest_word(a, vocab), _closest_word(b, vocab)
                if ca and cb:
                    merged = f"{ca} {cb}"
                    break
            if merged:
                out.append(merged)
                continue
        out.append(part)
    return " ".join(out)


def _tokens(lower: str) -> list[str]:
    return [t for t in re.split(r"\W+", lower) if t]


def _fuzzy_in(tokens: list[str], vocab: set[str] | frozenset[str]) -> bool:
    for t in tokens:
        if _closest_word(t, vocab):
            return True
    return False


def looks_like_web_search(text: str) -> bool:
    """True for search/browse-the-web phrasing, including typos."""
    lower = _strip_conversational_prefix(text).lower()
    tokens = _tokens(lower)
    if re.search(r"https?://|\b[\w\-]+\.(com|org|net|io|dev|ai|gov|edu)\b", lower):
        if any(k in lower for k in ("open", "go to", "navigate", "visit", "browse")):
            return True
    has_verb = (
        any(lower.startswith(p) for p in (
            "search ", "search for ", "google ", "look up ", "find ",
            "browse ", "look for ",
        ))
        or _fuzzy_in(tokens, _SEARCH_VERBS)
        or "look up" in lower
        or "look for" in lower
    )
    has_loc = (
        _fuzzy_in(tokens, _WEB_LOCS)
        or "in the web" in lower
        or "on the web" in lower
        or "in web" in lower
        or "on web" in lower
        or "on google" in lower
    )
    if has_verb and any(t in _LOCAL_FIND for t in tokens) and not has_loc:
        return False
    if has_verb and (has_loc or any(lower.startswith(p) for p in (
        "search", "google", "look up", "look for", "browse",
    ))):
        return True
    if has_loc and has_verb:
        return True
    return False


def web_search_query(text: str) -> str:
    q = _strip_conversational_prefix(text.strip())
    for p in (
        "search google for ", "search the web for ", "search web for ",
        "search for ", "search ", "google for ", "google ",
        "find results for ", "look up ", "look for ", "find ",
        "browse for ", "browse ", "open ",
    ):
        if q.lower().startswith(p):
            q = q[len(p):].strip()
            break
    q = re.sub(r"\s+(in|on)\s+(the\s+)?web\b.*$", "", q, flags=re.IGNORECASE).strip()
    q = re.sub(r"\s+online\s*$", "", q, flags=re.IGNORECASE).strip()
    q = re.sub(r"\s+(using|via|with)\s+google\s*$", "", q, flags=re.IGNORECASE).strip()
    q = re.sub(r"^(the|a|an)\s+", "", q, flags=re.IGNORECASE).strip()
    return q or text.strip()


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
        """Heuristic-first routing; Laya and a constrained LLM only on misses."""
        t0 = time.perf_counter()
        text = utterance.strip()
        if not text:
            return Route(kind=CHAT, latency_ms=0, confidence=1.0)

        tools = self.registry.available(ctx)
        extra = list(ctx.running_apps or []) + ([ctx.active_app] if ctx.active_app else [])
        
        # Apply typo repair and prefix stripping FIRST, before any routing
        stripped = _strip_conversational_prefix(text)
        corrected = autocorrect_utterance(stripped, extra)
        if corrected.lower() != stripped.lower():
            log.info(f"Normalized '{text}' → '{corrected}'")

        # 1. Comprehensive heuristic routing (typo-repaired, fuzzy matching).
        # This runs FIRST and handles the majority of common commands.
        heuristic_route = self._heuristic_route(corrected, tools, t0, ctx, memory=memory)
        if heuristic_route.kind != NEW_ACTION and heuristic_route.confidence >= 0.8:
            return heuristic_route

        # 2. Laya on remaining cases (small tool-only question).
        # Only runs if heuristics didn't confidently match.
        try:
            questions = self._build_questions(corrected, tools, ctx)
            result = await self._worker.apredict(corrected, questions, self.timeout)
            routed = self._route_from_answers(result, corrected, t0)
            if routed.kind != NEW_ACTION:
                return routed
            # If Laya says NEW_ACTION but heuristics had a match, prefer heuristics
            if routed.kind == NEW_ACTION and heuristic_route.kind != NEW_ACTION:
                return heuristic_route
        except asyncio.TimeoutError:
            log.warning(f"Laya routing timed out after {self.timeout}s; using heuristic fallback.")
            if heuristic_route.kind != NEW_ACTION:
                return heuristic_route
        except Exception as e:
            log.warning(f"Laya routing failed ({e}); using heuristic fallback.")
            if heuristic_route.kind != NEW_ACTION:
                return heuristic_route

        # 3. Opt-in constrained local LLM for paraphrases.
        try:
            llm_route = await self._route_with_llm(corrected, tools, ctx, t0)
            if llm_route is not None and llm_route.kind != NEW_ACTION:
                return llm_route
            if llm_route is not None:
                return llm_route
        except Exception as e:
            log.warning(f"Small-LLM routing failed ({e}); using heuristic result.")
        return heuristic_route

    async def _route_with_llm(
        self, text: str, tools: list[Tool], ctx: WindowsContext, t0: float
    ) -> Optional[Route]:
        """Constrained small-model routing. None = fall through to NEW_ACTION."""
        choice = await small_llm.route_with_llm(
            text, tools,
            context_line=f"{ctx.active_title} ({ctx.active_app})",
        )
        if choice is None:
            return None
        name, args, conf = choice
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        if name == CHAT:
            return Route(kind=CHAT, confidence=conf, latency_ms=elapsed_ms, tier="llm")
        if name == STOP:
            return Route(kind=STOP, confidence=conf, latency_ms=elapsed_ms, tier="llm")
        if name == NEW_ACTION or conf < MIN_TOOL_CONFIDENCE:
            return Route(kind=NEW_ACTION, confidence=conf, latency_ms=elapsed_ms,
                         reason="Small model found no matching tool", tier="llm")
        tool = self.registry.get(name)
        if tool is None:
            return None
        return Route(kind="tool", tool=tool, args=args, confidence=conf,
                     probabilities={name: conf}, latency_ms=elapsed_ms, tier="llm")

    def _build_questions(self, text: str, tools: list[Tool], ctx: WindowsContext) -> dict[str, Any]:
        """Tool-only Laya question (enum args are filled from the utterance)."""
        criteria: dict[str, str] = {}
        for t in tools[: min(40, MAX_CHOICE_OPTIONS - 3)]:
            ex = f" (e.g. {', '.join(t.examples[:2])})" if t.examples else ""
            criteria[t.name] = f"{t.description}{ex}"

        criteria[CHAT] = "General conversation, greetings, asking what you can do, or questions not involving desktop action."
        criteria[NEW_ACTION] = "The user wants an action or complex Windows automation that none of the listed tools can do."
        criteria[STOP] = "Stop the assistant, cancel listening, or close WinBrow."

        return {
            "selected_tool": {
                "type": "choice",
                "instructions": (
                    f"The user said '{text}'. The frontmost window is '{ctx.active_title}' ({ctx.active_app}). "
                    f"Which tool best fulfils the request? Prefer an app-scoped tool when focused."
                ),
                "criteria": criteria,
            }
        }

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
                val = ans.get("choice") or self._enum_from_utterance(text, arg) or arg.default
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
            return web_search_query(text)
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

    def _enum_from_utterance(self, utterance: str, arg) -> Optional[str]:
        lower = utterance.lower()
        best, best_score = None, 0
        for key, desc in (arg.criteria or {}).items():
            blob = f"{key} {desc}".lower()
            score = sum(1 for tok in _tokens(blob) if tok in lower and len(tok) > 2)
            if key in lower:
                score += 3
            if score > best_score:
                best, best_score = key, score
        return best if best_score > 0 else None

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

    def _heuristic_route(self, text: str, tools: list[Tool], t0: float, ctx: WindowsContext, memory=None) -> Route:
        """
        Comprehensive heuristic-first routing with fuzzy/typo-tolerant matching.
        Runs BEFORE Laya and handles the vast majority of common commands.
        """
        _ = tools  # We pick tools directly by intent, not by keyword matching against tool list
        _ = ctx
        cleaned = _strip_conversational_prefix(text.strip())
        lower = cleaned.lower()
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

        # --- VOLUME (exact + fuzzy) ---
        if any(w in lower for w in ["volume", "volueme", "voluem", "voume", "vol ", "vol.", "mute", "unmute", "audio", "sound"]):
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

        # --- LOCK SCREEN ---
        if "lock" in lower and any(w in lower for w in ["screen", "computer", "pc", "workstation"]):
            t = self.registry.get("lock_screen")
            return Route(kind="tool", tool=t, args={}, confidence=0.95, latency_ms=elapsed_ms, tier="heuristic")

        # --- OPEN FOLDER (real directories only) ---
        folder_arg = self._extract_text_arg(text, "open_folder", "folder")
        if folder_arg:
            if folder_arg.lower() in _SYSTEM_FOLDERS or _folder_exists_on_disk(folder_arg):
                t = self.registry.get("open_folder")
                if t:
                    return Route(kind="tool", tool=t, args={"folder": folder_arg}, confidence=0.92, latency_ms=elapsed_ms, tier="heuristic")

        # --- OPEN FILE (real files only, Downloads first) ---
        resolved = self._resolve_open_file(lower, memory=memory)
        if resolved:
            target, app = resolved
            t = self.registry.get("open_file")
            if t:
                return Route(kind="tool", tool=t, args={"target": target, "app": app}, confidence=0.9, latency_ms=elapsed_ms, tier="heuristic")

        # --- CLOSE FOLDER WINDOW ---
        if re.search(r"\bclose\b", lower):
            hit = None
            hit_pos = len(lower) + 1
            for nick in _SYSTEM_FOLDERS:
                pos = lower.find(nick)
                if 0 <= pos < hit_pos:
                    hit, hit_pos = nick, pos
            if hit and _folder_exists_on_disk(hit):
                t = self.registry.get("close_folder_window")
                if t:
                    return Route(kind="tool", tool=t, args={"folder": hit}, confidence=0.9, latency_ms=elapsed_ms, tier="heuristic")

        # --- WEB SEARCH (fuzzy: verbs + locations, edit distance 1-2 on tokens) ---
        # Verbs: search, google, look up, find, browse
        # Locations: web, online, internet, google, browser, chrome, edge, firefox, bing
        search_verbs = frozenset({"search", "google", "browse", "lookup", "look up", "find"})
        web_locations = frozenset({"web", "online", "internet", "google", "browser", "chrome", "edge", "firefox", "bing"})
        
        tokens = _tokens(lower)
        has_search_verb = _fuzzy_in(tokens, search_verbs) or "look up" in lower or "look for" in lower
        has_web_location = _fuzzy_in(tokens, web_locations) or "in the web" in lower or "on the web" in lower or "in web" in lower or "on web" in lower
        
        # Also check if it starts with search-like prefix (after typo repair)
        starts_with_search = any(lower.startswith(p) for p in ("search ", "search for ", "google ", "look up ", "look for ", "find ", "browse ", "browse for "))
        
        is_web_search = (has_search_verb and has_web_location) or starts_with_search
        
        # Exclude local file searches
        if is_web_search and not any(t in _LOCAL_FIND for t in tokens):
            query = web_search_query(cleaned)
            if query:
                t = self.registry.get("web_search")
                if t:
                    return Route(kind="tool", tool=t, args={"query": query}, confidence=0.92, latency_ms=elapsed_ms, tier="heuristic")

        # --- APP LAUNCH / FOCUS (fuzzy matching against known apps) ---
        # Check for open/launch/switch/focus/start + app name
        launch_prefixes = ["open ", "launch ", "switch to ", "focus ", "bring up ", "start "]
        for prefix in launch_prefixes:
            if lower.startswith(prefix):
                remainder = lower[len(prefix):].strip()
                # Fuzzy match against known apps
                for app_key, app_exe in _LAUNCH_APPS.items():
                    if _closest_word(app_key, {remainder}) or remainder.startswith(app_key):
                        t = self.registry.get("app_focus_or_launch")
                        if t:
                            return Route(kind="tool", tool=t, args={"app_name": app_exe}, confidence=0.9, latency_ms=elapsed_ms, tier="heuristic")
                # Also check common apps (calculator, notepad, etc.)
                for app_key, app_exe in _COMMON_APPS.items():
                    if _closest_word(app_key, {remainder}) or remainder.startswith(app_key):
                        t = self.registry.get("open_common_app")
                        if t:
                            return Route(kind="tool", tool=t, args={"app": app_exe}, confidence=0.9, latency_ms=elapsed_ms, tier="heuristic")
                # Fallback: try app_focus_or_launch with the raw remainder
                t = self.registry.get("app_focus_or_launch")
                if t:
                    return Route(kind="tool", tool=t, args={"app_name": remainder}, confidence=0.75, latency_ms=elapsed_ms, tier="heuristic")

        # --- WINDOW ACTIONS ---
        if any(w in lower for w in ["minimize", "maximize", "snap left", "snap right", "show desktop", "close window"]):
            action_map = {
                "minimize": "minimize",
                "maximize": "maximize", 
                "snap left": "snap_left",
                "snap right": "snap_right",
                "show desktop": "desktop",
                "close window": "close",
            }
            for kw, action in action_map.items():
                if kw in lower:
                    t = self.registry.get("window_action")
                    if t:
                        return Route(kind="tool", tool=t, args={"action": action}, confidence=0.9, latency_ms=elapsed_ms, tier="heuristic")

        # --- SYSTEM SETTINGS (sound, network, bluetooth, display) ---
        settings_keywords = {
            "sound": "sound",
            "audio": "sound",
            "wifi": "network", "wi-fi": "network", "internet": "network",
            "bluetooth": "bluetooth",
            "display": "display", "monitor": "display", "screen": "display",
            "battery": "batterysaver",
            "apps": "appsfeatures", "programs": "appsfeatures",
        }
        if "settings" in lower or "open settings" in lower:
            for kw, pane in settings_keywords.items():
                if kw in lower:
                    t = self.registry.get("windows_settings")
                    if t:
                        return Route(kind="tool", tool=t, args={"pane": pane}, confidence=0.88, latency_ms=elapsed_ms, tier="heuristic")
            # Generic settings
            t = self.registry.get("windows_settings")
            if t:
                return Route(kind="tool", tool=t, args={"pane": "about"}, confidence=0.7, latency_ms=elapsed_ms, tier="heuristic")

        # --- SCREENSHOT ---
        if "screenshot" in lower or "screen capture" in lower or "capture screen" in lower:
            t = self.registry.get("take_screenshot")
            if t:
                loc = "desktop"
                if "download" in lower: loc = "downloads"
                elif "document" in lower: loc = "documents"
                elif any(w in lower for w in ["picture", "photo", "image"]): loc = "pictures"
                return Route(kind="tool", tool=t, args={"location": loc}, confidence=0.9, latency_ms=elapsed_ms, tier="heuristic")

        # --- CLIPBOARD ---
        if "clipboard" in lower:
            if "clear" in lower or "empty" in lower:
                t = self.registry.get("clipboard_inspect")
                if t:
                    return Route(kind="tool", tool=t, args={"action": "clear"}, confidence=0.9, latency_ms=elapsed_ms, tier="heuristic")
            else:
                t = self.registry.get("clipboard_inspect")
                if t:
                    return Route(kind="tool", tool=t, args={"action": "get"}, confidence=0.9, latency_ms=elapsed_ms, tier="heuristic")

        # --- TIME/DATE ---
        if any(p in lower for p in ["what time", "current time", "what is the date", "what date"]):
            t = self.registry.get("current_time")
            if t:
                return Route(kind="tool", tool=t, args={}, confidence=0.95, latency_ms=elapsed_ms, tier="heuristic")

        # --- NETWORK STATUS / IP ---
        if any(p in lower for p in ["network status", "check internet", "internet connection", "what is my ip", "my ip address"]):
            t = self.registry.get("network_status")
            if t:
                return Route(kind="tool", tool=t, args={}, confidence=0.9, latency_ms=elapsed_ms, tier="heuristic")

        # --- BATTERY ---
        if "battery" in lower and ("health" in lower or "status" in lower or "level" in lower or "report" in lower):
            t = self.registry.get("battery_health")
            if t:
                action = "report" if "report" in lower else "status"
                return Route(kind="tool", tool=t, args={"action": action}, confidence=0.9, latency_ms=elapsed_ms, tier="heuristic")

        # --- DARK/LIGHT MODE ---
        if "dark mode" in lower or "light mode" in lower or "toggle theme" in lower or "switch theme" in lower:
            t = self.registry.get("toggle_dark_mode")
            if t:
                mode = "dark" if "dark" in lower else ("light" if "light" in lower else "toggle")
                return Route(kind="tool", tool=t, args={"mode": mode}, confidence=0.9, latency_ms=elapsed_ms, tier="heuristic")

        # --- MEDIA PLAYBACK ---
        if any(w in lower for w in ["play", "pause", "next song", "previous song", "skip track", "media"]):
            t = self.registry.get("media_playback")
            if t:
                if "next" in lower or "skip" in lower: op = "next"
                elif "prev" in lower or "previous" in lower: op = "prev"
                else: op = "play_pause"
                return Route(kind="tool", tool=t, args={"operation": op}, confidence=0.85, latency_ms=elapsed_ms, tier="heuristic")

        # --- FIND FILES ---
        if lower.startswith("find file") or lower.startswith("search file") or "find document" in lower:
            m = re.search(r"(?:find|search)\s+(?:file|document|folder)?\s*(?:named|called)?\s*(.+)", lower)
            term = m.group(1).strip() if m else "*"
            t = self.registry.get("find_files")
            if t:
                return Route(kind="tool", tool=t, args={"search_term": term}, confidence=0.85, latency_ms=elapsed_ms, tier="heuristic")

        # --- OPEN URL directly (https://...) ---
        url_match = re.search(r"(https?://\S+|[\w\-]+\.(com|org|net|io|dev|ai|gov|edu|co|me|app)[\S]*)", cleaned, re.IGNORECASE)
        if url_match:
            t = self.registry.get("web_search")
            if t:
                return Route(kind="tool", tool=t, args={"query": url_match.group(0)}, confidence=0.9, latency_ms=elapsed_ms, tier="heuristic")

        # No heuristic match
        return Route(kind=NEW_ACTION, confidence=0.5, latency_ms=elapsed_ms, tier="heuristic")

    def _fallback_route(self, text: str, tools: list[Tool], t0: float, ctx: WindowsContext, memory=None) -> Route:
        """Exact-intent fallback, used ONLY when Laya errors, times out, or is unavailable.

        Covers the intents that must never fail: volume level, mute, lock
        screen, open_folder / close_folder_window gated on a real directory
        on disk, and open_file gated on a real file on disk (Downloads
        searched first). Anything else returns NEW_ACTION so the generator
        tier can try. This method never maps phrases to tools beyond these
        exact intents.
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
            if folder_arg.lower() in _SYSTEM_FOLDERS or _folder_exists_on_disk(folder_arg):
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

        # Close an Explorer window — ONLY when a known folder nickname is
        # present AND resolves to a real directory. Matched by nickname, so
        # word order, filler ("please") and typos in other words ("foler")
        # do not matter; unresolved names go to NEW_ACTION.
        if re.search(r"\bclose\b", lower):
            hit = None
            hit_pos = len(lower) + 1
            for nick in _SYSTEM_FOLDERS:
                pos = lower.find(nick)
                if 0 <= pos < hit_pos:
                    hit, hit_pos = nick, pos
            if hit and _folder_exists_on_disk(hit):
                t = self.registry.get("close_folder_window")
                if t:
                    return Route(kind="tool", tool=t, args={"folder": hit}, confidence=0.9, latency_ms=elapsed_ms, tier="heuristic")

        # Web Search / Browser query (exact intent fallback when Laya times out).
        if any(w in lower for w in ["search", "google", "look up", "find results", "on the web", "in the web", "ultrafast", "browse"]):
            q = _strip_conversational_prefix(cleaned)
            for p in ["search for ", "search ", "find results for ", "find ", "look up ", "google ", "open "]:
                if q.lower().startswith(p):
                    q = q[len(p):].strip()
                    break
            q = re.sub(r"\s+(in|on)\s+the\s+web$", "", q, flags=re.IGNORECASE).strip()
            q = re.sub(r"\s+online$", "", q, flags=re.IGNORECASE).strip()

            if q:
                t = self.registry.get("web_search")
                if t:
                    return Route(kind="tool", tool=t, args={"query": q}, confidence=0.92, latency_ms=elapsed_ms, tier="heuristic")

        return Route(kind=NEW_ACTION, confidence=0.5, latency_ms=elapsed_ms, tier="heuristic")

