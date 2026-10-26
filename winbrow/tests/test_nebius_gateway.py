"""Nebius Token Factory provider selection and request translation."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.brain import model_gateway
from winbrow.brain.model_gateway import ModelGateway


def _clear(monkeypatch):
    for key in (
        "NEBIUS_API_KEY", "WINBROW_NEBIUS_API_KEY", "OPENROUTER_API_KEY",
        "OPENAI_API_KEY", "WINBROW_BRAIN_PROVIDER", "WINBROW_NEBIUS_MODEL",
        "NEBIUS_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)


def test_nebius_is_first_in_auto_when_key_present(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("NEBIUS_API_KEY", "nb-test")
    gateway = ModelGateway()
    provider = gateway.providers[0]
    assert provider.name == "nebius"
    assert provider.base_url == "https://api.tokenfactory.nebius.com/v1"
    assert provider.model == "deepseek-ai/DeepSeek-V4-Flash-0731"
    assert provider.api_key == "nb-test"


def test_nebius_model_and_base_are_env_configurable(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("NEBIUS_API_KEY", "nb-test")
    monkeypatch.setenv("WINBROW_NEBIUS_MODEL", "moonshotai/Kimi-K2.5")
    monkeypatch.setenv("WINBROW_NEBIUS_BASE", "https://nebius.example/v1")
    provider = ModelGateway().providers[0]
    assert provider.model == "moonshotai/Kimi-K2.5"
    assert provider.base_url == "https://nebius.example/v1"


def test_provider_preference_orders_nebius_and_openrouter(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("NEBIUS_API_KEY", "nb")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or")

    monkeypatch.setenv("WINBROW_BRAIN_PROVIDER", "openrouter")
    assert ModelGateway().providers[0].name == "openrouter"

    monkeypatch.setenv("WINBROW_BRAIN_PROVIDER", "nebius")
    assert ModelGateway().providers[0].name == "nebius"

    monkeypatch.delenv("WINBROW_BRAIN_PROVIDER")
    assert ModelGateway().providers[0].name == "nebius"


def test_nebius_payload_uses_reasoning_effort_not_openrouter_field(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("NEBIUS_API_KEY", "nb-test")
    gateway = ModelGateway()
    provider = gateway.providers[0]
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured.update(url=url, payload=payload, headers=headers, timeout=timeout)
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "model": provider.model,
            "usage": {},
        }

    monkeypatch.setattr(model_gateway, "_post_json", fake_post)
    response = asyncio.run(gateway._chat_with_provider(
        provider,
        [{"role": "user", "content": "hello"}],
        tools=None,
        json_schema={"type": "object"},
        temperature=0.2,
        max_tokens=1000,
        timeout=30,
        reasoning={"max_tokens": 400},
    ))

    assert response.content == "ok"
    assert captured["url"] == "https://api.tokenfactory.nebius.com/v1/chat/completions"
    assert captured["payload"]["reasoning_effort"] == "low"
    assert "provider" not in captured["payload"]
    assert captured["headers"]["Authorization"] == "Bearer nb-test"
