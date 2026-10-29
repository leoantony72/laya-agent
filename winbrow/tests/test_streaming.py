"""SSE streaming: reasoning/content deltas and the assembled response."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.brain.model_gateway import ModelGateway, Provider


class _FakeResponse:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    async def aread(self):
        return b""

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStream:
    def __init__(self, lines):
        self._lines = lines

    async def __aenter__(self):
        return _FakeResponse(self._lines)

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    lines: list[str] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, *args, **kwargs):
        return _FakeStream(list(_FakeClient.lines))


def _chunk(content="", reasoning="", finish=None):
    delta = {}
    if reasoning:
        delta["reasoning_content"] = reasoning
    if content:
        delta["content"] = content
    payload = {"choices": [{"delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(payload)}"


def test_stream_collects_deltas(monkeypatch):
    import httpx

    _FakeClient.lines = [
        _chunk(reasoning="look at "),
        _chunk(reasoning="the file"),
        _chunk(content='{"kind":'),
        _chunk(content=' "action"}', finish="stop"),
        "data: [DONE]",
    ]
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    gateway = ModelGateway.__new__(ModelGateway)
    provider = Provider(
        name="nebius", base_url="https://example.invalid/v1",
        api_key="k", model="m",
    )
    deltas: list[tuple[str, str]] = []
    response = asyncio.run(gateway._stream_provider(
        provider, {"model": "m", "messages": []}, {},
        10.0, lambda kind, text: deltas.append((kind, text)),
    ))

    assert response.content == '{"kind": "action"}'
    assert response.reasoning == "look at the file"
    assert response.finish_reason == "stop"
    assert deltas[0] == ("reasoning", "look at ")
    assert ("content", '{"kind":') in deltas


def test_stream_http_error_raises_gateway_error(monkeypatch):
    import httpx
    from winbrow.brain.model_gateway import GatewayError

    class _Bad(_FakeResponse):
        status_code = 429

        async def aread(self):
            return b'{"error":"rate limited"}'

    class _BadStream(_FakeStream):
        async def __aenter__(self):
            return _Bad([])

    class _BadClient(_FakeClient):
        def stream(self, *args, **kwargs):
            return _BadStream([])

    monkeypatch.setattr(httpx, "AsyncClient", _BadClient)
    gateway = ModelGateway.__new__(ModelGateway)
    provider = Provider(name="nebius", base_url="https://example.invalid/v1",
                        api_key="k", model="m")
    try:
        asyncio.run(gateway._stream_provider(provider, {}, {}, 5.0, lambda *a: None))
    except GatewayError as exc:
        assert "429" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected GatewayError")
