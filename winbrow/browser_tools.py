"""
Browser primitives for the System-2 browser loop
================================================
`browser_navigate`, `browser_read_full`, `browser_read_page`,
`browser_interact`, `browser_wait` and `browser_upload_file` expose the browser
as small, observable steps. The
Brain planner can therefore navigate, inspect what actually rendered, and act
on the result instead of asking the one-shot Laya engine to finish a whole
research task in one call.

All state-reading tools return the current page state (title, URL, visible text
and links) so later planning turns can consume values discovered earlier.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
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
# The accessibility tree is capped by CHARACTERS, not by element count, so a
# dense page cannot cut off the control the brain needs before it appears.
_MAX_TREE_CHARS = 6000
_MAX_READ_FULL_TEXT_CHARS = 3000
_MAX_READ_FULL_TREE_CHARS = 3000

_A11Y_JS = r"""
(() => {
  const out = [];
  document.querySelectorAll('[data-winbrow-id]').forEach(
    el => el.removeAttribute('data-winbrow-id'));
  const implicitRole = {
    a: 'link', button: 'button', input: 'textbox', textarea: 'textbox',
    select: 'combobox', option: 'option', img: 'img', form: 'form',
    nav: 'navigation', main: 'main', header: 'banner', footer: 'contentinfo'
  };
  const nodes = document.querySelectorAll(
    'a[href], button, input, textarea, select, [role], [aria-label], ' +
    '[onclick], [contenteditable="true"], summary, label'
  );
  let n = 0;
  for (const el of nodes) {
    const tag = (el.tagName || '').toLowerCase();
    const isInput = tag === 'input' || tag === 'textarea' || tag === 'select';
    const isFile = tag === 'input' && (el.type || '').toLowerCase() === 'file';
    const r = el.getBoundingClientRect();
    const css = getComputedStyle(el);
    if (!isFile && (r.width < 2 || r.height < 2)) continue;
    if (!isFile && (css.visibility === 'hidden' || css.display === 'none')) continue;
    const assocLabel = el.labels && el.labels.length
      ? (el.labels[0].innerText || el.labels[0].textContent || '') : '';
    const raw = el.getAttribute('aria-label') || el.getAttribute('title') ||
                el.getAttribute('placeholder') || assocLabel ||
                (isFile ? (el.getAttribute('name') || 'file input')
                        : (isInput ? (el.value || '') : el.innerText)) || '';
    const label = raw.replace(/\s+/g, ' ').trim().slice(0, 120);
    if (!label) continue;
    const ref = 'w' + n;
    el.setAttribute('data-winbrow-id', ref);
    out.push({
      ref: ref,
      role: el.getAttribute('role') || implicitRole[tag] || tag,
      label: label,
      tag: tag,
      type: el.getAttribute('type') || '',
      href: el.getAttribute('href') || '',
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      selector: '[data-winbrow-id="' + ref + '"]'
    });
    n++;
  }
  return {
    title: document.title || '',
    url: location.href || '',
    elements: out
  };
})()
"""

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
                "Open a URL in the agent's own dedicated browser tab, wait for "
                "it to render, and return the page title, URL, visible text and "
                "links. The first navigation of a task creates the agent tab; "
                "later calls reuse it. It returns the new page state, so a "
                "separate browser_read_page call is not needed immediately "
                "after it."
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
                    instructions="'true' to force a fresh agent tab, 'false' to reuse the agent tab",
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
                "Read only the CURRENT page's title, URL, visible text and "
                "links. Use it to re-check text after browser_read_full already "
                "ran, or when interactive elements are not needed. Prefer "
                "browser_read_full as the first read on an unfamiliar page."
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
            name="browser_read_full",
            description=(
                "Read the current page text AND interactive elements in one "
                "call. Use as the first step on any unfamiliar page before "
                "interacting. Returns title, URL, visible text, links, and a "
                "filtered accessibility tree. Replaces calling "
                "browser_read_page and browser_get_accessibility_tree "
                "separately. Pass target= to filter elements by label."
            ),
            script="",
            impl=BROWSER_IMPL,
            args=[
                ArgSpec(
                    name="target", kind="text",
                    instructions="Optional label/role filter, e.g. 'upload' or 'search'",
                    default="",
                ),
                ArgSpec(
                    name="max_text_chars", kind="text",
                    instructions="Maximum page-text characters (1000-5000)",
                    default="3000",
                ),
                ArgSpec(
                    name="max_tree_chars", kind="text",
                    instructions="Maximum accessibility-tree characters (1000-6000)",
                    default="3000",
                ),
            ],
            speak="result",
            examples=[
                "read the page and show what I can click",
                "find the upload control and read the page",
                "inspect this page before acting",
            ],
        ),
        Tool(
            name="browser_get_accessibility_tree",
            description=(
                "List interactive elements with roles, labels and selectors. "
                "Use when browser_read_full already ran but you need to update "
                "the element list without re-reading page text. Pass target= "
                "to return only elements matching a label (upload, submit, "
                "search etc). Never call this immediately after "
                "browser_read_full on the same unchanged page — it returns the "
                "same data."
            ),
            script="",
            impl=BROWSER_IMPL,
            args=[
                ArgSpec(
                    name="target", kind="text",
                    instructions="Optional label/role filter, case-insensitive",
                    default="",
                ),
                ArgSpec(
                    name="max_chars", kind="text",
                    instructions="Maximum tree characters to return (1000-12000)",
                    default="6000",
                ),
            ],
            speak="result",
            examples=[
                "show the buttons on this page",
                "find the upload button",
                "what can I click here",
            ],
        ),
        Tool(
            name="browser_upload_file",
            description=(
                "Set a file on the active page's file input via CDP "
                "DOM.setFileInputFiles, bypassing the OS file picker dialog. "
                "Call this AFTER clicking the button that opens file selection. "
                "Use it for every upload (Aspose, Google Drive, OneDrive, any "
                "site). Call browser_wait before reading the page."
            ),
            script="",
            impl=BROWSER_IMPL,
            args=[
                ArgSpec(
                    name="file_path", kind="text",
                    instructions="Absolute path of the file to upload, e.g. C:\\Docs\\report.pptx",
                ),
            ],
            speak="result",
            examples=[
                "upload the file",
                "set the report on the file input",
            ],
        ),
        Tool(
            name="browser_interact",
            description=(
                "Perform exactly one click, type or submit. Pass selector= "
                "directly if you already have it from a prior accessibility "
                "tree call — this skips the internal tree walk and runs faster. "
                "Never use for file upload — use browser_upload_file instead. "
                "Returns the resulting page state."
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
                    instructions="Text to type into the selected element",
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


def _filter_elements(elements: list, target: str) -> list:
    """Keep only nodes whose role or label contains ``target``."""
    needle = str(target or "").strip().lower()
    if not needle:
        return elements
    matched = []
    for el in elements:
        if not isinstance(el, dict):
            continue
        label = str(el.get("label") or "").lower()
        role = str(el.get("role") or "").lower()
        if needle in label or needle in role:
            matched.append(el)
    return matched


def _render_elements(elements: list, max_chars: int) -> tuple[str, bool]:
    """Render interactive elements, truncating by characters not count."""
    lines: list[str] = []
    for el in elements:
        if not isinstance(el, dict):
            continue
        label = re.sub(r"\s+", " ", str(el.get("label") or "")).strip()
        if not label:
            continue
        role = str(el.get("role") or el.get("tag") or "element")
        selector = str(el.get("selector") or "")
        extras = []
        if el.get("type"):
            extras.append(f"type={el['type']}")
        if el.get("href"):
            extras.append(f"href={str(el['href'])[:100]}")
        if el.get("disabled"):
            extras.append("disabled")
        suffix = (" (" + ", ".join(extras) + ")") if extras else ""
        lines.append(f"- {role} \"{label[:100]}\"{suffix} -> selector {selector}")

    text = "\n".join(lines)
    if len(text) > max_chars:
        cut = text[:max_chars].rstrip()
        text = cut + "\n... [truncated]"
        return text, True
    return text, False


async def _page_state(browser: "BrowserController", max_chars: int,
                      force_refresh: bool = False) -> tuple[dict, str]:
    # BrowserController.get_page_state caches the snapshot while the URL is
    # unchanged, so a read immediately after navigate/interact/wait is free.
    getter = getattr(browser, "get_page_state", None)
    if callable(getter):
        state = await getter(force_refresh=force_refresh)
        if state.get("error"):
            return {"error": str(state["error"])}, "error"
        method = str(state.get("method") or "cdp")
        state = {
            "title": str(state.get("title") or ""),
            "url": str(state.get("url") or ""),
            "text": str(state.get("text") or ""),
            "links": state.get("links") or [],
        }
        return state, method

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
                        max_chars: int = _MAX_PAGE_CHARS,
                        force_refresh: bool = False) -> dict[str, Any]:
    state, method = await _page_state(browser, max_chars, force_refresh=force_refresh)
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
    state = await _state_output(browser, url=url, force_refresh=True)
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
                state = await _state_output(browser, url=url, force_refresh=True)
        except Exception:
            pass
    state["navigation"] = navigation
    if navigation.get("tab"):
        state["tab"] = navigation["tab"]
    if state.get("success"):
        state["output"] = f"Navigated to {state.get('url') or url}\n\n{state['output']}"
    return state


async def _read(browser: "BrowserController", args: dict) -> dict[str, Any]:
    max_chars = _int_arg(args, "max_chars", _MAX_PAGE_CHARS, 1000, _MAX_PAGE_CHARS)
    url = _normalise_url(args.get("url") or "")
    return await _state_output(
        browser, url=url, max_chars=max_chars, force_refresh=False)


def _parse_tree_value(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


async def _read_full(browser: "BrowserController", args: dict) -> dict[str, Any]:
    """Return page text and interactive elements from one parallel read."""
    target = str(args.get("target") or args.get("query") or "").strip()
    max_text = _int_arg(
        args, "max_text_chars", _MAX_READ_FULL_TEXT_CHARS,
        1000, _MAX_PAGE_CHARS)
    max_tree = _int_arg(
        args, "max_tree_chars", _MAX_READ_FULL_TREE_CHARS,
        1000, _MAX_TREE_CHARS)

    state_task = _page_state(browser, max_text, force_refresh=False)
    tree_task = browser.execute_js(_A11Y_JS)
    (state, method), tree_result = await asyncio.gather(state_task, tree_task)

    tree_value = _parse_tree_value(
        tree_result.get("value") if tree_result.get("success") else None)
    if state.get("error") and not tree_value:
        return {
            "success": False,
            "output": f"Could not read the page: {state['error']}",
            "method": method,
        }

    raw_text = str(state.get("text") or "")
    text = _clip(raw_text, max_text)
    links = state.get("links") or []
    elements = tree_value.get("elements") or []
    matched = _filter_elements(elements, target)
    tree_text, tree_truncated = _render_elements(matched, max_tree)
    title = str(state.get("title") or tree_value.get("title") or "")
    url = str(state.get("url") or tree_value.get("url") or "")

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
    lines.extend([
        "",
        "INTERACTIVE ELEMENTS:"
        + (f" matching {target!r}" if target else ""),
        tree_text or "(no interactive elements found)",
    ])

    result: dict[str, Any] = {
        "success": True,
        "output": _clip("\n".join(lines), _MAX_STATE_CHARS),
        "url": url,
        "title": title,
        "text": text,
        "links": links[:_MAX_LINKS],
        "elements": matched,
        "element_count": len(matched),
        "method": method,
    }
    if target:
        result["target"] = target
    if len(raw_text) > max_text:
        result["_truncated_text"] = True
    if tree_truncated:
        result["_truncated_tree"] = True
        result["_truncated"] = True
    return result


_ACTION_PREFIX_RE = re.compile(
    r"^\s*(?:click|press|tap|hit|open|select|choose|type|fill|enter|submit|"
    r"upload|set)\s+(?:on\s+|the\s+|a\s+|an\s+)?", re.I)


def _pick_element(instruction: str, elements: list) -> Optional[dict]:
    """Match a natural-language instruction to a visible element by label.

    Deterministic, no model call: 'click the Upload button' matches an element
    whose accessible name is 'Upload'. Returns None when the match is weak so
    the caller can fall back to the existing Laya decision path.
    """
    text = _ACTION_PREFIX_RE.sub("", instruction or "").strip().lower()
    text = re.sub(r"\b(?:button|link|field|box|input|menu|item|tab)\b", "", text)
    text = re.sub(r"[\"'“”]", "", text).strip()
    if not text:
        return None
    tokens = {t for t in re.split(r"[^a-z0-9]+", text) if len(t) > 1}
    best: Optional[dict] = None
    best_score = 0.0
    for el in elements:
        if not isinstance(el, dict):
            continue
        label = re.sub(r"\s+", " ", str(el.get("label") or "")).strip().lower()
        if not label:
            continue
        if label in text:
            score = 1.0
        else:
            label_tokens = {t for t in re.split(r"[^a-z0-9]+", label) if len(t) > 1}
            if not label_tokens:
                continue
            overlap = len(tokens & label_tokens) / len(label_tokens)
            score = overlap if overlap >= 0.6 else 0.0
        if score > best_score:
            best_score = score
            best = el
    return best if best_score >= 0.6 else None


async def _accessibility_tree(browser: "BrowserController", args: dict) -> dict[str, Any]:
    """DOM-derived interactive-element snapshot (no OCR, no screenshots).

    ``target`` filters the tree by role/label in a single call, so the brain
    gets the exact control instead of the whole page and a retry. The only
    output cap is ``max_chars`` (characters, not elements).
    """
    target = str(args.get("target") or args.get("query") or "").strip()
    max_chars = _int_arg(args, "max_chars", _MAX_TREE_CHARS, 1000, 12000)
    result = await browser.execute_js(_A11Y_JS)
    if not result.get("success"):
        return {
            "success": False,
            "output": f"Could not read the accessibility tree: {result.get('error') or 'CDP unavailable'}",
        }
    value = _parse_tree_value(result.get("value"))
    elements = value.get("elements") or []
    matched = _filter_elements(elements, target)
    title = str(value.get("title") or "")
    url = str(value.get("url") or "")
    tree_text, truncated = _render_elements(matched, max_chars)
    header = "INTERACTIVE ELEMENTS:"
    if target:
        header += f" matching {target!r}"
    lines = [f"Title: {title or '(untitled)'}", f"URL: {url or '(unknown)'}", "",
             header, tree_text or "(no interactive elements found)"]
    output: dict[str, Any] = {
        "success": True,
        "output": _clip("\n".join(lines), _MAX_STATE_CHARS),
        "url": url,
        "title": title,
        "element_count": len(matched),
        "total_elements": len(elements),
    }
    if target:
        output["target"] = target
    if truncated:
        output["_truncated"] = True
    return output


async def _type_or_upload(browser: "BrowserController", selector: str,
                          value: str) -> tuple[str, dict[str, Any]]:
    """Type into a text field, or set files when the target is a file input."""
    try:
        if await browser.is_file_input(selector):
            return "upload", await browser.set_file_input(selector, value)
    except Exception as exc:
        log.debug(f"File-input probe failed for {selector}: {exc}")
    return "type", await browser.type_in_element(selector, value)


async def _interact(browser: "BrowserController", args: dict) -> dict[str, Any]:
    instruction = str(args.get("instruction") or args.get("goal") or "").strip()
    selector = str(args.get("selector") or "").strip()
    value = str(args.get("value") or "")

    action = ""
    result: dict[str, Any] = {}
    if selector:
        try:
            if value:
                action, result = await _type_or_upload(browser, selector, value)
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
        # Search-before-act: try the DOM accessibility snapshot first. This
        # turns "click Upload" into a selector deterministically, with no
        # screenshot/OCR and no extra model call.
        try:
            snap = await browser.execute_js(_A11Y_JS)
            snap_value = snap.get("value")
            if isinstance(snap_value, str):
                try:
                    snap_value = json.loads(snap_value)
                except ValueError:
                    snap_value = None
            elements = (snap_value or {}).get("elements") or [] \
                if isinstance(snap_value, dict) else []
            match = None
            if value and elements:
                wants_file = not instruction or re.search(
                    r"\b(?:upload|attach|choose|select|file)\b", instruction, re.I)
                file_inputs = [
                    el for el in elements
                    if isinstance(el, dict)
                    and str(el.get("type") or "").lower() == "file"
                ]
                if wants_file and file_inputs:
                    match = file_inputs[0]
            if match is None:
                match = _pick_element(instruction, elements)
            if match:
                selector = str(match.get("selector") or "")
                if selector:
                    if value:
                        action, result = await _type_or_upload(browser, selector, value)
                    else:
                        action = "click"
                        result = await browser.click_element(selector)
                    result.setdefault("output", f"{action} {match.get('label', '')!r}")
        except Exception as exc:
            log.debug(f"Accessibility interaction match failed: {exc}")

        if not selector:
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
    state = await _state_output(browser, force_refresh=True)
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
    return await _state_output(browser, force_refresh=True)


# Google Drive upload sequence — the order matters:
#   1. browser_navigate https://drive.google.com/drive/my-drive
#   2. browser_interact "click the '+ New' button"
#   3. browser_interact "click 'File upload' in the dropdown menu"
#   4. browser_upload_file { "file_path": "..." }
#      (DOM.setFileInputFiles sets the file after Chrome activates the hidden
#      input when the upload menu item is clicked)
#   5. browser_wait "upload progress bar disappears or file appears in Drive"
#   6. browser_read_page to confirm the file is listed in Drive
#
# Drive keeps its <input type="file"> hidden until the upload menu item is
# clicked. Typing a path into the input with browser_interact therefore fails
# or targets nothing; the input must be activated first, then set over CDP.
async def browser_upload_file(
    file_path: str,
    browser: Optional["BrowserController"] = None,
) -> dict[str, Any]:
    """Set a file on the page's active file input via CDP.

    The OS file picker cannot be driven by CDP. DOM.setFileInputFiles writes
    the selected file directly onto the input element (hidden inputs included),
    which is the only reliable way to upload without the dialog.
    """
    path = str(file_path or "").strip().strip('"')
    if not path or not os.path.isfile(path):
        return {"success": False, "output": f"File not found: {file_path}"}
    if browser is None:
        return {"success": False, "output": "No browser controller available."}

    try:
        tabs_fn = getattr(browser, "_tabs", None)
        pick_fn = getattr(browser, "_pick_tab", None)
        conn_fn = getattr(browser, "_page_conn", None)
        if not (callable(tabs_fn) and callable(pick_fn) and callable(conn_fn)):
            result = await browser.set_file_input("input[type='file']", path)
        else:
            backend, tabs = await tabs_fn()
            target = pick_fn(tabs) if tabs else None
            if not target:
                return {
                    "success": False,
                    "output": "CDP not available. Could not reach the browser.",
                }
            async with conn_fn(target) as conn:
                document = await conn.call(
                    "DOM.getDocument", {"depth": 1}, timeout=10,
                )
                if "error" in document:
                    return {
                        "success": False,
                        "output": f"DOM.getDocument failed: {document['error']}",
                    }
                root_id = document.get("result", {}).get("root", {}).get("nodeId")
                if not root_id:
                    return {
                        "success": False,
                        "output": "DOM.getDocument returned no root node.",
                    }

                query = await conn.call(
                    "DOM.querySelectorAll",
                    {"nodeId": root_id, "selector": "input[type='file']"},
                    timeout=10,
                )
                if "error" in query:
                    return {
                        "success": False,
                        "output": f"DOM.querySelectorAll failed: {query['error']}",
                    }
                node_ids = query.get("result", {}).get("nodeIds") or []
                if not node_ids:
                    return {
                        "success": False,
                        "output": (
                            "No file input found. Make sure you clicked the "
                            "upload button first."
                        ),
                    }

                set_result = await conn.call(
                    "DOM.setFileInputFiles",
                    {"nodeId": node_ids[0], "files": [path]},
                    timeout=15,
                )
                if "error" in set_result:
                    return {
                        "success": False,
                        "output": f"DOM.setFileInputFiles failed: {set_result['error']}",
                    }
            result = {"success": True, "method": f"cdp_{backend}"}

        if not result.get("success", False):
            return {
                "success": False,
                "output": str(
                    result.get("error") or result.get("output") or "File upload failed."
                ),
            }
        return {
            "success": True,
            "output": (
                f"File set on the page's file input: {path}. "
                "Call browser_wait before reading the page to confirm the upload."
            ),
            "path": path,
            "method": result.get("method", "cdp_dom_set_file_input_files"),
        }
    except Exception as exc:
        return {"success": False, "output": f"File upload failed: {exc}"}


def _tab_kind(browser: "BrowserController") -> str:
    """Where a browser call acted: the agent tab or the user's browser."""
    return "agent" if getattr(browser, "_agent_tab_id", None) else "user"


