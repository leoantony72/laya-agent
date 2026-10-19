"""
Model Gateway — Provider-Agnostic LLM Access for the WinBrow Brain
===================================================================
One interface, many backends, tried in priority order:

  1. OpenRouter (default)   — OpenAI-compatible. Default model:
                              deepseek/deepseek-v4.1-flash  (env: WINBROW_BRAIN_MODEL)
  2. Other cloud keys       — OpenAI (OPENAI_API_KEY)
  3. Local OpenAI-compatible — LM Studio (:1234/v1) / Ollama (:11434/v1) if reachable
  4. No provider            — chat() raises GatewayUnavailable → callers fall back
                              to heuristics/generator tiers.

Env WINBROW_BRAIN_PROVIDER reorders the chain so the local model can be the
brain instead of the cloud (cloud stays as fallback):

  WINBROW_BRAIN_PROVIDER=local | qwen   → local Qwen first, cloud after
  WINBROW_BRAIN_PROVIDER=openrouter | openai → cloud first (default order)
  (auto — default)                      → cloud first, local last

When the local brain is requested and WINBROW_LOCAL_LLM_ENDPOINT is not set,
the built-in llama.cpp server at 127.0.0.1:8080 (models/qwen GGUF) is used;
WINBROW_LOCAL_LLM=1 is an alias for the same. WINBROW_LOCAL_LLM_MODEL picks
the model name sent to custom endpoints (Ollama/LM Studio); the built-in
llama.cpp server defaults to "qwen2.5-0.5b-instruct".

Supports OpenAI `tools` (function calling) and `response_format` json_schema.
API keys are read from env/runtime config and never logged.
"""

from __future__ import annotations

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

REQUEST_TIMEOUT_S = float(os.environ.get("WINBROW_BRAIN_TIMEOUT", "45"))

# Built-in local brain: llama.cpp server started by server.py (winbrow/llm.py)
BUILTIN_LOCAL_ENDPOINT = "http://127.0.0.1:8080"
BUILTIN_LOCAL_MODEL = "qwen2.5-0.5b-instruct"
DEFAULT_LOCAL_TIMEOUT_S = 300.0

LOCAL_PROVIDER_ALIASES = ("local", "qwen", "llama.cpp", "llamacpp")


def brain_provider_pref() -> str:
    """Selected brain provider: 'auto' (default) | 'local'/'qwen' | 'openrouter' | 'openai'."""
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

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


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


class ModelGateway:
    """Chat interface with automatic provider fallback."""

    def __init__(
        self,
        openrouter_key: Optional[str] = None,
        openai_key: Optional[str] = None,
        brain_model: Optional[str] = None,
        local_endpoint: Optional[str] = None,
    ):
        self.providers: List[Provider] = []
        self._build_chain(
            openrouter_key=openrouter_key,
            openai_key=openai_key,
            brain_model=brain_model,
            local_endpoint=local_endpoint,
        )

    def _build_chain(self, openrouter_key, openai_key, brain_model, local_endpoint) -> None:
        pref = brain_provider_pref()
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
        elif pref == "openai":
            self._prioritize("openai")
        elif pref not in ("auto", "", "openrouter"):
            log.warning(f"Unknown WINBROW_BRAIN_PROVIDER '{pref}'; keeping default chain order.")

        if not self.providers:
            log.warning("ModelGateway: no providers configured (set OPENROUTER_API_KEY).")

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
    ) -> GatewayResponse:
        """One chat completion across the provider chain. Async wrapper."""
        if not self.providers:
            raise GatewayUnavailable("No LLM provider configured (set OPENROUTER_API_KEY)")
        last_err: Optional[Exception] = None
        for provider in self.providers:
            t0 = time.perf_counter()
            try:
                resp = await self._chat_with_provider(
                    provider, messages, tools, json_schema, temperature, max_tokens,
                    timeout or _provider_timeout(provider),
                )
                resp.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
                return resp
            except GatewayUnavailable:
                raise
            except Exception as e:
                last_err = e
                log.warning(f"Provider '{provider.name}' failed: {_redact(e)}; trying next.")
        raise GatewayError(f"All providers failed; last error: {_redact(last_err)}")

    async def _chat_with_provider(
        self,
        provider: Provider,
        messages: List[Dict[str, str]],
        tools: Optional[List[dict]],
        json_schema: Optional[dict],
        temperature: float,
        max_tokens: int,
        timeout: float,
    ) -> GatewayResponse:
        import asyncio
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

        resp = GatewayResponse(
            content=msg.get("content") or "",
            provider=provider.name,
            model=result.get("model", provider.model),
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
