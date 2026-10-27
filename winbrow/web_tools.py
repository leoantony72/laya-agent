"""
Native web access tools — no browser, no tab, no CDP
====================================================
The old ``web_search`` was a seed PowerShell tool that called
``laya_ultrafast_browser``. That is the root cause of the browser opening for
research tasks. These tools make the first two web layers real:

  web_search(query, count, freshness) -> ranked titles, URLs, snippets
  web_fetch(url, max_chars)           -> readable text from one URL

Both use ``curl_cffi`` with a real browser TLS/HTTP2 fingerprint (DuckDuckGo
serves plain ``requests`` clients a bot challenge). No browser is opened.
Browser automation remains available for interaction tasks through the
browser_* skills.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup

try:
    # TLS/HTTP2 fingerprint impersonation. Plain ``requests`` gets an HTTP 202
    # bot challenge from DuckDuckGo; curl_cffi's browser handshake gets results.
    from curl_cffi import requests as _cffi
except Exception:  # missing wheel must never break app startup
    _cffi = None

_TRAFILATURA = None


def _trafilatura_module():
    """Lazy import (~750ms): only web_fetch needs the extractor."""
    global _TRAFILATURA
    if _TRAFILATURA is None:
        try:
            import trafilatura as module
        except Exception:
            return None
        _TRAFILATURA = module
    return _TRAFILATURA

from .registry import ArgSpec, Tool

WEB_IMPL = "web"
DEFAULT_SEARCH_COUNT = 5
MAX_SEARCH_COUNT = 10
DEFAULT_FETCH_CHARS = 12000
MAX_FETCH_CHARS = 40000
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
log = logging.getLogger("winbrow.web_tools")
if _cffi is None:
    log.warning(
        "curl_cffi is not installed; web_search will fail (pip install curl_cffi)"
    )

# Fingerprints tried in order when a request is challenged. A challenged
# identity is retried once after a short backoff before moving to the next.
_DEFAULT_IMPERSONATIONS = "chrome124,safari17_0"
_DEFAULT_SEARCH_TIMEOUT = 20.0
_SEARCH_ATTEMPTS = 3
_BLOCK_MARKERS = ("bots use duckduckgo", "anomaly", "captcha", "unusual traffic")

# Main-content extraction / focus selection for web_fetch.
_DEFAULT_FOCUS_CHARS = 6000
_WALL_MARKERS = (
    "you signed in with another tab or window",
    "please enable javascript",
    "enable javascript and cookies to continue",
    "verify you are human",
    "checking your browser before accessing",
    "just a moment...",
)
_HEADING_RE = re.compile(r"(?m)^(?=#{1,3}\s)")
_WORD_RE = re.compile(r"\w+", re.UNICODE)
_NUMERIC_RE = re.compile(
    r"[$€£¥%]"
    r"|\b\d[\d,.]*\s?(?:billion|million|trillion|thousand|B|GB|MB|KB|K|k|tokens?|params?|t/s)\b"
    r"|\b(?:19|20)\d\d\b"
)
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the "
    "to was were will with this these those".split()
)


def _impersonations() -> tuple[str, ...]:
    """Read at call time: server.py loads .env after module import."""
    raw = os.environ.get("WINBROW_TLS_IMPERSONATE", _DEFAULT_IMPERSONATIONS)
    names = tuple(part.strip() for part in raw.split(",") if part.strip())
    return names or tuple(_DEFAULT_IMPERSONATIONS.split(","))


def _search_budget() -> float:
    try:
        value = float(os.environ.get("WINBROW_SEARCH_TIMEOUT", _DEFAULT_SEARCH_TIMEOUT))
    except (TypeError, ValueError):
        value = _DEFAULT_SEARCH_TIMEOUT
    return max(5.0, min(value, 40.0))


def _is_blocked(status: int, body: str) -> bool:
    """True when the response is a bot challenge instead of real results."""
    if status in (202, 403, 429):
        return True
    head = (body or "")[:4000].lower()
    return any(marker in head for marker in _BLOCK_MARKERS)


def _search_get(url: str, params: dict[str, str], deadline: float):
    """GET a search page with browser TLS impersonation, retrying challenges.

    Returns (response, identity). Raises RuntimeError when every attempt is
    blocked, transport-fails, or the deadline expires.
    """
    if _cffi is None:
        raise RuntimeError(
            "curl_cffi is not installed (pip install curl_cffi); DuckDuckGo "
            "rejects plain requests with a bot challenge"
        )
    identities = _impersonations()
    last_error = "no attempt made"
    for attempt in range(_SEARCH_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            break
        identity = identities[attempt % len(identities)]
        try:
            response = _cffi.get(
                url,
                params=params,
                impersonate=identity,
                timeout=min(8.0, remaining),
                allow_redirects=True,
                # No manual User-Agent: curl_cffi sets headers matching the
                # impersonated browser. A mismatched UA is itself a signal.
            )
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if not _is_blocked(response.status_code, response.text):
                return response, identity
            last_error = f"HTTP {response.status_code} (bot challenge)"
        time.sleep(0.6 * (attempt + 1))
    raise RuntimeError(last_error)


def _int(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


def _ddg_redirect_url(href: str) -> str:
    """DuckDuckGo HTML results wrap URLs in /l/?uddg=<encoded>."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        query = parse_qs(parsed.query)
        target = (query.get("uddg") or [""])[0]
        if target:
            return unquote(target)
    return href