def _key_arg(args: dict) -> str:
    for name in ("url", "instruction", "file_path", "path", "target", "query"):
        value = str(args.get(name) or "").strip()
        if value:
            return value[:60]
    return ""


async def execute_browser_tool(tool_name: str, args: dict,
                               browser: Optional["BrowserController"] = None) -> dict[str, Any]:
    """Execute one browser primitive against the agent's BrowserController."""
    if browser is None:
        return {"success": False, "output": "No browser controller available."}

    t0 = time.perf_counter()
    tab_before = _tab_kind(browser)
    if tool_name == "browser_navigate":
        operation = _navigate(browser, args)
    elif tool_name == "browser_read_page":
        operation = _read(browser, args)
    elif tool_name == "browser_read_full":
        operation = _read_full(browser, args)
    elif tool_name == "browser_get_accessibility_tree":
        operation = _accessibility_tree(browser, args)
    elif tool_name == "browser_interact":
        operation = _interact(browser, args)
    elif tool_name == "browser_wait":
        operation = _wait(browser, args)
    elif tool_name == "browser_upload_file":
        operation = browser_upload_file(
            str(args.get("file_path") or args.get("path") or ""), browser,
        )
    else:
        return {"success": False, "output": f"Unknown browser op '{tool_name}'"}

    try:
        result = await asyncio.wait_for(operation, timeout=_BROWSER_OP_TIMEOUT_S)
    except asyncio.TimeoutError:
        result = {
            "success": False,
            "timeout": True,
            "output": (
                f"Browser operation timed out after {_BROWSER_OP_TIMEOUT_S:.0f}s. "
                "The browser may not be reachable over CDP. Retry, or enable "
                "remote debugging / use mcp_fetch as a read-only fallback."
            ),
        }

    elapsed = time.perf_counter() - t0
    tab = str(result.get("tab") or tab_before)
    log.info(
        f"{tool_name} '{_key_arg(args)}' → "
        f"success={result.get('success')} tab={tab} "
        f"elapsed={elapsed:.2f}s"
    )
    return result
