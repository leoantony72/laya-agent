"""
M5 - Cloud-VLM pixel tier (grounding ladder rung 4; plan §M5 "optional
accelerator, w/ cloud vision").

When the local rungs (UIA → OCR) miss, a vision-capable LLM can look at the
window screenshot and answer with pixel coordinates. Strictly opt-in and
budget-capped — this hardware has no GPU and every call costs money:

  WINBROW_CLOUD_VISION=1        enable the rung
  WINBROW_VISION_MODEL          default: openai/gpt-4o-mini (vision-cheap)
  WINBROW_VLM_BUDGET_PER_DAY    default 20 calls; a counter in data/ caps it

Never enabled by default; the ladder only climbs here when everything local
failed AND the budget is unspent.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("winbrow.computer.vlm")

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEFAULT_VISION_MODEL = "openai/gpt-4o-mini"

PROMPT = (
    "You are a screen-grounding assistant. The image is a Windows app window. "
    "Return ONLY JSON: {\"x\": <int>, \"y\": <int>, \"found\": <bool>, \"label\": \"<text at that point>\"} "
    "for the control whose label best matches: {text!r}. Coordinates are pixels in "
    "the provided image's own space. If no matching control exists, return found=false."
)


# --------------------------------------------------------------------- budget
def _budget_path() -> Path:
    root = Path(os.environ.get("WINBROW_OBSERVE_DIR", str(Path("data") / "observe")))
    return root / "vlm_budget.json"


def budget_used_today() -> int:
    try:
        data = json.loads(_budget_path().read_text(encoding="utf-8"))
        if data.get("date") == time.strftime("%Y-%m-%d"):
            return int(data.get("count", 0))
    except Exception:
        pass
    return 0


def _spend_one() -> None:
    """Increment the daily counter (best-effort)."""
    try:
        data = {"date": time.strftime("%Y-%m-%d"), "count": budget_used_today() + 1}
        p = _budget_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:
        log.debug(f"VLM budget write failed: {e}")


def vlm_budget_per_day() -> int:
    try:
        return max(1, int(os.environ.get("WINBROW_VLM_BUDGET_PER_DAY", "20")))
    except ValueError:
        return 20


def vlm_enabled() -> bool:
    """Rung is opt-in, key-required, and budget-respecting."""
    if os.environ.get("WINBROW_CLOUD_VISION", "0") != "1":
        return False
    if not os.environ.get("OPENROUTER_API_KEY", ""):
        return False
    return budget_used_today() < vlm_budget_per_day()


# ------------------------------------------------------------------ call
def _openrouter_chat_vision(png_path: Path, text: str, timeout: float = 25.0) -> dict:
    """Blocking OpenRouter vision call. Returns the parsed content dict."""
    key = os.environ.get("OPENROUTER_API_KEY", "")
    model = os.environ.get("WINBROW_VISION_MODEL", DEFAULT_VISION_MODEL)
    b64 = base64.b64encode(png_path.read_bytes()).decode("ascii")
    payload = {
        "model": model,
        "max_tokens": 200,
        "temperature": 0.0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT.format(text=text)},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/png;base64,{b64}"}},
            ],
        }],
    }
    import urllib.request
    req = urllib.request.Request(
        f"{OPENROUTER_BASE}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "HTTP-Referer": "https://github.com/winbrow",
            "X-Title": "WinBrow Desktop Agent",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    # tolerate prose-wrapped JSON
    m = re.search(r"\{[^{}]*\}", content, re.S)
    if not m:
        raise ValueError("VLM returned no JSON object")
    return json.loads(m.group(0))


def vlm_ground(text: str, hwnd: Optional[int] = None) -> dict:
    """Ground `text` on the window screenshot via the cloud VLM.

    Returns a GroundingHit.to_dict()-shaped result:
      {"found": bool, "source": "vlm", "x": int, "y": int, "matched": str}
    Coordinates are translated from image space to SCREEN space, since
    clicks happen on real pixels.
    """
    from .tools import _get_screen     # shared Screen singleton (mss/PIL)
    from .ladder import _norm

    out = {"found": False, "source": "vlm", "x": 0, "y": 0, "matched": "",
           "reason": ""}
    if not vlm_enabled():
        out["reason"] = "vlm rung disabled or budget spent"
        return out

    try:
        screen = _get_screen()
        if hwnd:
            from .tools import _get_driver
            win = next((w for w in _get_driver().list_windows() if w.hwnd == hwnd), None)
            view = screen.capture_window(win) if win is not None else screen.capture()
            origin_x, origin_y = (win.rect[0], win.rect[1]) if win is not None \
                else (view.rect[0], view.rect[1])
        else:
            view = screen.capture()
            origin_x, origin_y = view.rect[0], view.rect[1]

        result = _openrouter_chat_vision(Path(view.image_path), _norm(text) or text)
        _spend_one()

        if not result.get("found"):
            out["reason"] = "vlm found no matching control"
            return out
        ix, iy = int(result.get("x", 0)), int(result.get("y", 0))
        out.update({
            "found": True,
            "x": int(origin_x + max(ix, 0)),
            "y": int(origin_y + max(iy, 0)),
            "matched": str(result.get("label", ""))[:60],
        })
        return out
    except Exception as e:
        out["reason"] = f"vlm rung failed: {e}"
        log.debug(out["reason"])
        return out
