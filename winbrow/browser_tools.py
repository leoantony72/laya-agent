"""
Browser primitives for the System-2 browser loop
================================================
`browser_navigate`, `browser_read_page`, `browser_interact` and `browser_wait`
expose the browser as small, observable steps. The Brain planner can therefore
navigate, inspect what actually rendered, and act on the result instead of
asking the one-shot Laya engine to finish a whole research task in one call.

All four tools return the current page state (title, URL, visible text and
links) so later planning turns can consume values discovered earlier.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING, Any, Optional

from .ultrafast import LayaUltrafastEngine

if TYPE_CHECKING:
    from .browser_task import BrowserController

log = logging.getLogger("winbrow.browser_tools")

BROWSER_IMPL = "browser"

_MAX_PAGE_CHARS = 5000
_MAX_STATE_CHARS = 9000
_MAX_LINKS = 40
_MAX_WAIT_S = 10.0
_BROWSER_OP_TIMEOUT_S = 20.0

_PAGE_STATE_JS = r"""
(() => {
  const content = document.querySelector('main, [role="main"], article, #content');
  const bodyText = document.body ? (document.body.innerText || '') : '';
  const mainText = content ? (content.innerText || '') : '';
  const text = mainText.length > 200 ? mainText : bodyText;
  const seen = new Set();
  const links = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href || '';
    const label = (a.innerText || a.getAttribute('aria-label') || '').trim();
    if (!href.startsWith('http') || !label || seen.has(href)) continue;
    seen.add(href);
    links.push({text: label.slice(0, 160), href: href});
    if (links.length >= 200) break;
  }
  return {title: document.title || '', url: location.href || '', text, links};
})()
"""


def get_browser_tools():
    """Tool list registered into the ToolRegistry at init."""
    from .registry import ArgSpec, Tool

    return [
        Tool(
            name="browser_navigate",
            description=(
                "Open a URL in the browser, wait for it to render, and return "
                "the page title, URL, visible text and links. Use this to start "
                "a browser task or move to another page. It returns the new page "
                "state, so a separate browser_read_page call is not needed "
                "immediately after it."
            ),
            script="",
            impl=BROWSER_IMPL,
            args=[
                ArgSpec(
                    name="url", kind="text",
                    instructions="URL to open, e.g. https://github.com/trending/python",
                ),
                ArgSpec(
                    name="new_tab", kind="text",
                    instructions="'true' to open in a new tab, 'false' to reuse the active tab",
                    default="false",
                ),
            ],
            speak="result",
            examples=[
                "go to github trending python",
                "open the page",
                "navigate to the repository page",
            ],
        ),
        Tool(
            name="browser_read_page",
            description=(
                "Read the CURRENT browser page without navigating: title, URL, "
                "cleaned visible text and links. Use it to inspect or extract "
                "data from the page that is open, such as repository names, "
                "star counts, dates and descriptions. Reuse the URL from "
                "HISTORY if the browser's current URL is unknown."
            ),
            script="",
            impl=BROWSER_IMPL,
            args=[
                ArgSpec(
                    name="url", kind="text",
                    instructions="Optional URL to report/fetch if the current tab URL is unknown",
                    default="",
                ),
                ArgSpec(
                    name="max_chars", kind="text",
                    instructions="Maximum page-text characters to return (1000-5000)",
                    default="5000",
                ),
            ],
            speak="result",
            examples=[
                "read the page",
                "extract the repositories from this page",
                "what does the page say",
            ],
        ),
        Tool(
            name="browser_interact",
            description=(
                "Perform ONE interaction on the current page: click a link or "
                "button, type into a field, submit a search, or scroll. Prefer "
                "passing a CSS selector and (for typing) a value when known; "
                "otherwise give a natural-language instruction and Laya will "
                "choose the element. Returns the resulting page state."
            ),
            script="",
            impl=BROWSER_IMPL,
            args=[
                ArgSpec(
                    name="instruction",
                    kind="text",
                    instructions="What to do on the current page, e.g. 'click the Python language filter'",
                ),
                ArgSpec(
                    name="selector", kind="text",
                    instructions="Optional CSS selector of the element to click/type into",
                    default="",
                ),
                ArgSpec(
                    name="value", kind="text",
                    instructions="Optional text to type into the selector",
                    default="",
                ),
            ],
            speak="result",
            examples=[
                "click the Python filter",
                "type python into the search box",
                "submit the search",
            ],
        ),
        Tool(
            name="browser_wait",
            description=(
                "Wait for the current page to load or update, then return its "
                "state. Use after an interaction when the page text still looks "
                "stale, or when content loads asynchronously."
            ),
            script="",
            impl=BROWSER_IMPL,
            args=[
                ArgSpec(
                    name="seconds", kind="text",
                    instructions="Seconds to wait (0-10)",
                    default="1",
                ),
            ],
            speak="result",
            examples=["wait for the page to load", "wait a second"],
        ),
    ]


def _normalise_url(raw: str) -> str:
    url = str(raw or "").strip().strip("\"'")
    if not url:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    return url


def _int_arg(args: dict, name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(float(str(args.get(name, default)).strip()))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"[ \t]+\n", "\n", str(text or ""))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n... [truncated {len(text) - limit} chars]"


def _render_state(state: dict, max_chars: int) -> str:
    title = str(state.get("title") or "").strip()
    url = str(state.get("url") or "").strip()
    text = _clip(state.get("text") or "", max_chars)
    links = state.get("links") or []

    lines = [f"Title: {title or '(untitled)'}", f"URL: {url or '(unknown)'}",
             "", "PAGE TEXT:", text or "(empty)"]
    rendered_links = []
    for link in links[:_MAX_LINKS]:
        if not isinstance(link, dict):
            continue
        label = re.sub(r"\s+", " ", str(link.get("text") or "")).strip()
        href = str(link.get("href") or "").strip()
        if label and href:
            rendered_links.append(f"- {label[:120]} -> {href}")
    if rendered_links:
        lines.extend(["", "LINKS:"])
        lines.extend(rendered_links)
    return _clip("\n".join(lines), _MAX_STATE_CHARS)


async def _page_state(browser: "BrowserController", max_chars: int) -> tuple[dict, str]:
    result = await browser.execute_js(_PAGE_STATE_JS)
    if result.get("success"):
        value = result.get("value")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = {"text": value}
        if isinstance(value, dict):
            state = {
                "title": str(value.get("title") or ""),
                "url": str(value.get("url") or ""),
                "text": str(value.get("text") or ""),
                "links": value.get("links") or [],
            }
            if state["text"] or state["title"] or state["url"] or state["links"]:
                return state, "cdp"

    fallback = await browser.read_page_text()
    if fallback.get("success"):
        state = {
            "title": "",
            "url": "",
            "text": str(fallback.get("text") or ""),
            "links": [],
        }
        try:
            current = await browser.get_current_url()
            state["url"] = str(current.get("url") or "")
        except Exception:
            pass
        return state, "read_page_text"

    error = result.get("error") or fallback.get("error") or "Could not read the page"
    return {"error": str(error)}, "error"


async def _state_output(browser: "BrowserController", url: str = "",
                        max_chars: int = _MAX_PAGE_CHARS) -> dict[str, Any]:
    state, method = await _page_state(browser, max_chars)
    if state.get("error"):
        return {
            "success": False,
            "output": f"Could not read the page: {state['error']}",
            "method": method,
        }
    if url and not state.get("url"):
        state["url"] = url
    return {
        "success": True,
        "output": _render_state(state, max_chars),
        "url": state.get("url", ""),
        "title": state.get("title", ""),
        "method": method,
    }


async def _navigate(browser: "BrowserController", args: dict) -> dict[str, Any]:
    url = _normalise_url(args.get("url") or args.get("href") or args.get("target") or "")
    if not url:
        return {"success": False, "output": "No URL provided for browser_navigate."}

    raw_new_tab = str(args.get("new_tab", "false")).strip().lower()
    new_tab = raw_new_tab not in ("false", "0", "no", "off")
    try:
        navigation = await browser.navigate_to(url, new_tab=new_tab)
    except Exception as exc:
        return {"success": False, "output": f"Navigation failed: {exc}"}

    if not navigation.get("success", True):
        detail = navigation.get("error") or navigation.get("stderr") or navigation.get("stdout")
        return {
            "success": False,
            "output": f"Could not open {url}: {detail or 'navigation failed'}",
            "navigation": navigation,
        }

    await asyncio.sleep(1.0)
    state = await _state_output(browser, url=url)
    if new_tab and (
        not state.get("success")
        or str(state.get("url") or "").startswith("about:")
        or not state.get("title")
    ):
        # Some Chrome builds create the new target as about:blank and ignore
        # the URL on /json/new. The controller now remembers the new tab, so
        # navigate it explicitly.
        try:
            again = await browser.navigate_to(url, new_tab=False)
            if again.get("success", True):
                await asyncio.sleep(1.0)
                state = await _state_output(browser, url=url)
        except Exception:
            pass
    state["navigation"] = navigation
    if state.get("success"):
        state["output"] = f"Navigated to {state.get('url') or url}\n\n{state['output']}"
    return state


async def _read(browser: "BrowserController", args: dict) -> dict[str, Any]:
    max_chars = _int_arg(args, "max_chars", _MAX_PAGE_CHARS, 1000, _MAX_PAGE_CHARS)
    url = _normalise_url(args.get("url") or "")
    return await _state_output(browser, url=url, max_chars=max_chars)


async def _interact(browser: "BrowserController", args: dict) -> dict[str, Any]:
    instruction = str(args.get("instruction") or args.get("goal") or "").strip()
    selector = str(args.get("selector") or "").strip()
    value = str(args.get("value") or "")

    action = ""
    result: dict[str, Any] = {}
    if selector:
        try:
            if value:
                action = "type"
                result = await browser.type_in_element(selector, value)
            else:
                action = "click"
                result = await browser.click_element(selector)
        except Exception as exc:
            return {"success": False, "output": f"Browser interaction failed: {exc}"}
    else:
        if not instruction:
            return {
                "success": False,
                "output": "browser_interact needs an instruction or a selector.",
            }
        engine = LayaUltrafastEngine(browser)
        try:
            harvest = await engine.harvest_elements()
            elements = harvest.get("elements") or []
            if harvest.get("error") or not elements:
                return {
                    "success": False,
                    "output": (
                        "Could not find interactive elements on the current page"
                        + (f": {harvest.get('error')}" if harvest.get("error") else ".")
                    ),
                }
            step = await engine.decide_with_laya(
                instruction, harvest.get("url", ""), harvest.get("title", ""), elements, [],
            )
            if step is None:
                step = engine.make_typed_decision(
                    instruction, harvest.get("url", ""), harvest.get("title", ""), elements, [],
                )
            if step is None:
                return {"success": False, "output": "No browser action could be chosen."}
            action = step.action
            if action == "finish":
                state = await _state_output(browser)
                state["output"] = (
                    "No action was needed; the page already matches the instruction.\n\n"
                    + state.get("output", "")
                )
                return state
            result = await engine.execute_step(step)
        except Exception as exc:
            return {"success": False, "output": f"Browser interaction failed: {exc}"}

    await asyncio.sleep(0.6)
    state = await _state_output(browser)
    detail = result.get("output") or result.get("error") or ""
    state["output"] = (
        f"Action: {action or 'interact'}"
        + (f" ({detail})" if detail else "")
        + "\n\n"
        + state.get("output", "")
    )
    state["success"] = bool(result.get("success", True)) and state.get("success", False)
    state["action"] = action
    return state


async def _wait(browser: "BrowserController", args: dict) -> dict[str, Any]:
    try:
        seconds = float(str(args.get("seconds", "1")).strip())
    except (TypeError, ValueError):
        seconds = 1.0
    seconds = max(0.0, min(_MAX_WAIT_S, seconds))
    if seconds:
        await asyncio.sleep(seconds)
    return await _state_output(browser)


async def execute_browser_tool(tool_name: str, args: dict,
                               browser: Optional["BrowserController"] = None) -> dict[str, Any]:
    """Execute one browser primitive against the agent's BrowserController."""
    if browser is None:
        return {"success": False, "output": "No browser controller available."}
    if tool_name == "browser_navigate":
        operation = _navigate(browser, args)
    elif tool_name == "browser_read_page":
        operation = _read(browser, args)
    elif tool_name == "browser_interact":
        operation = _interact(browser, args)
    elif tool_name == "browser_wait":
        operation = _wait(browser, args)
    else:
        return {"success": False, "output": f"Unknown browser op '{tool_name}'"}

    try:
        return await asyncio.wait_for(operation, timeout=_BROWSER_OP_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {
            "success": False,
            "timeout": True,
            "output": (
                f"Browser operation timed out after {_BROWSER_OP_TIMEOUT_S:.0f}s. "
                "The browser may not be reachable over CDP. Retry, or enable "
                "remote debugging / use mcp_fetch as a read-only fallback."
            ),
        }
