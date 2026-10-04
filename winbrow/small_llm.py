"""
Small-LLM Routing Tier (opt-in, local only)
===========================================
When Laya is too slow/unavailable AND the exact-intent fallback finds
nothing, an optional small local model (Ollama, e.g. qwen2.5:0.5b) gets one
shot at choosing a tool. This tier exists for paraphrases, typos, and word
orders the deterministic fallback cannot cover.

Safety design (hallucination-proofing, not prompt-hope):
- Disabled by default. Enable with WINBROW_ROUTER_LLM=1 (requires a local
  Ollama server with the model pulled; nothing ever leaves the machine).
- The model may ONLY pick from the tool catalog sent in the prompt. Unknown
  tool names are rejected.
- Only declared arg names are accepted; unknown args are dropped.
- Enum args must match the tool's criteria keys, else the arg default wins.
- Text args are capped in length; confidence must be numeric, else rejected.
- Anything rejected degrades to NEW_ACTION (generator tier), never to a guess.

No new third-party dependencies: plain urllib against Ollama's /api/chat.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from typing import Any, Optional

log = logging.getLogger("winbrow.small_llm")

MAX_TEXT_ARG_LEN = 200


def enabled() -> bool:
    """True only when explicitly opted in via environment."""
    return os.environ.get("WINBROW_ROUTER_LLM", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def model_name() -> str:
    return os.environ.get("WINBROW_ROUTER_MODEL", "qwen2.5:0.5b").strip() or "qwen2.5:0.5b"


def endpoint() -> str:
    return os.environ.get("WINBROW_ROUTER_ENDPOINT", "http://localhost:11434").rstrip("/")


def timeout_s() -> float:
    try:
        return max(5.0, float(os.environ.get("WINBROW_ROUTER_TIMEOUT", "25.0")))
    except ValueError:
        return 25.0


def build_system_prompt(tools: list) -> str:
    """Compact tool catalog: one line per tool so small models stay fast."""
    lines = [
        "You are a command router for a Windows desktop assistant.",
        "Reply with JSON ONLY, exactly: "
        '{"tool": "<tool_name>", "args": {"<arg>": "<value>"}, "confidence": 0.0-1.0}.',
        "Pick exactly one tool from this catalog (name: what it does [args]):",
    ]
    for t in tools:
        arg_names = ", ".join(a.name for a in (t.args or []))
        lines.append(f"- {t.name}: {t.description} [{arg_names}]")
    lines += [
        "- chat: general conversation, greetings, questions with no desktop action []",
        "- new_action: none of the tools above can do it []",
        "- stop: stop listening, cancel, close the assistant []",
        "Rules: use only catalog names; use only listed arg names; "
        "confidence is your certainty 0.0-1.0; JSON only, no other text.",
    ]
    return "\n".join(lines)


def _http_post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Blocking Ollama /api/chat call (runs in an executor)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _extract_json(text: str) -> Optional[dict[str, Any]]:
    """First {...} span that parses, else None."""
    if not text:
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except Exception:
                        pass
                    break
        start = text.find("{", start + 1)
    return None


def validate_choice(payload: Any, tools: list) -> Optional[tuple[str, dict[str, str], float]]:
    """Validate a model answer. Returns (tool, args, confidence) or None.

    Rejects: non-dicts, unknown tools, non-numeric confidence; drops unknown
    args; coerces bad enum values to the arg default; caps text length.
    """
    if not isinstance(payload, dict):
        return None
    by_name = {t.name: t for t in tools}
    for special in ("chat", "new_action", "stop"):
        by_name.setdefault(special, None)
    name = payload.get("tool")
    if not isinstance(name, str) or name not in by_name:
        return None
    try:
        conf = float(payload.get("confidence", float("nan")))
    except (TypeError, ValueError):
        return None
    if not (0.0 <= conf <= 1.0):
        return None
    raw_args = payload.get("args", {})
    if raw_args is None:
        raw_args = {}
    if not isinstance(raw_args, dict):
        return None
    args: dict[str, str] = {}
    tool = by_name[name]
    if tool is not None:
        specs = {a.name: a for a in (tool.args or [])}
        for key, val in raw_args.items():
            spec = specs.get(key)
            if spec is None:
                continue  # unknown arg: drop, never forward
            text = str(val)[:MAX_TEXT_ARG_LEN]
            if spec.kind == "enum" and text not in (spec.criteria or {}):
                text = spec.default or ""
            args[key] = text
    return (name, args, conf)


async def route_with_llm(
    utterance: str,
    tools: list,
    context_line: str = "",
    timeout: Optional[float] = None,
) -> Optional[tuple[str, dict[str, str], float]]:
    """One constrained routing call. Returns validated choice or None."""
    if not enabled():
        return None
    limit = timeout if timeout is not None else timeout_s()
    system = build_system_prompt(tools)
    user = utterance.strip()
    if context_line:
        user += f"\n(Foreground: {context_line})"
    payload = {
        "model": model_name(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0, "num_predict": 200},
    }
    loop = asyncio.get_event_loop()
    try:
        resp = await asyncio.wait_for(
            loop.run_in_executor(
                None, lambda: _http_post(f"{endpoint()}/api/chat", payload, limit)
            ),
            timeout=limit + 5,
        )
    except Exception as e:
        log.warning(f"Small-LLM routing call failed: {e}")
        return None
    try:
        content = (resp.get("message") or {}).get("content", "")
    except Exception:
        return None
    data = _extract_json(content)
    if data is None:
        log.warning("Small-LLM routing returned non-JSON output.")
        return None
    return validate_choice(data, tools)
