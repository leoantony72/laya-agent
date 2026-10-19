"""
Grounding Ladder — Escalating Senses for Screen Grounding (Rung 1..3)
======================================================================
Rung 1  window frames      (cheap, always available)
Rung 2  UIA elements       (rich, when the app exposes them)
Rung 3  OCR / screenshot   (universal, when the app paints pixels)

resolve_target() climbs the ladder until a clicked target is found, and
reports which rung answered so the brain can recover from misses.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from .uia import UIATree, UIAElement
from .driver import ComputerDriver

log = logging.getLogger("winbrow.computer.ladder")

_FUZZ_STOPWORDS = {"the", "a", "an", "button", "menu", "tab", "dialog", "please"}


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(w for w in s.split() if w not in _FUZZ_STOPWORDS)


@dataclass
class GroundingHit:
    ok: bool
    source: str = ""              # rung that answered: "uia" | "ocr" | "window"
    x: int = 0
    y: int = 0
    matched: str = ""
    candidates: List[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict:
        d = {"found": self.ok, "source": self.source, "x": self.x, "y": self.y,
             "matched": self.matched, "candidates": self.candidates[:8]}
        if self.reason:
            d["reason"] = self.reason
        return d


class GroundingLadder:
    """Tries rungs in order: UIA → OCR → window-title. Pure read sensing."""

    def __init__(self, driver: ComputerDriver, uia: Optional[UIATree] = None,
                 screen: Optional[Any] = None, ocr_enabled: bool = True):
        self.driver = driver
        self.uia = uia or UIATree()
        self.screen = screen
        self.ocr_enabled = ocr_enabled

    # ------------------------------------------------------------------ observe
    def observe(self, hwnd: Optional[int] = None, include_ocr: bool = False) -> dict:
        """Compact normalized context for a window (brain prompt material)."""
        if hwnd is None:
            hwnd = self.driver.user32.GetForegroundWindow()
        wins = self.driver.list_windows()
        win = next((w for w in wins if w.hwnd == hwnd), None)
        out: dict = {"hwnd": hwnd, "window": win.to_dict() if win else None}
        elems = self.uia.observe(hwnd)
        inter = [e for e in elems if e.interactable or e.name]
        out["uia"] = {
            "count": len(inter),
            "elements": [e.to_dict() for e in inter[:50]],
            "block": "\n".join(e.to_summary() for e in inter[:40]),
        }
        if include_ocr and self.screen is not None:
            ocr = self.screen.ocr()
            out["ocr"] = {"available": ocr.get("available"),
                          "lines": ocr.get("lines", [])[:40],
                          "text": ocr.get("text", "")[:2000]}
        return out

    # ------------------------------------------------------------------ resolve
    def resolve_target(self, text: str, hwnd: Optional[int] = None,
                       use_ocr: bool = True, wait_after_focus_ms: int = 150) -> GroundingHit:
        """Find a clickable target by climbing the ladder. Read-only."""
        target = _norm(text)
        if not target:
            return GroundingHit(ok=False, reason="no target text")

        if hwnd is None:
            hwnd = self.driver.user32.GetForegroundWindow()
        if not hwnd:
            return GroundingHit(ok=False, reason="no foreground window")

        candidates: List[str] = []

        # --- rung 2: UIA elements ---
        elems = self.uia.observe(hwnd)
        hits: List[Tuple[float, UIAElement]] = []
        tnorm = re.compile(r"(^|\s)" + re.escape(target) + r"(\s|$)")
        for el in elems:
            if not (el.interactable or el.invoke_supported):
                continue
            hay = _norm(f"{el.name} {el.value}")
            if not hay:
                continue
            if hay == target:
                hits.append((100.0, el))
            elif tnorm.search(hay):
                hits.append((80.0, el))
            elif target in hay:
                hits.append((60.0, el))
            else:
                # token overlap fallback
                tw, hw = set(target.split()), set(hay.split())
                if tw and hw:
                    overlap = len(tw & hw) / len(tw)
                    if overlap >= 0.7:
                        hits.append((40.0 + overlap * 20, el))
            candidates.append(el.to_summary()[:70])
        if hits:
            hits.sort(key=lambda t: -t[0])
            el = hits[0][1]
            x, y = el.center
            return GroundingHit(ok=True, source="uia", x=int(x), y=int(y),
                                matched=(el.name or el.value)[:60],
                                candidates=[h.name or h.value or h.automation_id for _, h in hits[:5]])

        # --- rung 3: OCR lines ---
        if use_ocr and self.ocr_enabled and self.screen is not None:
            try:
                ocr = self.screen.ocr()
            except Exception as e:
                ocr = {"available": False, "lines": []}
                log.debug(f"OCR rung failed: {e}")
            ocr_hits: List[Tuple[float, Tuple[int, int, int, int], str]] = []
            for ln in ocr.get("lines", []):
                lnorm = _norm(ln.get("text", ""))
                if lnorm == target:
                    ocr_hits.append((100.0, tuple(ln["rect"]), ln["text"]))
                elif target in lnorm:
                    ocr_hits.append((70.0, tuple(ln["rect"]), ln["text"]))
                else:
                    tw, lw = set(target.split()), set(lnorm.split())
                    if tw and lw and len(tw & lw) / len(tw) >= 0.9:
                        ocr_hits.append((50.0, tuple(ln["rect"]), ln["text"]))
            if ocr_hits:
                ocr_hits.sort(key=lambda t: -t[0])
                rect, matched = ocr_hits[0][1], ocr_hits[0][2]
                x = rect[0] + rect[2] // 2
                y = rect[1] + rect[3] // 2
                return GroundingHit(ok=True, source="ocr", x=int(x), y=int(y),
                                    matched=matched[:60],
                                    candidates=[h[2] for h in ocr_hits[:5]])

        # --- rung 4: cloud-VLM pixel tier (opt-in + budget-capped, see vlm.py) ---
        if use_ocr:      # pixel contexts only make sense visually
            try:
                from .vlm import vlm_enabled, vlm_ground
                if vlm_enabled():
                    data = vlm_ground(text, hwnd=hwnd)
                    if data.get("found"):
                        return GroundingHit(ok=True, source="vlm",
                                            x=int(data["x"]), y=int(data["y"]),
                                            matched=(data.get("matched") or "")[:60])
            except Exception as e:
                log.debug(f"VLM rung failed: {e}")

        return GroundingHit(ok=False, reason="not found on UIA/OCR rungs",
                            candidates=candidates[:8])
