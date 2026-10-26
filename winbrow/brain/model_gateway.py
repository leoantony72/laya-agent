"""
Model Gateway — Provider-Agnostic LLM Access for the WinBrow Brain
===================================================================
One interface, many backends, tried in priority order:

  1. Nebius Token Factory  — OpenAI-compatible. Default model:
                              deepseek-ai/DeepSeek-V4-Flash-0731
                              (env: NEBIUS_API_KEY, WINBROW_NEBIUS_MODEL)
  2. OpenRouter            — OpenAI-compatible. Default model:
                              deepseek/deepseek-v4.1-flash (env: OPENROUTER_API_KEY)
  3. Other cloud keys      — OpenAI (OPENAI_API_KEY)
  4. Local OpenAI-compatible — LM Studio (:1234/v1) / Ollama (:11434/v1) if reachable
  5. No provider           — chat() raises GatewayUnavailable → callers fall back
                              to heuristics/generator tiers.

Env WINBROW_BRAIN_PROVIDER reorders the chain:

  WINBROW_BRAIN_PROVIDER=nebius     → Nebius Token Factory first
  WINBROW_BRAIN_PROVIDER=openrouter → OpenRouter first
  WINBROW_BRAIN_PROVIDER=openai     → OpenAI first
  WINBROW_BRAIN_PROVIDER=local|qwen → local Qwen first, cloud after
  (auto — default)                  → Nebius, OpenRouter, OpenAI, local

When the local brain is requested and WINBROW_LOCAL_LLM_ENDPOINT is not set,
the built-in llama.cpp server at 127.0.0.1:8080 (models/qwen GGUF) is used;
WINBROW_LOCAL_LLM=1 is an alias for the same. WINBROW_LOCAL_LLM_MODEL picks
the model name sent to custom endpoints (Ollama/LM Studio); the built-in
llama.cpp server defaults to "qwen2.5-0.5b-instruct".

Supports OpenAI `tools` (function calling) and `response_format` json_schema.
API keys are read from env/runtime config and never logged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger("winbrow.brain.gateway")

OPENROUTER_BASE = os.environ.get("WINBROW_OPENROUTER_BASE", "https://openrouter.ai/api/v1")
DEFAULT_BRAIN_MODEL = os.environ.get("WINBROW_BRAIN_MODEL", "deepseek/deepseek-v4.1-flash")

# Nebius Token Factory: OpenAI-compatible inference API.
# Docs: https://docs.tokenfactory.nebius.com/
NEBIUS_BASE = os.environ.get(
    "WINBROW_NEBIUS_BASE", "https://api.tokenfactory.nebius.com/v1"
).rstrip("/")
NEBIUS_DEFAULT_MODEL = (
    os.environ.get("WINBROW_NEBIUS_MODEL")
    or os.environ.get("NEBIUS_MODEL")
    or "deepseek-ai/DeepSeek-V4-Flash-0731"
)

# Upstream OpenRouter provider for DeepSeek V4.1 Flash. Set to an empty string
# to let OpenRouter choose automatically, and allow fallbacks with 1.
OPENROUTER_ONLY_PROVIDER = os.environ.get("WINBROW_OPENROUTER_PROVIDER", "relace").strip()
OPENROUTER_ALLOW_FALLBACKS = os.environ.get("WINBROW_OPENROUTER_ALLOW_FALLBACKS", "0") == "1"

REQUEST_TIMEOUT_S = float(os.environ.get("WINBROW_BRAIN_TIMEOUT", "45"))

# Reasoning models (DeepSeek V4.1 Flash and R1-style models) spend completion
# tokens on hidden reasoning BEFORE emitting content, and those tokens count
# against max_tokens. Unbounded, a long think can consume the whole budget:
# content comes back empty with finish_reason="length" and the planner sees an
# unusable reply. Cap reasoning on structured calls so JSON always has room.
REASONING_MAX_TOKENS = int(os.environ.get("WINBROW_REASONING_MAX_TOKENS", "2000"))

# Retry ladder used when content is empty because reasoning ate the budget:
# first tighten the reasoning cap (same total budget), then disable reasoning
# entirely so the model has to emit its JSON answer.
REASONING_RETRY_MAX_TOKENS = int(os.environ.get("WINBROW_REASONING_RETRY_TOKENS", "400"))
REASONING_OFF_MAX_TOKENS = int(os.environ.get("WINBROW_REASONING_OFF_MAX_TOKENS", "1000"))

# Planner call budget. Some reasoning models ignore reasoning.max_tokens and
# spend most of max_tokens thinking, so the total must be large enough that
# ~14k reasoning tokens still leave room for the JSON plan on the wire.
PLANNER_CALL_CONFIG = {
    "max_tokens": int(os.environ.get("WINBROW_PLANNER_MAX_TOKENS", "16000")),
    "temperature": float(os.environ.get("WINBROW_PLANNER_TEMPERATURE", "1")),
}


def _history_skill(entry: Any) -> str:
    if isinstance(entry, dict):
        step = entry.get("step")
        if isinstance(step, dict):
            return str(step.get("skill") or "")
        return str(entry.get("skill") or "")
    return str(getattr(entry, "skill", "") or "")


def get_reasoning_config(history: List[Any]) -> dict:
    """Per-turn planner budget: browser decisions get less thinking room.

    Browser turns are mostly "read state → pick one element". A reasoning
    model that spends 10k+ tokens evaluating alternatives can exhaust
    max_tokens and return empty content, so cap reasoning when the recent
    history is entirely browser work.
    """
    recent_skills = [_history_skill(h) for h in (history or [])[-3:]]
    recent_skills = [s for s in recent_skills if s]
    browser_heavy = bool(recent_skills) and all(
        s.startswith("browser_") for s in recent_skills
    )
    if browser_heavy:
        return {"max_tokens": 8000, "reasoning_max": 600}
    return {
        "max_tokens": PLANNER_CALL_CONFIG["max_tokens"],
        "reasoning_max": REASONING_MAX_TOKENS,
    }

# Built-in local brain: llama.cpp server started by server.py (winbrow/llm.py)
BUILTIN_LOCAL_ENDPOINT = "http://127.0.0.1:8080"
BUILTIN_LOCAL_MODEL = "qwen2.5-0.5b-instruct"
DEFAULT_LOCAL_TIMEOUT_S = 300.0

LOCAL_PROVIDER_ALIASES = ("local", "qwen", "llama.cpp", "llamacpp")


def brain_provider_pref() -> str:
    """Selected brain provider: 'auto' | 'nebius' | 'openrouter' | 'openai' | local aliases."""
    return (os.environ.get("WINBROW_BRAIN_PROVIDER") or "auto").strip().lower()


def wants_local_brain() -> bool:
    """True when env asks for the local Qwen as the primary brain."""
    if os.environ.get("WINBROW_LOCAL_LLM", "").strip() == "1":
        return True
    return brain_provider_pref() in LOCAL_PROVIDER_ALIASES


def uses_builtin_local_server() -> bool:
    """True when the local brain is served by the built-in llama.cpp server."""
    return wants_local_brain() and not os.environ.get("WINBROW_LOCAL_LLM_ENDPOINT", "").strip()


class GatewayUnavailable(Exception):
    """No provider configured / reachable. Callers should use fallback tiers."""


class GatewayError(Exception):
    """Provider call failed after retries."""


class PlannerFailure(GatewayError):
    """Reasoning consumed the budget and no content could be produced."""

    def __init__(self, message: str, finish_reason: str = "",
                 reasoning_length: int = 0):
        super().__init__(message)
        self.finish_reason = finish_reason
        self.reasoning_length = reasoning_length


@dataclass
class ToolCall:
    name: str
    arguments: dict = field(default_factory=dict)


@dataclass
class GatewayResponse:
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    elapsed_ms: float = 0.0
    reasoning: str = ""          # hidden chain-of-thought, when the provider returns it
    finish_reason: str = ""      # "stop" | "length" (truncated) | "tool_calls" | ...
    usage: Dict[str, Any] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"

    @property
    def reasoning_length(self) -> int:
        return len(self.reasoning or "")


@dataclass
class Provider:
    name: str
    base_url: str
    api_key: str = ""
    model: str = ""
    extra_headers: Dict[str, str] = field(default_factory=dict)

    def describe(self) -> dict:
        return {
            "name": self.name,
            "base_url": self.base_url,
            "model": self.model,
            "has_key": bool(self.api_key),
        }


def _provider_timeout(provider: Provider) -> float:
    """Local CPU models generate slower; give them a higher default ceiling."""
    if provider.name == "local":
        try:
            return float(os.environ.get("WINBROW_LOCAL_LLM_TIMEOUT", str(DEFAULT_LOCAL_TIMEOUT_S)))
        except ValueError:
            return DEFAULT_LOCAL_TIMEOUT_S
    return REQUEST_TIMEOUT_S


TRANSIENT_HTTP_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


def _retry_delay(err: Exception) -> float:
    """Respect Retry-After when the provider sends one, capped for voice UX."""
    if isinstance(err, urllib.error.HTTPError):
        try:
            after = float(err.headers.get("Retry-After", ""))
            return min(max(after, 1.0), 8.0)
        except (TypeError, ValueError, AttributeError):
            pass
    return 1.5


def _local_ctx_tokens() -> int:
    try:
        return int(os.environ.get("WINBROW_LOCAL_LLM_CTX", "8192"))
    except ValueError:
        return 8192


def _is_transient_error(err: Exception) -> bool:
    """Rate limits / 5xx / network hiccups are worth a short retry; bad
    requests (400s) and malformed responses are not."""
    if isinstance(err, urllib.error.HTTPError):
        return err.code in TRANSIENT_HTTP_CODES
    if isinstance(err, (TimeoutError, ConnectionError)):
        return True
    if isinstance(err, urllib.error.URLError):
        return True
    text = str(err).lower()
    return any(k in text for k in (
        "timed out", "timeout", "connection reset", "connection aborted",
        "temporarily unavailable", "rate limit", "overloaded",
    ))


def _post_json(url: str, payload: dict, headers: Dict[str, str], timeout: float) -> dict:
    """Blocking HTTP POST returning parsed JSON. Raises on HTTP/network errors."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return json.loads(body)


