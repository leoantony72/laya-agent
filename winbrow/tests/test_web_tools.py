"""Web-access hierarchy, native web tools, and prompt consistency."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.agent import _is_browser_intent
from winbrow.brain.prompt import SYSTEM_PROMPT_STATIC, build_system_blocks
from winbrow.brain.skill_registry import load_skill_registry
from winbrow.registry import ToolRegistry
from winbrow.router import NEW_ACTION, WinBrowRouter
from winbrow import web_tools
from winbrow.web_tools import (
    _parse_ddg_html,
    _parse_ddg_lite,
    execute_web_tool,
    get_web_tools,
    web_fetch,
    web_search,
)
from winbrow.windows import WindowsContext


def test_duckduckgo_html_parser():
    html = """
    <div class="result">
      <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">Result A</a>
      <div class="result__snippet">Snippet A</div>
    </div>
    <div class="result">
      <a class="result__a" href="https://example.com/b">Result B</a>
      <div class="result__snippet">Snippet B</div>
    </div>
    """
    results = _parse_ddg_html(html, 2)
    assert results[0] == {
        "title": "Result A",
        "url": "https://example.com/a",
        "snippet": "Snippet A",
    }
    assert results[1]["url"] == "https://example.com/b"


def test_duckduckgo_lite_parser():
    html = """
    <a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fx">Lite A</a>
    <td class="result-snippet">Lite snippet</td>
    """
    results = _parse_ddg_lite(html, 1)
    assert results == [{
        "title": "Lite A",
        "url": "https://example.org/x",
        "snippet": "Lite snippet",
    }]


def test_duckduckgo_parsers_skip_ads():
    html = """
    <div class="result result--ad">
      <a class="result__a" href="//duckduckgo.com/y.js?ad_domain=shop.example">Ad result</a>
      <div class="result__snippet">Ad snippet</div>
    </div>
    <div class="result">
      <a class="result__a" href="https://example.com/real">Real result</a>
      <div class="result__snippet">Real snippet</div>
    </div>
    """
    assert [item["url"] for item in _parse_ddg_html(html, 5)] == ["https://example.com/real"]

    lite = """
    <a class="result-link" href="//duckduckgo.com/y.js?ad_domain=shop.example">Ad</a>
    <td class="result-snippet">Ad snippet</td>
    <a class="result-link" href="https://example.com/real">Real</a>
    <td class="result-snippet">Real snippet</td>
    """
    assert [item["url"] for item in _parse_ddg_lite(lite, 5)] == ["https://example.com/real"]


def test_web_search_and_fetch_validation_without_network():
    assert web_search("").get("success") is False
    assert web_fetch("not-a-url").get("success") is False
    assert execute_web_tool("nope", {}).get("success") is False


def test_web_tool_descriptions_are_browser_free():
    tools = {tool.name: tool for tool in get_web_tools()}
    assert "No browser opened" in tools["web_search"].description
    assert "No browser opened" in tools["web_fetch"].description
    assert "web_search" in tools["web_fetch"].description
    assert "clicking or interaction" in tools["web_search"].description


def test_browser_intent_rejects_research_and_keeps_interaction():
    research = [
        "research transformer architecture",
        "compare iPhone 16 and Galaxy S25",
        "latest Kimi K3 model details",
        "price of RTX 5090 on Flipkart",
        "search for cheap flights",
        "hotel prices in Goa",
        "search the web for async Python libraries",
        "compare github copilot and cursor",
    ]
    for text in research:
        assert not _is_browser_intent(text), text

    interaction = [
        "open twitter",
        "go to github.com/leoantony72/laya-agent",
        "fill out the contact form on example.com",
        "sign in to github",
        "add to cart",
    ]
    for text in interaction:
        assert _is_browser_intent(text), text


def test_prompt_hierarchy_order_and_worked_examples():
    prompt = SYSTEM_PROMPT_STATIC
    hierarchy = prompt.index("WEB ACCESS HIERARCHY")
    examples = prompt.index("WORKED EXAMPLES")
    assert hierarchy < examples
    assert "LAYER 1" in prompt and "LAYER 2" in prompt
    assert "LAYER 3" in prompt and "LAYER 4" in prompt
    assert "Could web_search or web_fetch return what I need?" in prompt
    assert "Browser research" not in prompt

    blocks = build_system_blocks("AVAILABLE SKILLS\nweb_search")
    assert blocks[1]["text"].startswith("AVAILABLE SKILLS")
    static = str(blocks[0]["text"])
    assert static.index("WORKED EXAMPLES") < len(static)
    assert static.rstrip().endswith("login wall + file upload.")


def test_web_skill_metadata_descriptions():
    registry = load_skill_registry(str(ROOT / "winbrow" / "skills"), sync_learned=False)
    assert "No browser opened" in registry["web_search"].description
    assert "No browser opened" in registry["web_fetch"].description
    browser = registry["browser_navigate"].description
    assert "ONLY when the task requires actual interaction" in browser
    assert "research" in browser.lower()


def test_router_uses_web_search_not_browser_for_research():
    registry = ToolRegistry()
    for tool in get_web_tools():
        registry.register(tool)
    router = WinBrowRouter.__new__(WinBrowRouter)
    router.registry = registry
    ctx = WindowsContext()
    import time

    def route(text):
        return router._heuristic_route(text, [], time.perf_counter(), ctx)

    assert route("search for cheap flights").tool.name == "web_search"
    assert route("book a flight to Delhi").tool.name == "laya_ultrafast_browser"
    assert route("research transformer architecture").kind == NEW_ACTION
    assert route("compare iPhone 16 and Galaxy S25").kind == NEW_ACTION
    assert route("read https://example.com").tool.name == "web_fetch"


def test_session_patterns_file():
    path = ROOT / ".claude" / "knowledge" / "session-patterns.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "ALWAYS web_search, NEVER browser" in text
    assert "Playing media → browser_navigate required" in text


# ---------------------------------------------------------------------------
# curl_cffi transport: challenge detection, retry, missing dependency
# ---------------------------------------------------------------------------

LITE_RESULTS_HTML = """
<a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fx">Lite A</a>
<td class="result-snippet">Lite snippet</td>
"""

DDG_CHALLENGE_HTML = """
<html><body>Unfortunately, bots use DuckDuckGo too.
Please complete the following challenge to confirm this search was made by a human.
Select all squares containing a duck.</body></html>
"""


class _FakeResponse:
    def __init__(self, status_code: int, text: str) -> None:
        self.status_code = status_code
        self.text = text


class _FakeCffi:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[str | None] = []

    def get(self, url, **kwargs):
        self.calls.append(kwargs.get("impersonate"))
        return self.responses.pop(0)


@pytest.fixture(autouse=True)
def _stable_search_env(monkeypatch):
    """Make fingerprint order, budget, and backoff deterministic."""
    monkeypatch.setenv("WINBROW_TLS_IMPERSONATE", "chrome124,safari17_0")
    monkeypatch.setenv("WINBROW_SEARCH_TIMEOUT", "20")
    monkeypatch.setattr(web_tools.time, "sleep", lambda _seconds: None)


def test_bot_challenge_detection():
    assert web_tools._is_blocked(202, "")
    assert web_tools._is_blocked(429, "<html>slow down</html>")
    assert web_tools._is_blocked(200, DDG_CHALLENGE_HTML)
    assert not web_tools._is_blocked(200, '<div class="result"><a class="result__a">ok</a></div>')


def test_search_get_retries_challenge_with_next_fingerprint(monkeypatch):
    fake = _FakeCffi([
        _FakeResponse(202, DDG_CHALLENGE_HTML),
        _FakeResponse(200, LITE_RESULTS_HTML),
    ])
    monkeypatch.setattr(web_tools, "_cffi", fake)
    response, identity = web_tools._search_get(
        "https://lite.duckduckgo.com/lite/", {"q": "x"}, time.monotonic() + 5,
    )
    assert response.status_code == 200
    assert identity == "safari17_0"
    assert fake.calls == ["chrome124", "safari17_0"]


def test_search_get_without_curl_cffi(monkeypatch):
    monkeypatch.setattr(web_tools, "_cffi", None)
    with pytest.raises(RuntimeError, match="curl_cffi is not installed"):
        web_tools._search_get(
            "https://html.duckduckgo.com/html/", {"q": "x"}, time.monotonic() + 5,
        )


def test_search_falls_back_to_lite_endpoint(monkeypatch):
    def fake_get(url, params, deadline):
        if "html.duckduckgo.com" in url:
            raise RuntimeError("HTTP 202 (bot challenge)")
        return _FakeResponse(200, LITE_RESULTS_HTML), "safari17_0"

    monkeypatch.setattr(web_tools, "_search_get", fake_get)
    ok, output, results, meta = web_tools._search("anything", 1, "")
    assert ok is True
    assert results == [{
        "title": "Lite A",
        "url": "https://example.org/x",
        "snippet": "Lite snippet",
    }]
    assert meta == {"provider": "duckduckgo", "transport": "curl_cffi:safari17_0"}
    assert "1. Lite A" in output


def test_search_failure_keeps_browser_free_guidance(monkeypatch):
    def fake_get(url, params, deadline):
        raise RuntimeError("HTTP 202 (bot challenge)")

    monkeypatch.setattr(web_tools, "_search_get", fake_get)
    result = web_tools.web_search("anything")
    assert result["success"] is False
    assert "Do not open the browser for research" in result["output"]
    assert result["count"] == 0


@pytest.mark.skipif(
    os.environ.get("WINBROW_LIVE_TESTS") != "1",
    reason="set WINBROW_LIVE_TESTS=1 to run live DuckDuckGo queries",
)
def test_live_web_search_returns_results():
    result = web_search("RTX 5090 price", 5)
    assert result["success"] is True, result["output"]
    assert result["count"] >= 5
    assert result["provider"] == "duckduckgo"
    assert result["transport"].startswith("curl_cffi:")
    assert not any("duckduckgo.com/l/" in item["url"] for item in result["results"])
