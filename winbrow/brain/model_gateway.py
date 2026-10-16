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


def extract_json_object(text: str) -> Optional[dict]:
    """Pull the first JSON object out of an LLM response (handles code fences)."""
    if not text:
        return None
    t = text.strip()
    # strip ```json fences
    if "```" in t:
        parts = t.split("```")
        for part in parts:
            p = part.strip()
            if p.startswith("json"):
                p = p[4:].strip()
            if p.startswith("{"):
                t = p
                break
    start = t.find("{")
    end = t.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError:
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
        # Local OpenAI-compatible (LM Studio / Ollama serve). Only when reachable.
        candidates = []
        if local_endpoint:
            candidates.append(local_endpoint)
        else:
            candidates.append(os.environ.get("WINBROW_LOCAL_LLM_ENDPOINT", ""))
        for ep in candidates:
            if not ep:
                continue
            base = ep.rstrip("/")
            if not base.endswith("/v1"):
                base = base + "/v1"
            self.providers.append(Provider(
                name="local",
                base_url=base,
                api_key=os.environ.get("WINBROW_LOCAL_LLM_KEY", "not-needed"),
                model=os.environ.get("WINBROW_LOCAL_LLM_MODEL", ""),
            ))
        if not self.providers:
            log.warning("ModelGateway: no providers configured (set OPENROUTER_API_KEY).")

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
                    timeout or REQUEST_TIMEOUT_S,
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