def _redact(obj: Any) -> Any:
    """Best-effort removal of key-like strings from error payloads."""
    try:
        text = str(obj)
        for k in ("sk-", "Bearer "):
            if k in text:
                import re
                text = re.sub(re.escape(k) + r"[A-Za-z0-9_\-\.]{8,}", k + "***", text)
        return text
    except Exception:
        return "<unprintable error>"


def _clean_and_load_json(s: str) -> Optional[dict]:
    """Helper to clean common LLM formatting issues before json.loads."""
    if not s or ("{" not in s and "[" not in s):
        return None
    s = s.strip()
    if s.startswith("```"):
        import re
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)
    try:
        data = json.loads(s)
        if isinstance(data, dict):
            return data.get("plan") if isinstance(data.get("plan"), dict) else data
        if isinstance(data, list):
            return {"kind": "action", "steps": data}
    except Exception:
        pass

    import re
    # Remove single-line and multi-line comments
    cleaned = re.sub(r"//[^\r\n]*", "", s)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
    # Remove trailing commas before } or ]
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data.get("plan") if isinstance(data.get("plan"), dict) else data
        if isinstance(data, list):
            return {"kind": "action", "steps": data}
    except Exception:
        pass

    try:
        import ast
        val = ast.literal_eval(cleaned)
        if isinstance(val, dict):
            return val.get("plan") if isinstance(val.get("plan"), dict) else val
        if isinstance(val, list):
            return {"kind": "action", "steps": val}
    except Exception:
        pass
    return None