def _is_ad_url(href: str) -> bool:
    """Ads point at duckduckgo.com/y.js (Bing click tracker), not a page."""
    parsed = urlparse(href)
    return "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/y.js")


def _parse_ddg_html(html: str, limit: int) -> list[dict[str, str]]:
    soup = BeautifulSoup(html or "", "html.parser")
    results: list[dict[str, str]] = []
    for block in soup.select(".result, .web-result"):
        classes = " ".join(block.get("class") or [])
        if "result--ad" in classes or "result-sponsored" in classes:
            continue
        link = block.select_one(".result__a, .result-link")
        if link is None:
            continue
        title = link.get_text(" ", strip=True)
        href = _ddg_redirect_url(str(link.get("href") or ""))
        if _is_ad_url(href):
            continue
        snippet_node = block.select_one(".result__snippet, .result-snippet")
        snippet = snippet_node.get_text(" ", strip=True) if snippet_node else ""
        if title and href:
            results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= limit:
            break
    return results


def _parse_ddg_lite(html: str, limit: int) -> list[dict[str, str]]:
    soup = BeautifulSoup(html or "", "html.parser")
    results: list[dict[str, str]] = []
    links = soup.select("a.result-link")
    snippets = soup.select("td.result-snippet")
    for index, link in enumerate(links[:limit]):
        title = link.get_text(" ", strip=True)
        href = _ddg_redirect_url(str(link.get("href") or ""))
        if _is_ad_url(href):
            continue
        snippet = snippets[index].get_text(" ", strip=True) if index < len(snippets) else ""
        if title and href:
            results.append({"title": title, "url": href, "snippet": snippet})
    return results


def _search(
    query: str, count: int, freshness: str,
) -> tuple[bool, str, list[dict[str, str]], dict[str, str]]:
    params = {"q": query}
    freshness = (freshness or "").strip().lower()
    if freshness in {"day", "week", "month"}:
        params["df"] = {"day": "d", "week": "w", "month": "m"}[freshness]
    errors: list[str] = []
    transport = ""
    overall_deadline = time.monotonic() + _search_budget()
    per_endpoint = _search_budget() / 2
    for url, parser in (
        ("https://html.duckduckgo.com/html/", _parse_ddg_html),
        ("https://lite.duckduckgo.com/lite/", _parse_ddg_lite),
    ):
        deadline = min(overall_deadline, time.monotonic() + per_endpoint)
        try:
            response, identity = _search_get(url, params, deadline)
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            continue
        transport = f"curl_cffi:{identity}"
        results = parser(response.text, count)
        if results:
            lines = []
            for index, item in enumerate(results, start=1):
                lines.append(
                    f"{index}. {item['title']}\n"
                    f"   {item['url']}\n"
                    f"   {item['snippet']}".rstrip()
                )
            meta = {"provider": "duckduckgo", "transport": transport}
            return True, "\n\n".join(lines), results, meta
        errors.append(f"{url}: no results parsed")
    meta = {"provider": "", "transport": transport}
    return (
        False,
        "Web search is unavailable right now ("
        + "; ".join(errors[-2:])
        + "). Do not open the browser for research. Try web_fetch with a known "
        "URL or ask the user for the source.",
        [],
        meta,
    )


