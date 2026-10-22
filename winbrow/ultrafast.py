"""
Laya Ultrafast Browser Automation Engine
========================================
Open-weight, local-first typed-decision engine for ultrafast browser task execution.
Inspired by browser-use/jev-ultrafast and ipenywis/laya-ultrafast.

Replaces slow multimodal LLM loops with:
1. High-speed DOM interactive element harvesting (~15-30ms)
2. Laya non-autoregressive decision model mapping goals to indexed candidate elements (~25-45ms)
3. Decoupled slot filling (only extracting/generating text when TYPE action is selected)
4. Direct CDP execution over WebSocket / Chrome DevTools Protocol (~5-20ms per action)
5. Multi-step loop capable of complex tasks (Google Flights search, ecommerce, multi-page funnels)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("winbrow.ultrafast")

# Play/watch-style goals: the engine must click into a video, not just search.
_PLAY_INTENT_KW = ("play", "watch", "video", "song", "music", "listen")
_YT_GOAL_RE = re.compile(r"\b(?:youtube|you\s+tube|yt)\b", re.IGNORECASE)
_YT_WATCH_RE = re.compile(r"youtube\.com/watch|youtu\.be/", re.IGNORECASE)
# Stable selectors for the first video result on a YouTube results page.
# (Double quotes inside: the click executor interpolates selectors into
# single-quoted JS strings.)
_YT_VIDEO_SELECTOR = (
    "ytd-video-renderer a#video-title, "
    "ytd-rich-item-renderer a#video-title, "
    "a[href^=\"/watch?v=\"]"
)


def _clean_video_query(goal: str) -> str:
    """Strip play/watch filler from a goal to get the video search query."""
    q = goal.lower()
    q = re.sub(
        r"^(?:please\s+)?(?:play|watch|show|find|search|stream|listen to)\s+"
        r"(?:me\s+)?(?:a\s+|an\s+|some\s+|the\s+|for\s+)?",
        "", q,
    )
    q = re.sub(
        r"\b(?:on|from|in)\s+(?:youtube|you\s+tube|yt)(?:\s+music)?\b",
        " ", q,
    )
    q = re.sub(r"\b(?:youtube|you\s+tube|yt)(?:\s+music)?\b", " ", q)
    q = re.sub(r"\b(?:video|videos|clip|clips)\s+(?:about|on|for|of)\b", " ", q)
    q = re.sub(r"\b(?:about|entitled|called)\b", " ", q)
    q = re.sub(r"\s*\b(?:video|videos|song|songs)\b\s*$", "", q)
    return re.sub(r"\s+", " ", q).strip() or goal

# ────────────────────────────────────────────────────────────────────────────
# Data Models
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class InteractiveElement:
    index: int
    selector: str
    tag_name: str
    element_type: str = ""
    role: str = ""
    label: str = ""
    text: str = ""
    placeholder: str = ""
    value: str = ""
    bounds: Dict[str, float] = field(default_factory=dict)
    is_visible: bool = True

    def to_summary(self) -> str:
        parts = [f"[{self.index}] <{self.tag_name}"]
        if self.element_type:
            parts.append(f" type='{self.element_type}'")
        if self.role:
            parts.append(f" role='{self.role}'")
        parts.append(">")

        info = []
        if self.label:
            info.append(f"label='{self.label}'")
        if self.placeholder:
            info.append(f"placeholder='{self.placeholder}'")
        if self.text:
            clean_text = self.text.replace("\n", " ").strip()
            if clean_text:
                info.append(f"text='{clean_text[:40]}'")
        if self.value:
            info.append(f"val='{self.value[:30]}'")

        if info:
            parts.append(" | " + " ".join(info))
        return "".join(parts)


@dataclass
class UltrafastStep:
    step_number: int
    action: str  # 'click', 'type', 'navigate', 'scroll', 'submit', 'wait', 'extract', 'finish'
    target_index: Optional[int] = None
    selector: Optional[str] = None
    value: Optional[str] = None
    reason: str = ""
    duration_ms: float = 0.0


@dataclass
class UltrafastResult:
    goal: str
    success: bool
    final_answer: str
    steps: List[Dict[str, Any]]
    total_elapsed_ms: float
    method: str = "laya_ultrafast"


# ────────────────────────────────────────────────────────────────────────────
# Injected DOM Harvester Script (with stable data-wb-idx indexing)
# ────────────────────────────────────────────────────────────────────────────

_DOM_HARVESTER_JS = """
(() => {
  const elements = [];
  const query = [
    'a[href]',
    'button',
    'input',
    'select',
    'textarea',
    '[role="button"]',
    '[role="link"]',
    '[role="searchbox"]',
    '[role="textbox"]',
    '[role="combobox"]',
    '[role="option"]',
    '[role="tab"]',
    '[role="checkbox"]',
    '[role="radio"]',
    '[role="menuitem"]',
    '[contenteditable="true"]',
    '[tabindex]:not([tabindex="-1"])'
  ].join(', ');

  const rawCandidates = Array.from(document.querySelectorAll(query));
  let idx = 1;

  for (const el of rawCandidates) {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    const isVisible = rect.width > 0 && rect.height > 0 && 
                      style.visibility !== 'hidden' && 
                      style.display !== 'none' && 
                      style.opacity !== '0';
    if (!isVisible) continue;

    // Stamp a stable, collision-free attribute for CDP targeting
    el.setAttribute('data-wb-idx', idx.toString());

    // Determine descriptive label
    let label = '';
    if (el.labels && el.labels.length > 0) {
      label = el.labels[0].innerText;
    } else if (el.getAttribute('aria-label')) {
      label = el.getAttribute('aria-label');
    } else if (el.getAttribute('placeholder')) {
      label = el.getAttribute('placeholder');
    } else if (el.getAttribute('title')) {
      label = el.getAttribute('title');
    } else if (el.name) {
      label = el.name;
    }

    // Determine CSS selector (prefers id or data-wb-idx)
    let selector = `[data-wb-idx="${idx}"]`;
    if (el.id) {
      selector = '#' + CSS.escape(el.id);
    }

    const textContent = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');

    elements.push({
      index: idx,
      selector: selector,
      tag_name: el.tagName.toLowerCase(),
      element_type: el.type || '',
      role: el.getAttribute('role') || '',
      label: (label || '').trim(),
      text: textContent.slice(0, 80),
      placeholder: el.placeholder || '',
      value: el.value || '',
      bounds: {
        x: Math.round(rect.left),
        y: Math.round(rect.top),
        width: Math.round(rect.width),
        height: Math.round(rect.height)
      },
      is_visible: true
    });

    idx++;
    if (idx > 40) break; // Keep under 40 elements for optimal Laya context and sub-30ms speed
  }

  return {
    url: window.location.href,
    title: document.title,
    elements: elements
  };
})()
"""


# ────────────────────────────────────────────────────────────────────────────
# Slot Extraction Helpers (for TYPE_TEXT in flight & search goals)
# ────────────────────────────────────────────────────────────────────────────

def extract_slot_value(goal: str, element: InteractiveElement) -> str:
    """
    Extract the appropriate value to type into an input field given the user's goal.
    Handles flights (origin, destination, dates), search queries, and form inputs.
    """
    goal_lower = goal.lower()
    el_desc = f"{element.label} {element.placeholder} {element.name if hasattr(element, 'name') else ''} {element.role}".lower()

    # Flight Origin (from, departure, origin, where from)
    if any(k in el_desc for k in ["from", "origin", "depart", "where from"]):
        m = re.search(r"(?:from|departing|leaving)\s+([a-zA-Z\s]+?)(?:\s+to|\s+on|\s+for|\s+at|$)", goal, re.IGNORECASE)
        if m:
            return m.group(1).strip()

    # Flight Destination (to, destination, arrival, where to)
    if any(k in el_desc for k in ["to", "dest", "arriv", "where to"]):
        m = re.search(r"(?:to|going to|arriving in)\s+([a-zA-Z\s]+?)(?:\s+on|\s+for|\s+at|\s+from|$)", goal, re.IGNORECASE)
        if m:
            return m.group(1).strip()

    # Flight Dates (date, departure date, return date)
    if "date" in el_desc:
        m = re.search(r"(?:on|date|for)\s+(\d{1,2}(?:st|nd|rd|th)?\s+[a-zA-Z]+|\d{1,2}/\d{1,2}(?:/\d{2,4})?|[a-zA-Z]+\s+\d{1,2})", goal, re.IGNORECASE)
        if m:
            return m.group(1).strip()

    # Quoted text: type "hello world"
    val_m = re.search(r'(?:with|as|type|enter)\s+["\']([^"\']+)["\']', goal, re.IGNORECASE)
    if val_m:
        return val_m.group(1).strip()

    # Search query: "search for X on Y" or "search X"
    search_m = re.search(r"search\s+(?:for\s+)?(.+?)(?:\s+on\s+.*|\s+in\s+.*|$)", goal, re.IGNORECASE)
    if search_m:
        return search_m.group(1).strip()

    # Cleaned goal keywords as fallback
    cleaned = re.sub(r"^(?:search|find|book|look up|open|go to)\s+(?:for\s+)?", "", goal, flags=re.IGNORECASE).strip()
    return cleaned or "query"


# ────────────────────────────────────────────────────────────────────────────
# Laya Ultrafast Engine
# ────────────────────────────────────────────────────────────────────────────

class LayaUltrafastEngine:
    """
    Typed Decision Browser Engine for WinBrow based on Jev-Ultrafast & Laya.
    Replaces per-step LLM calls with Laya's non-autoregressive decision model.
    """

    def __init__(self, browser_controller: Any, worker: Optional[Any] = None):
        self.browser = browser_controller
        self._worker = worker

    def _get_worker(self) -> Optional[Any]:
        if self._worker is not None:
            return self._worker
        try:
            from .router import get_laya_worker
            self._worker = get_laya_worker()
            return self._worker
        except Exception as e:
            log.debug(f"Could not load Laya worker: {e}")
            return None

    async def harvest_elements(self) -> Dict[str, Any]:
        """Harvest visible interactive elements from the current page via CDP JS injection."""
        t0 = time.perf_counter()
        res = await self.browser.execute_js(_DOM_HARVESTER_JS)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

        if not res.get("success") or not res.get("value"):
            return {
                "url": "",
                "title": "",
                "elements": [],
                "elapsed_ms": elapsed_ms,
                "error": res.get("error", "Failed to harvest DOM"),
            }

        val = res["value"]
        elements = [InteractiveElement(**e) for e in val.get("elements", [])]
        return {
            "url": val.get("url", ""),
            "title": val.get("title", ""),
            "elements": elements,
            "elapsed_ms": elapsed_ms,
        }

    async def decide_with_laya(
        self,
        goal: str,
        url: str,
        title: str,
        elements: List[InteractiveElement],
        previous_steps: List[UltrafastStep],
    ) -> Optional[UltrafastStep]:
        """
        Sub-40ms decision via Laya's choice engine.
        Constructs candidate criteria for action and target element.
        """
        worker = self._get_worker()
        if worker is None:
            return None

        step_num = len(previous_steps) + 1
        if not elements:
            return UltrafastStep(
                step_number=step_num,
                action="wait",
                reason="Waiting for elements to render",
            )

        # 1. Build Laya prompt context
        history_summary = []
        for s in previous_steps[-3:]:
            history_summary.append(f"Step {s.step_number}: {s.action} on [{s.target_index}] ({s.reason})")
        hist_text = "; ".join(history_summary) if history_summary else "Initial state"

        elements_summary = "\n".join(e.to_summary() for e in elements[:30])
        prompt_text = (
            f"Goal: {goal}\n"
            f"Page: {title} ({url})\n"
            f"History: {hist_text}\n"
            f"Interactive Elements:\n{elements_summary}"
        )

        # 2. Build target element criteria
        target_criteria = {}
        for el in elements[:30]:
            label = el.label or el.placeholder or el.text or el.role or el.tag_name
            target_criteria[f"el_{el.index}"] = f"[{el.index}] <{el.tag_name}> {label[:40]}"

        target_criteria["none"] = "No specific element"

        # 3. Build question schema
        questions = {
            "action": {
                "type": "choice",
                "instructions": "Select the next browser action to execute towards the goal.",
                "criteria": {
                    "click": "Click an element (button, tab, link, combobox, or option)",
                    "type": "Type text into an input field or search combobox",
                    "scroll": "Scroll the page down to view more content",
                    "wait": "Wait for flight search results or page updates to complete",
                    "finish": "Task is completed or search results are clearly visible",
                },
            },
            "target": {
                "type": "choice",
                "instructions": "Select the numbered element from the page to interact with.",
                "criteria": target_criteria,
            },
        }

        try:
            # Query Laya with strict budget (1.5s timeout)
            res = await worker.apredict(prompt_text, questions, timeout=1.5)
            answers = res.get("answers", {})

            action_choice = answers.get("action", {}).get("choice", "click")
            target_choice = answers.get("target", {}).get("choice", "el_1")

            # Extract element index from target choice (e.g. "el_2" -> 2)
            target_idx = None
            if target_choice and target_choice.startswith("el_"):
                try:
                    target_idx = int(target_choice.split("_")[1])
                except (ValueError, IndexError):
                    target_idx = None

            target_el = next((e for e in elements if e.index == target_idx), None)

            if action_choice == "finish":
                return UltrafastStep(
                    step_number=step_num,
                    action="finish",
                    reason="Laya determined goal is accomplished",
                    value=f"Completed on {title or url}",
                )

            if action_choice == "scroll":
                return UltrafastStep(
                    step_number=step_num,
                    action="scroll",
                    value="down",
                    reason="Scroll to view more results",
                )

            if action_choice == "wait":
                return UltrafastStep(
                    step_number=step_num,
                    action="wait",
                    reason="Wait for page updates",
                )

            if target_el:
                # If Laya selected an input or combobox
                if action_choice == "type" or target_el.tag_name in ("input", "textarea") or target_el.role in ("combobox", "searchbox", "textbox"):
                    val = extract_slot_value(goal, target_el)
                    return UltrafastStep(
                        step_number=step_num,
                        action="type",
                        target_index=target_el.index,
                        selector=target_el.selector,
                        value=val,
                        reason=f"Type '{val}' into {target_el.to_summary()}",
                    )
                else:
                    return UltrafastStep(
                        step_number=step_num,
                        action="click",
                        target_index=target_el.index,
                        selector=target_el.selector,
                        reason=f"Click {target_el.to_summary()}",
                    )

        except Exception as e:
            log.debug(f"Laya ultrafast decision failed or timed out: {e}")

        return None

    def make_typed_decision(
        self,
        goal: str,
        url: str,
        title: str,
        elements: List[InteractiveElement],
        previous_steps: List[UltrafastStep],
    ) -> UltrafastStep:
        """
        Fast heuristic fallback when Laya decision is offline or times out.
        Handles flight searches, form filling, button clicks, and search submissions.
        """
        step_num = len(previous_steps) + 1
        goal_lower = goal.lower()

        # Check termination condition
        if previous_steps:
            last_step = previous_steps[-1]
            if last_step.action in ("click", "submit", "type") and any(kw in goal_lower for kw in ["open", "go to", "navigate"]) and not any(kw in goal_lower for kw in ["find", "search", "book", "flight"]):
                return UltrafastStep(
                    step_number=step_num,
                    action="finish",
                    reason=f"Navigated successfully to {url or title}",
                    value=f"Opened {title or url}",
                )

        # 1. Flight Search & Multi-Input Flow (e.g. "flights from New York to London")
        is_flight_goal = any(kw in goal_lower for kw in ["flight", "flights", "fly", "airline", "booking"])
        if is_flight_goal or "google.com/travel/flights" in url:
            # Check for unfilled origin or destination inputs first
            for el in elements:
                el_desc = f"{el.label} {el.placeholder} {el.text}".lower()
                already_typed = any(s.action == "type" and s.target_index == el.index for s in previous_steps)
                if not already_typed and (el.tag_name in ("input", "textarea") or el.role in ("combobox", "textbox")):
                    if any(k in el_desc for k in ["from", "origin", "where from", "depart"]):
                        val = extract_slot_value(goal, el)
                        return UltrafastStep(
                            step_number=step_num,
                            action="type",
                            target_index=el.index,
                            selector=el.selector,
                            value=val,
                            reason=f"Enter origin '{val}' into {el.to_summary()}",
                        )
                    elif any(k in el_desc for k in ["to", "dest", "where to", "arriv"]):
                        val = extract_slot_value(goal, el)
                        return UltrafastStep(
                            step_number=step_num,
                            action="type",
                            target_index=el.index,
                            selector=el.selector,
                            value=val,
                            reason=f"Enter destination '{val}' into {el.to_summary()}",
                        )

            # Check if there is an unclicked search button
            search_btns = [
                e for e in elements
                if e.tag_name in ("button", "input") and any(k in f"{e.text} {e.label} {e.value}".lower() for k in ["search", "explore", "find flights", "done"])
            ]
            if search_btns and any(s.action == "type" for s in previous_steps) and not any(s.action == "click" for s in previous_steps):
                return UltrafastStep(
                    step_number=step_num,
                    action="click",
                    target_index=search_btns[0].index,
                    selector=search_btns[0].selector,
                    reason=f"Click flight search button {search_btns[0].to_summary()}",
                )

            # If inputs are filled / search clicked / or results loaded: extract flight options
            has_extracted = any(s.action == "extract" for s in previous_steps)
            if not has_extracted:
                return UltrafastStep(
                    step_number=step_num,
                    action="extract",
                    reason="Extract flight itineraries, airlines, and price options",
                )
            else:
                return UltrafastStep(
                    step_number=step_num,
                    action="finish",
                    reason="Flight details extracted successfully",
                    value=previous_steps[-1].value or "Flight search completed",
                )

        # 2. General Search Bar & Results Exploration (e.g. "search for RTX 4090 on amazon" or "search for chicken curry")
        search_keywords = ["search", "find", "look up", "query", "recipe", "details"]
        is_search_goal = any(kw in goal_lower for kw in search_keywords)

        if is_search_goal:
            # Check for unfilled search input box first
            query_text = goal
            m = re.search(r"search\s+(?:for\s+)?(.+?)(?:\s+on\s+.*|\s+in\s+.*|$)", goal, re.IGNORECASE)
            if m:
                query_text = m.group(1).strip()

            input_elements = [
                e for e in elements
                if e.tag_name in ("input", "textarea") or e.role in ("searchbox", "textbox", "combobox")
            ]
            search_inputs = [
                e for e in input_elements
                if "search" in e.placeholder.lower()
                or "search" in e.label.lower()
                or "search" in e.role.lower()
                or e.element_type in ("search", "text")
            ]
            target_el = search_inputs[0] if search_inputs else (input_elements[0] if input_elements and "google.com/search" not in url else None)

            if target_el:
                already_typed = any(s.action == "type" and s.target_index == target_el.index for s in previous_steps)
                if not already_typed:
                    return UltrafastStep(
                        step_number=step_num,
                        action="type",
                        target_index=target_el.index,
                        selector=target_el.selector,
                        value=query_text,
                        reason=f"Type search query into {target_el.to_summary()}",
                    )

            if "google.com/search" in url:
                # On Google Search results page: extract answers/results and finish immediately
                has_extracted = any(s.action == "extract" for s in previous_steps)
                if not has_extracted:
                    return UltrafastStep(
                        step_number=step_num,
                        action="extract",
                        reason="Extract search results and overview from Google Search",
                    )
                return UltrafastStep(
                    step_number=step_num,
                    action="finish",
                    reason="Completed search extraction",
                    value=previous_steps[-1].value or f"Searched for: {goal}",
                )

        # If we clicked into a website from search or user wants details: extract content
        has_extracted = any(s.action == "extract" for s in previous_steps)
        if (len(previous_steps) >= 1 and not has_extracted and "google.com/search" not in url) or any(kw in goal_lower for kw in ["extract", "read", "get price", "what is", "content", "recipe"]):
            if not has_extracted:
                return UltrafastStep(
                    step_number=step_num,
                    action="extract",
                    reason="Extract content, instructions, and details from the page",
                )

        # 3. Form Field Filling
        if any(kw in goal_lower for kw in ["fill", "enter", "type"]):
            for el in elements:
                if el.tag_name in ("input", "textarea"):
                    already_typed = any(s.target_index == el.index for s in previous_steps)
                    if not already_typed:
                        val = extract_slot_value(goal, el)
                        return UltrafastStep(
                            step_number=step_num,
                            action="type",
                            target_index=el.index,
                            selector=el.selector,
                            value=val,
                            reason=f"Type value into input {el.to_summary()}",
                        )

        # 4. Explicit Click Goal
        if "click" in goal_lower:
            target_phrase = goal_lower.replace("click", "").replace("on", "").replace("the", "").strip()
            target_words = [w for w in target_phrase.split() if w not in ("button", "link", "element", "item")]
            for el in elements:
                comb = f"{el.text} {el.label} {el.placeholder} {el.selector}".lower()
                if target_phrase and (target_phrase in comb or any(w in comb for w in target_words if len(w) > 2)):
                    return UltrafastStep(
                        step_number=step_num,
                        action="click",
                        target_index=el.index,
                        selector=el.selector,
                        reason=f"Click element matching '{target_phrase}'",
                    )

        # 5. Default Fallback
        if previous_steps:
            return UltrafastStep(
                step_number=step_num,
                action="finish",
                reason="Completed browser interaction sequence",
                value=f"Finished goal on {title or url}",
            )

        return UltrafastStep(
            step_number=step_num,
            action="extract",
            reason="Inspect page content to determine next step",
        )

    async def execute_step(self, step: UltrafastStep) -> Dict[str, Any]:
        """Execute a single ultrafast browser action step via CDP."""
        t0 = time.perf_counter()
        act = step.action

        if act == "type":
            if step.selector and step.value:
                # Type using browser controller (CDP path preferred)
                res = await self.browser.type_in_element(step.selector, step.value)

                # Only dispatch Enter when typing into a standalone search box,
                # NOT on a multi-field form (flight origin/destination, login, etc.)
                # We check if this is the only/last input before submitting.
                is_search_submit = any(
                    kw in (step.reason or "").lower()
                    for kw in ["search query", "search submit", "submit"]
                )
                if is_search_submit:
                    await self.browser.execute_js(f"""
                    (() => {{
                        const el = document.querySelector('{step.selector}')
                                || document.querySelector('[data-wb-idx="{step.target_index}"]');
                        if (el) {{
                            el.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Enter', keyCode: 13, bubbles: true }}));
                            el.dispatchEvent(new KeyboardEvent('keyup',   {{ key: 'Enter', keyCode: 13, bubbles: true }}));
                        }}
                    }})()
                    """)

                # Short wait for autocomplete dropdowns / AJAX
                await asyncio.sleep(0.5)
                duration = round((time.perf_counter() - t0) * 1000, 1)
                return {"success": res.get("success", True), "output": f"Typed '{step.value}'", "elapsed_ms": duration}

        elif act == "click":
            if step.selector or step.target_index:
                # Click element with scrollIntoView and synthetic dispatch
                sel = step.selector or f'[data-wb-idx="{step.target_index}"]'
                sel_js = sel.replace("\\", "\\\\").replace("'", "\\'")
                res = await self.browser.execute_js(f"""
                (() => {{
                    const el = document.querySelector('{sel_js}')
                            || document.querySelector('[data-wb-idx="{step.target_index}"]');
                    if (el) {{
                        const link = el.closest('a');
                        if (link && link.target === '_blank') {{
                            link.removeAttribute('target');
                        }}
                        el.scrollIntoView({{ behavior: 'instant', block: 'center' }});
                        el.focus();
                        el.click();
                        return true;
                    }}
                    return false;
                }})()
                """)
                # Wait for page/DOM updates after click (AJAX, navigation, dropdown open)
                await asyncio.sleep(0.8)
                duration = round((time.perf_counter() - t0) * 1000, 1)
                clicked = bool(res.get("value", False))
                after = await self.browser.get_current_url()
                return {
                    "success": clicked,
                    "output": f"Clicked {sel}",
                    "url": after.get("url", ""),
                    "elapsed_ms": duration,
                }

        elif act == "submit":
            sel = step.selector or f'[data-wb-idx="{step.target_index}"]'
            js = f"""
            (() => {{
                const el = document.querySelector('{sel}') || document.querySelector('[data-wb-idx="{step.target_index}"]');
                if (el && el.form) {{
                    el.form.submit();
                    return true;
                }} else if (el) {{
                    el.click();
                    return true;
                }}
                return false;
            }})()
            """
            res = await self.browser.execute_js(js)
            duration = round((time.perf_counter() - t0) * 1000, 1)
            return {"success": True, "output": "Submitted form", "elapsed_ms": duration}

        elif act == "navigate":
            if step.value:
                res = await self.browser.navigate_to(step.value)
                # Wait for page to start loading
                await asyncio.sleep(1.5)
                duration = round((time.perf_counter() - t0) * 1000, 1)
                return {"success": True, "output": f"Navigated to {step.value}", "elapsed_ms": duration}

        elif act == "scroll":
            direction = step.value or "down"
            res = await self.browser.scroll_page(direction=direction, amount=3)
            duration = round((time.perf_counter() - t0) * 1000, 1)
            return {"success": True, "output": f"Scrolled {direction}", "elapsed_ms": duration}

        elif act == "wait":
            await asyncio.sleep(1.2)
            duration = round((time.perf_counter() - t0) * 1000, 1)
            return {"success": True, "output": "Waited 1.2s", "elapsed_ms": duration}

        elif act == "extract":
            # Smart context-aware DOM extraction
            extract_js = """
            (() => {
                const url = window.location.href;
                const title = document.title;

                // 1. Google Flights
                if (url.includes('google.com/travel/flights')) {
                    const cards = Array.from(document.querySelectorAll('li, [role="listitem"], .pIavfa'))
                        .map(e => (e.getAttribute('aria-label') || e.innerText || '').trim())
                        .filter(t => t.length > 20 && (t.includes('₹') || t.includes('$') || t.includes('€') || t.includes('stop') || t.includes('hr') || t.includes('Find flights')))
                        .slice(0, 6);
                    if (cards.length > 0) {
                        return { type: 'flights', title: title, data: cards };
                    }
                }

                // 2. Google Search
                if (url.includes('google.com/search')) {
                    const featured = document.querySelector('[data-attrid="wa:/description"], [data-attrid*="recipe"], .kp-blk, .hgKElc');
                    const firstLink = document.querySelector('#rso a h3');
                    const snippets = Array.from(document.querySelectorAll('#rso .VwiC3b, #rso span'))
                        .map(e => e.innerText.trim())
                        .filter(t => t.length > 30)
                        .slice(0, 4);
                    return {
                        type: 'search',
                        title: title,
                        featured: featured ? featured.innerText.trim() : null,
                        top_result: firstLink ? firstLink.innerText.trim() : null,
                        snippets: snippets
                    };
                }

                // 3. Content Webpage (Recipe, Article, Product)
                const headings = Array.from(document.querySelectorAll('h1, h2, h3'))
                    .map(e => e.innerText.trim())
                    .filter(t => t.length > 3 && t.length < 80)
                    .slice(0, 5);
                const paras = Array.from(document.querySelectorAll('article, main, .recipe, .recipe-body, p, li'))
                    .map(e => e.innerText.trim())
                    .filter(t => t.length > 35 && !t.includes('cookie') && !t.includes('subscribe'))
                    .slice(0, 10);
                return {
                    type: 'content',
                    title: title,
                    headings: headings,
                    content: paras.join('\\n\\n').slice(0, 1200)
                };
            })()
            """
            res = await self.browser.execute_js(extract_js)
            val = res.get("value") or {}
            out_str = ""
            if isinstance(val, dict):
                v_type = val.get("type")
                if v_type == "flights":
                    items = val.get("data", [])
                    out_str = f"Found Flights on {val.get('title', 'Google Flights')}:\n" + "\n---\n".join(items)
                elif v_type == "search":
                    feat = val.get("featured")
                    top = val.get("top_result")
                    snips = val.get("snippets", [])
                    if feat:
                        out_str = f"Summary: {feat}\n\nTop Result: {top}"
                    else:
                        out_str = f"Top Result: {top}\n" + "\n".join(snips)
                elif v_type == "content":
                    h = ", ".join(val.get("headings", [])[:3])
                    c = val.get("content", "")
                    out_str = f"{val.get('title', '')}\n{h}\n\n{c}"
            if not out_str:
                page_text_res = await self.browser.read_page_text()
                out_str = page_text_res.get("text", "")[:800]

            duration = round((time.perf_counter() - t0) * 1000, 1)
            step.value = out_str
            return {"success": True, "output": out_str, "elapsed_ms": duration}

        elif act == "finish":
            duration = round((time.perf_counter() - t0) * 1000, 1)
            return {"success": True, "output": step.value or "Finished task", "elapsed_ms": duration}

        duration = round((time.perf_counter() - t0) * 1000, 1)
        return {"success": True, "output": "Step executed", "elapsed_ms": duration}

    async def _youtube_play_step(
        self,
        goal_lower: str,
        url: str,
        title: str,
        elements: List[InteractiveElement],
        previous_steps: List[UltrafastStep],
    ) -> Optional[UltrafastStep]:
        """Deterministic handling for play/watch goals on YouTube.

        Clicks the first video result on the results page and finishes once the
        watch page is open, so the engine no longer stops at the search results.
        """
        if "youtube.com" not in (url or "") and "youtu.be" not in (url or ""):
            return None
        if not any(kw in goal_lower for kw in _PLAY_INTENT_KW):
            return None

        step_num = len(previous_steps) + 1
        clicked_ok = any(
            s.action == "click" and "/watch" in (s.value or "")
            for s in previous_steps
        )

        if _YT_WATCH_RE.search(url or ""):
            if not clicked_ok and not any(s.action == "click" for s in previous_steps):
                return UltrafastStep(
                    step_number=step_num,
                    action="wait",
                    reason="Already on a video watch page; letting it play",
                )
            return UltrafastStep(
                step_number=step_num,
                action="finish",
                reason="Video watch page opened",
                value=f"Now playing on YouTube: {title or url}",
            )

        if clicked_ok:
            return UltrafastStep(
                step_number=step_num,
                action="wait",
                reason="Clicked a video result; waiting for the player to load",
            )

        n_click_attempts = sum(1 for s in previous_steps if s.action == "click")
        if n_click_attempts >= 4:
            return None
        n_waits = sum(1 for s in previous_steps if s.action == "wait")
        if n_waits >= 20:
            return None

        last = previous_steps[-1] if previous_steps else None
        if last is not None and last.action == "click":
            # Click just failed to navigate: back off a beat, then click again.
            return UltrafastStep(
                step_number=step_num,
                action="wait",
                reason="Video click did not navigate; waiting before retry",
            )

        # Only click once the results DOM actually contains a video link —
        # the shell/header renders first and would make blind clicks miss.
        probe = await self.browser.execute_js(
            "!!document.querySelector(" + json.dumps(_YT_VIDEO_SELECTOR) + ")"
        )
        if not probe.get("value"):
            return UltrafastStep(
                step_number=step_num,
                action="wait",
                reason="Waiting for video results to render",
            )
        return UltrafastStep(
            step_number=step_num,
            action="click",
            selector=_YT_VIDEO_SELECTOR,
            reason="Click first video result to play it",
        )

    async def _ensure_video_playing(self) -> None:
        """Make sure the YouTube player is actually playing, with sound.

        Watch pages often load scrolled away from the player, and synthetic JS
        clicks are not user gestures, so autoplay can be blocked. Scroll the
        player into view, click it with a trusted CDP mouse event, then
        play()/unmute via JS.
        """
        try:
            # Wait for the player to be interactive (duration known, data buffered).
            info: Any = None
            for _ in range(12):
                res = await self.browser.execute_js(
                    """
                    (() => {
                        const v = document.querySelector('video');
                        if (!v) return { exists: false };
                        const r = v.getBoundingClientRect();
                        return {
                            exists: true,
                            ready: v.readyState >= 2 && isFinite(v.duration) && v.duration > 0,
                            paused: !!v.paused,
                            muted: !!v.muted,
                            x: Math.round(r.x + r.width / 2),
                            y: Math.round(r.y + r.height / 2),
                        };
                    })()
                    """
                )
                info = res.get("value")
                if isinstance(info, dict) and info.get("exists") and info.get("ready"):
                    break
                await asyncio.sleep(0.5)
            if not isinstance(info, dict) or not info.get("exists"):
                return

            if not info.get("paused"):
                if info.get("muted"):
                    await self.browser.execute_js(
                        "(() => { const v = document.querySelector('video');"
                        " if (v) v.muted = false; return true; })()"
                    )
                return

            # Player is often above the viewport on load — bring it into view,
            # then toggle play with a trusted mouse click (not JS el.click(),
            # which is not a user gesture and cannot start sound playback).
            # NOTE: click coordinates must come from the player container —
            # YouTube's <video> is CSS-offset inside it and its own rect lies.
            await self.browser.execute_js("window.scrollTo(0, 0); true")
            await asyncio.sleep(0.4)
            res = await self.browser.execute_js(
                """
                (() => {
                    const p = document.querySelector('#movie_player, .html5-video-player');
                    const v = document.querySelector('video');
                    const el = p || v;
                    if (!el) return null;
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.bottom <= 0 || r.top >= window.innerHeight) return null;
                    const cy = Math.max(10, Math.min(window.innerHeight - 10, r.top + r.height / 2));
                    const cx = Math.max(10, Math.min(window.innerWidth - 10, r.left + r.width / 2));
                    return { x: Math.round(cx), y: Math.round(cy) };
                })()
                """
            )
            pos = res.get("value") or {}
            if pos.get("x") is None:
                return

            paused_now = True
            for _attempt in range(2):
                await self.browser.click_at(pos["x"], pos.get("y", 360))
                # The player needs a beat to process the click — poll, don't
                # race it with extra keys (a second toggle would pause again).
                for _ in range(6):
                    await asyncio.sleep(0.5)
                    check = await self.browser.execute_js(
                        "(() => { const v = document.querySelector('video');"
                        " return v ? !!v.paused : null; })()"
                    )
                    paused_now = check.get("value") is True
                    if not paused_now:
                        break
                if not paused_now:
                    break
            if paused_now:
                return
            # Playing — unmute so the user can actually hear it.
            await self.browser.execute_js(
                "(() => { const v = document.querySelector('video');"
                " if (v) v.muted = false; return true; })()"
            )
        except Exception as e:
            log.debug(f"ensure_video_playing failed: {e}")

    async def run_task(self, goal: str, max_steps: int = 20) -> UltrafastResult:
        """
        Run multi-step Ultrafast Browser Task (Jev-Ultrafast / Laya-Ultrafast architecture).
        Executes sub-50ms Laya decisions until finish condition or max steps reached.
        """
        t0 = time.perf_counter()
        await self.browser.ensure_browser_open()

        # Detect the best starting URL from the goal and navigate there first
        goal_lower = goal.lower()
        cleaned_query = re.sub(r"^(?:search|find|book|look up|open|go to|show me|get me)\s+(?:for\s+)?", "", goal, flags=re.IGNORECASE).strip()

        _SITE_MAP = [
            # Travel (check google flights before generic flight so it wins)
            (["google flights", "google flight"], "https://www.google.com/travel/flights"),
            (["flight", "flights", "fly ", "airline"], "https://www.google.com/travel/flights"),
            (["booking.com", "book hotel", "hotels"], "https://www.booking.com"),
            (["airbnb"], "https://www.airbnb.com"),
            (["expedia"], "https://www.expedia.com"),
            (["kayak"], "https://www.kayak.com"),
            # Shopping
            (["amazon"], "https://www.amazon.com"),
            (["ebay"], "https://www.ebay.com"),
            (["walmart"], "https://www.walmart.com"),
            # Video
            (["youtube"], "https://www.youtube.com"),
            # Social
            (["twitter", "tweet", "x.com"], "https://twitter.com"),
            (["instagram"], "https://www.instagram.com"),
            (["reddit"], "https://www.reddit.com"),
            (["linkedin"], "https://www.linkedin.com"),
            # Dev
            (["github"], "https://www.github.com"),
        ]
        current_url_info = await self.browser.get_current_url()
        current_url_str = current_url_info.get("url", "")

        target_url: Optional[str] = None
        # Flight search with query pre-filled
        if any(kw in goal_lower for kw in ["flight", "flights", "fly ", "airline", "book flight"]):
            target_url = "https://www.google.com/travel/flights?q=" + urllib.parse.quote_plus(goal)
        # Amazon search
        elif "amazon" in goal_lower:
            m = re.search(r"(?:on amazon|amazon for)\s*(.*)", goal_lower)
            q = m.group(1).strip() if m and m.group(1).strip() else cleaned_query
            target_url = "https://www.amazon.com/s?k=" + urllib.parse.quote_plus(q)
        # YouTube search (play/watch goals get a cleaned query + later click a video)
        elif _YT_GOAL_RE.search(goal_lower):
            q = _clean_video_query(goal)
            target_url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(q)
        else:
            for keywords, site_url in _SITE_MAP:
                if any(kw in goal_lower for kw in keywords):
                    site_domain = site_url.replace("https://www.", "").replace("https://", "").split("/")[0]
                    if site_domain not in current_url_str:
                        target_url = site_url
                    break
            # Direct URL check
            url_match = re.search(r"(https?://\S+|[\w\-]+\.(com|org|net|io|dev|ai|gov|edu|co|me|app)[\S]*)", goal, re.IGNORECASE)
            if url_match and not target_url:
                matched_u = url_match.group(0)
                target_url = matched_u if matched_u.startswith("http") else f"https://{matched_u}"
            # General Web Search Fallback
            elif not target_url:
                target_url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(cleaned_query or goal)

        if target_url:
            log.info(f"Ultrafast: navigating to {target_url} for goal: {goal!r}")
            # Open the target in a NEW tab so the user's current tab (e.g. the
            # prompt/UI tab) is never hijacked; same window, not a new one.
            await self.browser.navigate_to(target_url, new_tab=True)
            await asyncio.sleep(0.8)  # brief wait for initial page render

        steps_history: List[UltrafastStep] = []
        step_traces: List[Dict[str, Any]] = []
        final_answer = ""

        # Early termination: if we just navigated to a search results page,
        # extract content and return immediately — no multi-step loop needed.
        if target_url and "google.com/search" in (target_url or ""):
            await asyncio.sleep(0.8)  # let results render
            page_text = await self.browser.read_page_text()
            text = page_text.get("text", "")
            title_info = await self.browser.get_current_url()
            page_title = title_info.get("title", "")

            # If browser extraction didn't yield text, query Wikipedia API fallback
            if not text:
                try:
                    clean_q = cleaned_query or goal
                    clean_q = re.sub(r"^(?:fetch|get|show|tell)\s+(?:me\s+)?(?:the\s+)?(?:details\s+of\s+|info\s+about\s+)?", "", clean_q, flags=re.I).strip()
                    wiki_q = urllib.parse.quote(clean_q.replace(" ", "_"))
                    req = urllib.request.Request(
                        f"https://en.wikipedia.org/api/rest_v1/page/summary/{wiki_q}",
                        headers={"User-Agent": "WinBrow/1.0"}
                    )
                    with urllib.request.urlopen(req, timeout=3.5) as r:
                        wdata = json.loads(r.read().decode("utf-8", errors="replace"))
                        if wdata.get("extract"):
                            text = f"{wdata.get('title')}: {wdata.get('extract')}"
                            page_title = wdata.get("title", page_title)
                except Exception:
                    pass

            snippet = (text[:800] + "...") if len(text) > 800 else text
            total_elapsed = round((time.perf_counter() - t0) * 1000, 1)
            return UltrafastResult(
                goal=goal,
                success=True,
                final_answer=f"{page_title}\n{snippet}" if (page_title and snippet) else (snippet or f"Searched for: {goal}"),
                steps=[{"step": 1, "action": "navigate", "reason": f"Direct search: {target_url}", "elapsed_ms": total_elapsed}],
                total_elapsed_ms=total_elapsed,
                method="laya_ultrafast",
            )

        # Cap max_steps for simple search goals (they should finish in 2-3 steps)
        if any(kw in goal_lower for kw in ["search", "find", "look up", "weather", "news"]):
            max_steps = min(max_steps, 5)

        # Loop protection: track target element interactions
        consecutive_same_actions = 0
        last_action_signature = None

        for step_idx in range(1, max_steps + 1):
            # Step 1: Harvest interactive DOM state
            harvest = await self.harvest_elements()
            url = harvest.get("url", "")
            title = harvest.get("title", "")
            elements = harvest.get("elements", [])

            # Step 2: Make decision (YouTube play goals first, then Laya, then heuristic)
            decision = await self._youtube_play_step(goal_lower, url, title, elements, steps_history)
            if decision is None:
                decision = await self.decide_with_laya(goal, url, title, elements, steps_history)
                if decision is None:
                    decision = self.make_typed_decision(goal, url, title, elements, steps_history)

            # Loop detection
            current_signature = (decision.action, decision.target_index)
            if current_signature == last_action_signature and decision.action in ("click", "type"):
                consecutive_same_actions += 1
                if consecutive_same_actions >= 2:
                    log.debug("Loop detected on same element, triggering scroll or finish")
                    decision = UltrafastStep(
                        step_number=step_idx,
                        action="scroll",
                        value="down",
                        reason="Loop break: scroll to reveal new elements",
                    )
                    consecutive_same_actions = 0
            else:
                consecutive_same_actions = 0
                last_action_signature = current_signature

            # Step 3: Execute step via CDP
            exec_res = await self.execute_step(decision)

            decision.duration_ms = exec_res.get("elapsed_ms", 0.0)
            if decision.action == "click" and exec_res.get("url"):
                decision.value = exec_res["url"]
            steps_history.append(decision)

            step_traces.append({
                "step": step_idx,
                "action": decision.action,
                "target_index": decision.target_index,
                "reason": decision.reason,
                "output": exec_res.get("output", ""),
                "success": exec_res.get("success"),
                "url": url,
                "elapsed_ms": decision.duration_ms,
            })
            if decision.action == "click" and exec_res.get("success") is False:
                log.warning(f"Ultrafast click failed (selector={decision.selector!r}) on {url}")

            # Check termination
            if decision.action == "finish":
                final_answer = exec_res.get("output", decision.value or "Goal completed successfully")
                if "youtube" in goal_lower and any(kw in goal_lower for kw in _PLAY_INTENT_KW):
                    await self._ensure_video_playing()
                    cur_title = ""
                    for _ in range(6):
                        cur = await self.browser.get_current_url()
                        cur_title = (cur.get("title") or "").replace(" - YouTube", "").strip()
                        if cur_title and cur_title.lower() != "youtube":
                            break
                        await asyncio.sleep(0.7)
                    if cur_title:
                        final_answer = f"Now playing on YouTube: {cur_title}"
                break

            if decision.action == "extract" and exec_res.get("output"):
                final_answer = exec_res.get("output")
                if len(steps_history) >= 1 and ("flight" in goal_lower or "google.com/travel/flights" in url):
                    break
                elif len(steps_history) >= 2:
                    break

            # Short sleep to allow DOM updates / AJAX transitions
            await asyncio.sleep(0.15)

        total_elapsed = round((time.perf_counter() - t0) * 1000, 1)

        if not final_answer:
            final_answer = f"Completed {len(steps_history)} ultrafast browser steps for goal: '{goal}'"

        return UltrafastResult(
            goal=goal,
            success=True,
            final_answer=final_answer,
            steps=step_traces,
            total_elapsed_ms=total_elapsed,
            method="laya_ultrafast",
        )