def _repair_truncated_json(s: str) -> Optional[dict]:
    """Attempt to balance unclosed quotes, brackets, and braces in truncated JSON."""
    if not s or ("{" not in s and "[" not in s):
        return None
    s = s.strip()
    if s.startswith("```"):
        import re
        s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s*```$", "", s)
    idx_brace = s.find("{")
    idx_bracket = s.find("[")
    if idx_brace == -1 and idx_bracket == -1:
        return None
    start = idx_brace if (idx_bracket == -1 or (idx_brace != -1 and idx_brace < idx_bracket)) else idx_bracket
    chunk = s[start:]

    in_string = False
    escape = False
    stack = []
    for c in chunk:
        if c == '"' and not escape:
            in_string = not in_string
        elif c == '\\' and in_string:
            escape = not escape
            continue
        elif not in_string:
            if c in ('{', '['):
                stack.append('}' if c == '{' else ']')
            elif c in ('}', ']'):
                if stack and stack[-1] == c:
                    stack.pop()
        escape = False

    repaired = chunk
    if in_string:
        repaired += '"'
    import re
    repaired = re.sub(r",\s*$", "", repaired)
    while stack:
        repaired += stack.pop()

    return _clean_and_load_json(repaired)


def extract_json_object(text: str) -> Optional[dict]:
    """Pull the first valid JSON object out of an LLM response (handles code fences, <think> tags, trailing commas)."""
    if not text:
        return None
    t = str(text).strip()

    # 1. Strip reasoning/think blocks (DeepSeek R1, Qwen reasoning, etc.)
    import re
    t = re.sub(r"<(think|thought)>.*?</\1>", "", t, flags=re.DOTALL).strip()

    # 2. Extract from code fence if present
    if "```" in t:
        fence_matches = re.findall(r"```(?:json)?\s*([\s\S]*?)```", t, re.IGNORECASE)
        for block in fence_matches:
            block = block.strip()
            if "{" in block or "[" in block:
                parsed = _clean_and_load_json(block) or _repair_truncated_json(block)
                if parsed is not None:
                    return parsed

    # 3. Try direct clean and load
    parsed = _clean_and_load_json(t)
    if parsed is not None:
        return parsed

    # 4. Search for balanced { ... } block
    start_idx = -1
    depth = 0
    in_string = False
    escape = False
    for i, c in enumerate(t):
        if c == '"' and not escape:
            in_string = not in_string
        elif c == '\\' and in_string:
            escape = not escape
            continue
        elif not in_string:
            if c == '{':
                if depth == 0:
                    start_idx = i
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0 and start_idx != -1:
                    candidate = t[start_idx : i + 1]
                    res = _clean_and_load_json(candidate)
                    if res is not None:
                        return res
        escape = False

    # 5. Search for balanced [ ... ] block
    start_idx = -1
    depth = 0
    in_string = False
    escape = False
    for i, c in enumerate(t):
        if c == '"' and not escape:
            in_string = not in_string
        elif c == '\\' and in_string:
            escape = not escape
            continue
        elif not in_string:
            if c == '[':
                if depth == 0:
                    start_idx = i
                depth += 1
            elif c == ']':
                depth -= 1
                if depth == 0 and start_idx != -1:
                    candidate = t[start_idx : i + 1]
                    res = _clean_and_load_json(candidate)
                    if res is not None:
                        return res
        escape = False

    # 6. Fallback: span from first { to last }
    start = t.find("{")
    end = t.rfind("}")
    if start >= 0 and end > start:
        res = _clean_and_load_json(t[start : end + 1])
        if res is not None:
            return res

    # 7. Fallback: repair truncated JSON
    repaired = _repair_truncated_json(t)
    if repaired is not None:
        return repaired

    return None