def web_search(query: str, count: Any = DEFAULT_SEARCH_COUNT,
               freshness: str = "") -> dict[str, Any]:
    query = str(query or "").strip()
    if not query:
        return {"success": False, "output": "web_search needs a non-empty query."}
    limit = _int(count, DEFAULT_SEARCH_COUNT, 1, MAX_SEARCH_COUNT)
    ok, output, results, meta = _search(query, limit, str(freshness or ""))
    return {
        "success": ok,
        "output": output,
        "query": query,
        "results": results,
        "count": len(results),
        **meta,
    }


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _focus_budget() -> int:
    """Character target when web_fetch is given a focus question."""
    try:
        value = int(os.environ.get("WINBROW_FETCH_FOCUS_CHARS", _DEFAULT_FOCUS_CHARS))
    except (TypeError, ValueError):
        value = _DEFAULT_FOCUS_CHARS
    return max(1500, min(value, MAX_FETCH_CHARS))


def _tokens(text: str) -> list[str]:
    return [
        word for word in _WORD_RE.findall((text or "").lower())
        if len(word) > 1 and word not in _STOPWORDS
    ]


def _bm25_scores(docs: list[str], query: str) -> list[float]:
    """Small dependency-free BM25 (k1=1.5, b=0.75) over sections."""
    tokenized = [_tokens(doc) for doc in docs]
    n = len(tokenized)
    if not n:
        return []
    terms = _tokens(query)
    if not terms:
        return [0.0] * n
    avgdl = sum(len(doc) for doc in tokenized) / n or 1.0
    df: dict[str, int] = {}
    for doc in tokenized:
        for term in set(doc):
            df[term] = df.get(term, 0) + 1
    k1, b = 1.5, 0.75
    scores: list[float] = []
    for doc in tokenized:
        counts: dict[str, int] = {}
        for term in doc:
            counts[term] = counts.get(term, 0) + 1
        score = 0.0
        for term in terms:
            tf = counts.get(term, 0)
            if not tf:
                continue
            idf = math.log(1 + (n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
            score += idf * (tf * (k1 + 1)) / (
                tf + k1 * (1 - b + b * len(doc) / avgdl)
            )
        scores.append(score)
    return scores


def _split_sections(text: str) -> list[str]:
    """Markdown sections split on #/##/### headings (preamble included)."""
    return [part.strip() for part in _HEADING_RE.split(text or "") if part.strip()]


def _fit_section(section: str, budget: int) -> str:
    """Trim one section to budget without cutting mid-word where possible."""
    section = (section or "").strip()
    if len(section) <= budget:
        return section
    if budget < 200:
        return ""
    cut = section[:budget]
    boundary = max(cut.rfind("\n\n"), cut.rfind("\n"), cut.rfind(". "))
    if boundary >= int(budget * 0.6):
        cut = cut[:boundary]
    return cut.rstrip()


def _fact_lines(
    sections: list[str], chosen: dict[int, str], focus: str, budget: int,
) -> list[str]:
    """Keep numeric/price/date lines from dropped sections when relevant."""
    if budget <= 0:
        return []
    terms = {term for term in _tokens(focus) if len(term) > 2}
    out: list[str] = []
    seen: set[str] = set()
    used = 0
    for index, section in enumerate(sections):
        if index in chosen:
            continue
        for line in section.splitlines():
            line = line.strip()
            if len(line) < 25:
                continue
            low = line.lower()
            if low in seen:
                continue
            if len(_NUMERIC_RE.findall(line)) < 2:
                continue
            if terms and not any(term in low for term in terms):
                continue
            if used + len(line) + 1 > budget:
                return out
            out.append(line)
            seen.add(low)
            used += len(line) + 1
    return out


def _select_sections(
    text: str, focus: str, max_chars: int,
) -> tuple[str, int, int]:
    """Keep the sections most relevant to ``focus`` (plus the intro).

    Returns (selected_text, kept_sections, total_sections).
    """
    text = (text or "").strip()
    sections = _split_sections(text)
    if len(sections) <= 1:
        return text[:max_chars], 1, 1
    scores = _bm25_scores(sections, focus)
    if not scores or max(scores) <= 0:
        # Focus terms appear nowhere: keep document order instead of guessing.
        return text[:max_chars], 0, len(sections)

    fact_budget = min(int(max_chars * 0.15), 1200)
    body_budget = max(400, max_chars - fact_budget)
    order = sorted(range(len(sections)), key=lambda index: scores[index], reverse=True)

    chosen: dict[int, str] = {}
    total = 0
    intro = _fit_section(sections[0], body_budget)
    if intro:
        chosen[0] = intro
        total = len(intro)
    for index in order:
        if index in chosen or scores[index] <= 0:
            continue
        remaining = body_budget - total - 2
        if remaining < 300:
            break
        piece = _fit_section(sections[index], remaining)
        if not piece:
            continue
        chosen[index] = piece
        total += len(piece) + 2

    extras = _fact_lines(
        sections, chosen, focus, min(fact_budget, max_chars - total),
    )
    body = "\n\n".join(chosen[i] for i in sorted(chosen))
    if extras:
        body += "\n\n## Additional relevant facts\n" + "\n".join(extras)
    return body[:max_chars], len(chosen), len(sections)


def _extract_main_content(html: str) -> str:
    """Trafilatura main-content Markdown; '' when unavailable or empty."""
    module = _trafilatura_module()
    if module is None or not html:
        return ""
    try:
        text = module.extract(
            html,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            include_links=False,
            include_images=False,
            favor_recall=True,
            deduplicate=True,
        )
    except Exception as exc:
        log.warning("trafilatura extraction failed: %s", exc)
        return ""
    return (text or "").strip()


def _extraction_is_useful(extracted: str, fallback: str) -> bool:
    if not extracted or len(extracted) < 200:
        return False
    if len(extracted) < 800 and len(extracted) < len(fallback) * 0.1:
        return False
    return True


def _looks_like_wall(text: str) -> bool:
    head = (text or "")[:600].lower()
    return any(marker in head for marker in _WALL_MARKERS)


def _pdf_to_text(raw: bytes) -> str:
    try:
        from .pdf_utils import extract_text

        text = extract_text(raw)
    except Exception as exc:
        return f"[PDF text extraction unavailable: {exc}]"
    if not text.strip():
        return "[PDF contained no extractable text]"
    return text


def web_fetch(url: str, max_chars: Any = DEFAULT_FETCH_CHARS,
              focus: str = "") -> dict[str, Any]:
    url = str(url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {"success": False, "output": "web_fetch needs a full http(s) URL."}
    limit = _int(max_chars, DEFAULT_FETCH_CHARS, 1000, MAX_FETCH_CHARS)
    focus = str(focus or "").strip()
    try:
        if _cffi is not None:
            # Impersonation also removes the 403s many sites serve to requests.
            response = _cffi.get(
                url,
                impersonate=_impersonations()[0],
                timeout=25,
                allow_redirects=True,
            )
        else:
            response = requests.get(
                url,
                headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
                timeout=25,
                allow_redirects=True,
            )
    except Exception as exc:
        return {"success": False, "output": f"web_fetch failed: {exc}"}
    if response.status_code >= 400:
        return {
            "success": False,
            "output": f"web_fetch returned HTTP {response.status_code} for {url}.",
            "url": url,
            "status": response.status_code,
        }

    content_type = str(response.headers.get("content-type") or "").lower()
    extractor = "raw"
    if "application/pdf" in content_type or parsed.path.lower().endswith(".pdf"):
        text = _pdf_to_text(response.content)
        extractor = "pdf"
    elif "json" in content_type:
        try:
            text = json.dumps(response.json(), indent=2, ensure_ascii=False)
        except Exception:
            text = response.text
        extractor = "json"
    elif "html" in content_type or "xml" in content_type or not content_type:
        fallback = _html_to_text(response.text)
        extracted = _extract_main_content(response.text)
        if _looks_like_wall(extracted) or (
            not extracted and _looks_like_wall(fallback)
        ):
            return {
                "success": False,
                "output": (
                    f"URL: {response.url}\n"
                    f"STATUS: {response.status_code}\n"
                    f"CONTENT-TYPE: {content_type or 'unknown'}\n---\n"
                    "This page is a login or JavaScript wall; web_fetch cannot "
                    "read it. Use browser_navigate for this URL."
                ),
                "url": str(response.url),
                "status": response.status_code,
                "content_type": content_type,
                "wall": True,
            }
        if _extraction_is_useful(extracted, fallback):
            text, extractor = extracted, "trafilatura"
        else:
            text, extractor = fallback, "html"
    else:
        text = response.text
        extractor = "text"

    raw_chars = len(text)
    selected_sections = 0
    total_sections = 0
    target = min(limit, _focus_budget())
    if extractor == "trafilatura" and focus and raw_chars > target:
        text, kept, total = _select_sections(text, focus, target)
        if total > 1:
            selected_sections, total_sections = kept, total

    total = len(text)
    clipped = text[:limit]
    if total > limit:
        clipped += f"\n\n[TRUNCATED: {total - limit} chars remain]"
    header_lines = [
        f"URL: {response.url}",
        f"STATUS: {response.status_code}",
        f"CONTENT-TYPE: {content_type or 'unknown'}",
        f"CHARS: {total}",
        f"EXTRACTOR: {extractor}",
        f"RAW-CHARS: {raw_chars}",
    ]
    if focus:
        header_lines.append(f"FOCUS: {focus}")
    if selected_sections:
        header_lines.append(f"SELECTED: {selected_sections}/{total_sections} sections")
    header = "\n".join(header_lines)
    return {
        "success": True,
        "output": header + "\n---\n" + clipped,
        "url": str(response.url),
        "status": response.status_code,
        "content_type": content_type,
        "chars": total,
        "truncated": total > limit,
        "extractor": extractor,
        "raw_chars": raw_chars,
        "focus": focus,
        "selected_sections": selected_sections,
        "total_sections": total_sections,
    }


def execute_web_tool(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    args = args or {}
    try:
        if tool_name == "web_search":
            return web_search(
                str(args.get("query") or ""),
                count=args.get("count", DEFAULT_SEARCH_COUNT),
                freshness=str(args.get("freshness") or ""),
            )
        if tool_name == "web_fetch":
            return web_fetch(
                str(args.get("url") or ""),
                max_chars=args.get("max_chars", DEFAULT_FETCH_CHARS),
                focus=str(args.get("focus") or ""),
            )
        return {"success": False, "output": f"Unknown web tool '{tool_name}'"}
    except Exception as exc:
        return {"success": False, "output": f"{tool_name} failed: {exc}"}


async def execute_web_tool_async(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(execute_web_tool, tool_name, args)


def get_web_tools() -> list[Tool]:
    return [
        Tool(
            name="web_search",
            description=(
                "Search the web via API and return ranked results with titles, "
                "URLs and snippets. No browser opened. Use for ALL research, "
                "comparison, product lookup, news, documentation search, and "
                "any question requiring current information. Always try this "
                "before considering the browser. Do not use for clicking or "
                "interaction."
            ),
            script="",
            impl=WEB_IMPL,
            args=[
                ArgSpec(
                    name="query", kind="text",
                    instructions="Search query, e.g. 'RTX 5090 price Flipkart'.",
                ),
                ArgSpec(
                    name="count", kind="text",
                    instructions="Number of results to return (1-10)",
                    default=str(DEFAULT_SEARCH_COUNT),
                ),
                ArgSpec(
                    name="freshness", kind="enum",
                    instructions="Optional recency filter: day, week, or month.",
                    criteria={"day": "last 24 hours", "week": "last 7 days", "month": "last 30 days"},
                    default="",
                ),
            ],
            speak="result",
            examples=[
                "research transformer architecture",
                "compare iPhone 16 and Galaxy S25",
                "latest Kimi K3 model details",
                "price of RTX 5090 on Flipkart",
            ],
        ),
        Tool(
            name="web_fetch",
            description=(
                "Fetch a specific URL and return its readable main content "
                "(navigation, ads, and boilerplate removed). No browser opened. "
                "Use after web_search when a result URL needs to be read in "
                "full, or when you know the exact URL of a public page. Pass "
                "focus with the exact facts you need to keep only the relevant "
                "sections. Does not work for login-required or JavaScript-heavy "
                "pages — use browser_navigate for those."
            ),
            script="",
            impl=WEB_IMPL,
            args=[
                ArgSpec(
                    name="url", kind="text",
                    instructions="Full http(s) URL to read.",
                ),
                ArgSpec(
                    name="max_chars", kind="text",
                    instructions="Maximum characters to return (1000-40000)",
                    default=str(DEFAULT_FETCH_CHARS),
                ),
                ArgSpec(
                    name="focus", kind="text",
                    instructions=(
                        "Optional: the question or facts this page must answer, "
                        "e.g. 'K3.1 Pro price, context window, benchmark scores'. "
                        "When set, only the most relevant sections are returned."
                    ),
                    default="",
                ),
            ],
            speak="result",
            examples=[
                "read that documentation page",
                "fetch the product listing",
                "read this URL",
            ],
        ),
    ]
