"""Main-content extraction and focus-aware section selection for web_fetch."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow import web_tools


class _Response:
    def __init__(self, text, status=200, url="https://example.com/a",
                 ctype="text/html; charset=utf-8"):
        self.text = text
        self.content = text.encode("utf-8")
        self.status_code = status
        self.url = url
        self.headers = {"content-type": ctype}

    def json(self):
        return json.loads(self.text)


class _FakeCffi:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.response


ARTICLE_HTML = """
<html><body>
<nav><a>Home</a> <a>Products</a> <a>Subscribe</a> <a>Login</a></nav>
<header><h1>Example Blog</h1></header>
<main><article>
<h1>Kimi K3.1 Pro released</h1>
<p>Moonshot released Kimi K3.1 Pro on October 23, 2026. It costs $2 per million
input tokens and $10 per million output tokens, a third less than Kimi K3.</p>
<h2>Specs</h2>
<p>The model has 2.8 trillion total parameters, 104B active parameters, and a
1-million-token context window. The full weights are due by November 4.</p>
<h2>Benchmarks</h2>
<p>Moonshot claims improvements on coding, 3D creation, and financial research.
The company published benchmark charts comparing K3.1 Pro with Kimi K3.</p>
</article></main>
<footer>Copyright 2026 Example. Privacy Policy. Terms of Service.</footer>
</body></html>
"""

DOC_MD = """# Kimi K3.1 Pro release notes

Kimi K3.1 Pro is available today on the Kimi API and Kimi.com.

## Pricing

It costs $2 per million input tokens and $10 per million output tokens.

## Company history

Moonshot was founded in 2023 and raised several funding rounds from investors.
The company is based in Beijing and has offices in several cities.

## Benchmarks

Kimi K3.1 Pro scores higher on coding and agentic benchmarks than Kimi K3.
The published charts cover coding, 3D creation, finance, and cybersecurity.

## Unrelated appendix

This appendix lists the site's cookie policy, privacy policy, and legal notices.
It also repeats the footer navigation and the subscription call to action.
""" + ("Additional filler paragraph about legal notices. " * 20)


def _serve(monkeypatch, html, url="https://example.com/a"):
    monkeypatch.setattr(web_tools, "_cffi", _FakeCffi(_Response(html, url=url)))


def test_extraction_drops_navigation_and_footer():
    if web_tools._trafilatura_module() is None:
        pytest.skip("trafilatura not installed")
    extracted = web_tools._extract_main_content(ARTICLE_HTML)
    assert "Moonshot released Kimi K3.1 Pro" in extracted
    assert "2.8 trillion" in extracted
    assert "Privacy Policy" not in extracted
    assert "Subscribe" not in extracted


def test_extraction_is_useful_checks_size():
    assert not web_tools._extraction_is_useful("", "fallback text")
    assert not web_tools._extraction_is_useful("short", "x" * 5000)
    assert web_tools._extraction_is_useful(
        "article " * 100, "nav " * 500,
    )


def test_bm25_ranks_focus_section_first():
    docs = ["Kimi pricing per million tokens", "Company history and founders"]
    scores = web_tools._bm25_scores(docs, "price per million tokens")
    assert scores[0] > scores[1]


def test_select_sections_keeps_relevant_and_drops_rest():
    selected, kept, total = web_tools._select_sections(
        DOC_MD, "price per million tokens benchmarks", 600,
    )
    assert "$2 per million" in selected
    assert "benchmarks" in selected.lower()
    assert "founded in 2023" not in selected
    assert kept >= 2
    assert total == 5
    assert len(selected) <= 600


def test_select_sections_without_focus_match_keeps_top():
    selected, kept, total = web_tools._select_sections(
        DOC_MD, "quantum chromodynamics lattice", 400,
    )
    assert selected.startswith("# Kimi K3.1 Pro release notes")
    assert len(selected) <= 400
    assert total == 5


def test_web_fetch_uses_focus_selection(monkeypatch):
    monkeypatch.setenv("WINBROW_FETCH_FOCUS_CHARS", "600")
    _serve(monkeypatch, "<html><body>placeholder</body></html>")
    monkeypatch.setattr(web_tools, "_extract_main_content", lambda _html: DOC_MD)

    result = web_tools.web_fetch(
        "https://example.com/a", 2000, "price per million tokens",
    )
    assert result["success"] is True
    assert result["extractor"] == "trafilatura"
    assert result["focus"] == "price per million tokens"
    assert result["selected_sections"] >= 2
    assert result["total_sections"] == 5
    assert "$2 per million" in result["output"]
    assert "founded in 2023" not in result["output"]
    assert result["chars"] < result["raw_chars"]
    assert "SELECTED:" in result["output"]


def test_web_fetch_without_focus_returns_full_extract(monkeypatch):
    _serve(monkeypatch, "<html><body>placeholder</body></html>")
    monkeypatch.setattr(web_tools, "_extract_main_content", lambda _html: DOC_MD)

    result = web_tools.web_fetch("https://example.com/a", 40000)
    assert result["success"] is True
    assert result["selected_sections"] == 0
    assert result["chars"] == len(DOC_MD)
    assert "founded in 2023" in result["output"]


def test_web_fetch_falls_back_when_extraction_empty(monkeypatch):
    _serve(monkeypatch, "<html><body><p>Hello readable page</p></body></html>")
    monkeypatch.setattr(web_tools, "_extract_main_content", lambda _html: "")

    result = web_tools.web_fetch("https://example.com/a")
    assert result["success"] is True
    assert result["extractor"] == "html"
    assert "Hello readable page" in result["output"]


def test_web_fetch_reports_login_wall(monkeypatch):
    _serve(
        monkeypatch,
        "<html><body>You signed in with another tab or window. Reload to "
        "refresh your session.</body></html>",
        url="https://github.com/psf/requests",
    )
    result = web_tools.web_fetch("https://github.com/psf/requests")
    assert result["success"] is False
    assert result.get("wall") is True
    assert "browser_navigate" in result["output"]


def test_execute_web_tool_passes_focus(monkeypatch):
    calls = {}

    def fake_fetch(url, max_chars=None, focus=""):
        calls.update(url=url, max_chars=max_chars, focus=focus)
        return {"success": True}

    monkeypatch.setattr(web_tools, "web_fetch", fake_fetch)
    result = web_tools.execute_web_tool(
        "web_fetch", {"url": "https://x.test/a", "focus": "price", "max_chars": 5000},
    )
    assert result["success"] is True
    assert calls == {"url": "https://x.test/a", "max_chars": 5000, "focus": "price"}