def _prompt_cache_enabled() -> bool:
    """Prompt caching is on unless WINBROW_PROMPT_CACHE explicitly disables it."""
    return os.environ.get("WINBROW_PROMPT_CACHE", "1").strip().lower() not in (
        "0", "false", "off", "no",
    )


def _flatten_content(content: Any) -> Any:
    """Turn Anthropic-style content blocks into one string for OpenAI/local."""
    if isinstance(content, list):
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict)
        ]
        return "\n\n".join(part for part in parts if part)
    return content


def _adapt_messages(
    messages: List[Dict[str, Any]], provider: Provider,
) -> List[Dict[str, Any]]:
    """Keep two cached system blocks for OpenRouter; flatten elsewhere.

    OpenRouter accepts Anthropic-style content blocks and forwards
    ``cache_control`` to the upstream provider. OpenAI-compatible endpoints
    (OpenAI, local llama.cpp) require a plain string system message, so the
    blocks are flattened there and still preserve both text sections.
    """
    if not any(isinstance(message.get("content"), list) for message in messages):
        return messages
    if provider.name == "openrouter" and _prompt_cache_enabled():
        return messages
    return [
        {**message, "content": _flatten_content(message.get("content"))}
        for message in messages
    ]


class ModelGateway:
    """Chat interface with automatic provider fallback."""

    def __init__(
        self,
        openrouter_key: Optional[str] = None,
        openai_key: Optional[str] = None,
        brain_model: Optional[str] = None,
        local_endpoint: Optional[str] = None,
        nebius_key: Optional[str] = None,
    ):
        self.providers: List[Provider] = []
        self._build_chain(
            openrouter_key=openrouter_key,
            openai_key=openai_key,
            brain_model=brain_model,
            local_endpoint=local_endpoint,
            nebius_key=nebius_key,
        )

    def _build_chain(self, openrouter_key, openai_key, brain_model, local_endpoint,
                     nebius_key=None) -> None:
        pref = brain_provider_pref()

        # Nebius Token Factory first in auto mode. It is OpenAI-compatible and
        # can be selected explicitly with WINBROW_BRAIN_PROVIDER=nebius.
        nb_key = (
            nebius_key
            or os.environ.get("NEBIUS_API_KEY", "")
            or os.environ.get("WINBROW_NEBIUS_API_KEY", "")
        )
        if nb_key:
            self.providers.append(Provider(
                name="nebius",
                base_url=os.environ.get(
                    "WINBROW_NEBIUS_BASE", NEBIUS_BASE
                ).rstrip("/"),
                api_key=nb_key,
                model=(
                    os.environ.get("WINBROW_NEBIUS_MODEL")
                    or os.environ.get("NEBIUS_MODEL")
                    or brain_model
                    or NEBIUS_DEFAULT_MODEL
                ),
            ))

        or_key = openrouter_key or os.environ.get("OPENROUTER_API_KEY", "")
        if or_key:
            self.providers.append(Provider(
                name="openrouter",
                base_url=OPENROUTER_BASE,
                api_key=or_key,
                model=brain_model or DEFAULT_BRAIN_MODEL,
                extra_headers={
                    "HTTP-Referer": "https://github.com/winbrow",
                    "X-Title": "WinBrow Desktop Agent",
                },
            ))
        oa_key = openai_key or os.environ.get("OPENAI_API_KEY", "")
        if oa_key:
            self.providers.append(Provider(
                name="openai",
                base_url="https://api.openai.com/v1",
                api_key=oa_key,
                model=os.environ.get("WINBROW_OPENAI_MODEL", "gpt-4o-mini"),
            ))
        # Local OpenAI-compatible (llama.cpp / LM Studio / Ollama serve).
        # Explicit endpoint first; built-in llama.cpp server when the local
        # brain is requested and no custom endpoint is configured.
        builtin = False
        ep = local_endpoint or os.environ.get("WINBROW_LOCAL_LLM_ENDPOINT", "").strip()
        if not ep and wants_local_brain():
            ep = BUILTIN_LOCAL_ENDPOINT
            builtin = True
        if ep:
            base = ep.rstrip("/")
            if not base.endswith("/v1"):
                base = base + "/v1"
            model = os.environ.get("WINBROW_LOCAL_LLM_MODEL", "").strip()
            if not model and builtin:
                model = BUILTIN_LOCAL_MODEL
            self.providers.append(Provider(
                name="local",
                base_url=base,
                api_key=os.environ.get("WINBROW_LOCAL_LLM_KEY", "not-needed"),
                model=model,
            ))

        # Reorder for the selected brain. Cloud stays as fallback so a weak/
        # offline local model degrades gracefully instead of failing the task.
        if wants_local_brain():
            self._prioritize("local")
        elif pref == "nebius":
            self._prioritize("nebius")
        elif pref == "openrouter":
            self._prioritize("openrouter")
        elif pref == "openai":
            self._prioritize("openai")
        elif pref not in ("auto", ""):
            log.warning(f"Unknown WINBROW_BRAIN_PROVIDER '{pref}'; keeping default chain order.")

        if not self.providers:
            log.warning(
                "ModelGateway: no providers configured "
                "(set NEBIUS_API_KEY or OPENROUTER_API_KEY)."
            )

    def _prioritize(self, name: str) -> None:
        """Move the named provider (if present) to the front of the chain."""
        for i, p in enumerate(self.providers):
            if p.name == name:
                self.providers.insert(0, self.providers.pop(i))
                return

    # ------------------------------------------------------------------ status
    @property
    def available(self) -> bool:
        return bool(self.providers)

    def status(self) -> dict:
        return {
            "available": self.available,
            "providers": [p.describe() for p in self.providers],
        }

    # ------------------------------------------------------------------ chat
    async def chat(
        self,
        messages: List[Dict[str, str]],
        tools: Optional[List[dict]] = None,
        json_schema: Optional[dict] = None,
        temperature: float = 0.2,
        max_tokens: int = 900,
        timeout: Optional[float] = None,
        reasoning: Optional[dict] = None,
    ) -> GatewayResponse:
        """One chat completion across the provider chain. Async wrapper.

        `reasoning` overrides the OpenRouter reasoning policy for this call
        (e.g. {"enabled": False} for normalizers/chat). By default, structured
        calls get a bounded reasoning budget and unstructured calls disable it,
        so hidden reasoning can never eat the whole max_tokens allowance.
        """
        if not self.providers:
            raise GatewayUnavailable(
                "No LLM provider configured (set NEBIUS_API_KEY or OPENROUTER_API_KEY)"
            )
        last_err: Optional[Exception] = None
        for provider in self.providers:
            t0 = time.perf_counter()
            # One retry for transient failures (HTTP 429 from free tiers,
            # timeouts, 5xx) — the closed-loop planner calls providers in
            # quick succession, which makes rate limiting common.
            for attempt in range(2):
                try:
                    resp = await self._chat_with_provider(
                        provider, messages, tools, json_schema, temperature, max_tokens,
                        timeout or _provider_timeout(provider), reasoning,
                    )
                    # Reasoning can exhaust max_tokens before any content is
                    # emitted. Retry with a hard reasoning cap, then with
                    # reasoning off entirely, so the model is forced to write
                    # the JSON answer instead of thinking until it is cut off.
                    if self._empty_after_reasoning(resp, provider):
                        log.warning(
                            f"finish_reason={resp.finish_reason!r} — "
                            f"reasoning={resp.reasoning_length} tokens, "
                            f"content={'empty' if not resp.content else len(resp.content)}; "
                            "retrying with capped reasoning."
                        )
                        try:
                            answered = await self._retry_with_capped_reasoning(
                                provider, messages, tools, json_schema, temperature,
                                max_tokens, timeout or _provider_timeout(provider),
                            )
                        except GatewayUnavailable:
                            raise
                        except Exception as e:
                            last_err = e
                            log.warning(
                                f"Reasoning retries on '{provider.name}' failed: "
                                f"{_redact(e)}; trying next."
                            )
                            break
                        if answered is not None and (
                                answered.content.strip() or answered.tool_calls):
                            answered.elapsed_ms = round(
                                (time.perf_counter() - t0) * 1000, 1)
                            return answered
                        detail = answered or resp
                        last_err = PlannerFailure(
                            f"finish_reason={detail.finish_reason!r}, "
                            f"reasoning_tokens={detail.reasoning_length}, "
                            "content=empty after reasoning retries",
                            finish_reason=detail.finish_reason,
                            reasoning_length=detail.reasoning_length,
                        )
                        log.warning(
                            f"Provider '{provider.name}' still returned no "
                            "content after reasoning retries; trying next."
                        )
                        break
                    resp.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
                    return resp
                except GatewayUnavailable:
                    raise
                except Exception as e:
                    last_err = e
                    if attempt == 0 and _is_transient_error(e):
                        log.warning(
                            f"Provider '{provider.name}' transient failure "
                            f"({_redact(e)}); retrying once."
                        )
                        await asyncio.sleep(_retry_delay(e))
                        continue
                    log.warning(f"Provider '{provider.name}' failed: {_redact(e)}; trying next.")
                    break
        if isinstance(last_err, PlannerFailure):
            raise last_err
        raise GatewayError(f"All providers failed; last error: {_redact(last_err)}")

    @staticmethod
    def _empty_after_reasoning(resp: GatewayResponse, provider: Provider) -> bool:
        return bool(
            not resp.content.strip()
            and not resp.tool_calls
            and provider.name in ("openrouter", "nebius")
            and (resp.reasoning or resp.finish_reason == "length")
        )

    async def _retry_with_capped_reasoning(
        self,
        provider: Provider,
        messages: List[Dict[str, str]],
        tools: Optional[List[dict]],
        json_schema: Optional[dict],
        temperature: float,
        max_tokens: int,
        timeout: float,
    ) -> Optional[GatewayResponse]:
        """Retry an empty truncated response with a tighter reasoning budget.

        Attempt 2 keeps the total budget but caps reasoning hard. Attempt 3
        disables reasoning entirely and only asks for enough tokens to emit
        the JSON answer. Returns the last response if none produce content.
        """
        capped = min(REASONING_RETRY_MAX_TOKENS, max(64, max_tokens // 2))
        answer_budget = max(256, min(max_tokens, REASONING_OFF_MAX_TOKENS))
        ladder = (
            (max_tokens, {"max_tokens": capped}),
            (answer_budget, {"enabled": False}),
        )
        last_resp: Optional[GatewayResponse] = None
        for attempt_no, (budget, policy) in enumerate(ladder, start=2):
            try:
                resp = await self._chat_with_provider(
                    provider, messages, tools, json_schema, temperature,
                    budget, timeout, policy,
                )
            except GatewayUnavailable:
                raise
            except Exception as e:
                log.warning(
                    f"Reasoning retry {attempt_no} on '{provider.name}' "
                    f"failed: {_redact(e)}; trying next policy."
                )
                continue
            last_resp = resp
            if resp.content.strip() or resp.tool_calls:
                return resp
            log.warning(
                f"Reasoning retry {attempt_no} on '{provider.name}' still "
                f"empty (finish_reason={resp.finish_reason!r}, "
                f"reasoning={resp.reasoning_length} tokens)."
            )
        return last_resp

    async def _chat_with_provider(
        self,
        provider: Provider,
        messages: List[Dict[str, str]],
        tools: Optional[List[dict]],
        json_schema: Optional[dict],
        temperature: float,
        max_tokens: int,
        timeout: float,
        reasoning: Optional[dict] = None,
    ) -> GatewayResponse:
        import asyncio
        messages = _adapt_messages(messages, provider)
        payload: Dict[str, Any] = {
            "model": provider.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]
        if json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "winbrow_plan", "strict": False, "schema": json_schema},
            }
        if provider.name == "openrouter":
            if reasoning is not None:
                payload["reasoning"] = reasoning
            elif json_schema:
                # Leave the rest of the budget for the actual JSON object.
                if REASONING_MAX_TOKENS > 0:
                    payload["reasoning"] = {
                        "max_tokens": min(
                            REASONING_MAX_TOKENS, max(64, max_tokens // 2)),
                    }
            else:
                # Short replies (chat, STT normalization, parameter fill) do
                # not need chain-of-thought and cannot afford it.
                payload["reasoning"] = {"enabled": False}
        if provider.name == "nebius":
            # Nebius exposes OpenAI-style reasoning_effort rather than
            # OpenRouter's reasoning object. Translate the shared policy:
            # structured planner calls get a medium effort; chat/normalizers
            # disable it; an explicit retry policy maps to low/none.
            effort = "medium" if json_schema else "none"
            if reasoning is not None:
                if reasoning.get("enabled") is False:
                    effort = "none"
                elif reasoning.get("max_tokens"):
                    effort = "low"
            payload["reasoning_effort"] = effort
        if (
            provider.name == "openrouter"
            and OPENROUTER_ONLY_PROVIDER
            and "deepseek-v4.1-flash" in provider.model
        ):
            payload["provider"] = {
                "only": [OPENROUTER_ONLY_PROVIDER],
                "allow_fallbacks": OPENROUTER_ALLOW_FALLBACKS,
            }

        if provider.name == "local":
            # Never ask for more completion tokens than remain after the
            # prompt: the built-in llama.cpp server rejects the whole request
            # when prompt + max_tokens exceed its context window (the closed
            # loop re-sends the skills block every turn).
            est_prompt = sum(len(str(m.get("content", ""))) for m in messages) // 4
            if json_schema:
                est_prompt += len(json.dumps(json_schema)) // 4
            room = _local_ctx_tokens() - est_prompt - 200
            if max_tokens > room:
                clamped = max(256, room)
                log.info(
                    f"Local context guard: max_tokens {max_tokens} → {clamped} "
                    f"(prompt ≈{est_prompt} tokens, ctx {_local_ctx_tokens()})"
                )
                max_tokens = clamped
                payload["max_tokens"] = clamped

        headers = {"Authorization": f"Bearer {provider.api_key}", **provider.extra_headers}
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: _post_json(f"{provider.base_url}/chat/completions", payload, headers, timeout),
        )
        return self._parse_response(result, provider)

    def _parse_response(self, result: dict, provider: Provider) -> GatewayResponse:
        try:
            choice = result["choices"][0]
            msg = choice.get("message", {})
        except (KeyError, IndexError, TypeError):
            raise GatewayError(f"Malformed provider response: {_redact(result)[:300]}")

        reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
        if not reasoning:
            details = msg.get("reasoning_details")
            if isinstance(details, list):
                reasoning = "\n".join(
                    str(d.get("text") or d.get("summary") or "")
                    for d in details if isinstance(d, dict)
                ).strip()

        usage_raw = result.get("usage")
        usage: Dict[str, Any] = usage_raw if isinstance(usage_raw, dict) else {}
        input_tokens = int(
            usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        )
        output_tokens = int(
            usage.get("output_tokens") or usage.get("completion_tokens") or 0
        )
        prompt_details = usage.get("prompt_tokens_details")
        if not isinstance(prompt_details, dict):
            prompt_details = {}
        cache_read = int(
            usage.get("cache_read_input_tokens")
            or usage.get("prompt_cache_hit_tokens")
            or prompt_details.get("cached_tokens") or 0
        )
        cache_create = int(
            usage.get("cache_creation_input_tokens")
            or usage.get("prompt_cache_miss_tokens") or 0
        )
        resp = GatewayResponse(
            content=msg.get("content") or "",
            provider=provider.name,
            model=result.get("model", provider.model),
            reasoning=str(reasoning or ""),
            finish_reason=str(choice.get("finish_reason") or ""),
            usage=usage,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_create,
        )
        log.info(
            f"tokens — input: {input_tokens}, cache_read: {cache_read}, "
            f"cache_create: {cache_create} "
            f"(provider={provider.name}, output={output_tokens})"
        )
        for tc in msg.get("tool_calls") or []:
            try:
                fn = tc.get("function", {})
                args_raw = fn.get("arguments") or "{}"
                args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
                resp.tool_calls.append(ToolCall(name=fn.get("name", ""), arguments=args))
            except (json.JSONDecodeError, AttributeError, TypeError) as e:
                log.warning(f"Skipping unparseable tool call: {e}")
        # some models return JSON in content even with tools
        return resp


_gateway_instance: Optional[ModelGateway] = None


def get_gateway() -> ModelGateway:
    """Gateway singleton (rebuilds if env changed)."""
    global _gateway_instance
    if _gateway_instance is None:
        _gateway_instance = ModelGateway()
    return _gateway_instance


def reset_gateway() -> None:
    """Force rebuild on next get_gateway() (used by settings updates/tests)."""
    global _gateway_instance
    _gateway_instance = None
