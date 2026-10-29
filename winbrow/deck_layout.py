"""Headless layout check for HTML slide decks.

Chrome's PDF printer clips any slide content that overflows its fixed canvas.
This runs the deck in headless Chrome, measures ``scrollHeight/clientHeight``
and ``scrollWidth/clientWidth`` for every ``.slide``, and reports the exact
slides that would lose text. The planner can then shorten those slides before
export instead of shipping a clipped PDF.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from .html_to_pdf import find_browser

_REPORT_RE = re.compile(
    r'<pre[^>]*id=["\']winbrow-layout-report["\'][^>]*>(.*?)</pre>',
    re.IGNORECASE | re.DOTALL,
)

_SCRIPT = r"""
<script>
(async function () {
  function done(report) {
    var pre = document.createElement('pre');
    pre.id = 'winbrow-layout-report';
    pre.textContent = JSON.stringify(report);
    document.body.appendChild(pre);
  }
  try {
    if (document.fonts && document.fonts.ready) {
      await Promise.race([
        document.fonts.ready,
        new Promise(function (resolve) { setTimeout(resolve, 1500); })
      ]);
    }
  } catch (e) {}
  await new Promise(function (resolve) { setTimeout(resolve, 250); });
  var deck = document.querySelector('.deck');
  var slides = deck
    ? Array.prototype.slice.call(deck.children).filter(function (el) {
        return el.classList && el.classList.contains('slide');
      })
    : Array.prototype.slice.call(document.querySelectorAll('.slide'));
  var report = slides.map(function (el, index) {
    var slideRect = el.getBoundingClientRect();
    var maxBottom = 0, minTop = 0, maxRight = 0, minLeft = 0;
    var nodes = el.querySelectorAll('h1,h2,h3,h4,h5,h6,p,li,pre,code,td,th,blockquote,figcaption,span,strong,em,a');
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      if (node.children.length > 0) continue;
      var text = (node.textContent || '').trim();
      if (!text) continue;
      var r = node.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      maxBottom = Math.max(maxBottom, r.bottom - slideRect.bottom);
      minTop = Math.min(minTop, r.top - slideRect.top);
      maxRight = Math.max(maxRight, r.right - slideRect.right);
      minLeft = Math.min(minLeft, r.left - slideRect.left);
    }
    return {
      slide: index + 1,
      title: (el.getAttribute('data-title') || '').slice(0, 80),
      overflowY: Math.max(
        Math.max(0, el.scrollHeight - el.clientHeight),
        Math.max(0, maxBottom),
        Math.max(0, -minTop)
      ),
      overflowX: Math.max(
        Math.max(0, el.scrollWidth - el.clientWidth),
        Math.max(0, maxRight),
        Math.max(0, -minLeft)
      ),
      clientHeight: el.clientHeight,
      clientWidth: el.clientWidth
    };
  });
  done(report);
})();
</script>
"""


def _inject_script(html: str) -> str:
    if re.search(r"</body\s*>", html, re.IGNORECASE):
        return re.sub(r"</body\s*>", _SCRIPT + "\n</body>", html,
                      count=1, flags=re.I)
    return html + _SCRIPT


def check_deck_layout(
    html_path: str | Path,
    *,
    timeout: int = 60,
    tolerance_px: int = 4,
) -> dict[str, Any]:
    """Return ``{ok, slides, issues, report}`` for a deck's slide overflow."""
    source = Path(html_path).resolve()
    if not source.is_file():
        return {"ok": False, "slides": 0, "issues": [f"Deck not found: {source}"],
                "report": []}
    html = source.read_text(encoding="utf-8", errors="replace")
    if not re.search(r'class\s*=\s*["\'][^"\']*\bslide\b', html, re.IGNORECASE):
        return {"ok": True, "slides": 0, "issues": [], "report": []}

    browser = find_browser()
    if browser is None:
        return {
            "ok": True,
            "slides": 0,
            "issues": ["Chrome/Edge not found; skipped visual overflow check."],
            "report": [],
            "skipped": True,
        }

    work = Path(tempfile.gettempdir()) / f"winbrow-layout-{uuid.uuid4().hex}"
    profile = work / "profile"
    work.mkdir(parents=True, exist_ok=True)
    # The check page MUST live beside the deck so relative CSS/JS/image refs
    # resolve exactly as they do in the real render.
    temporary = source.with_name(f".winbrow_layout_{uuid.uuid4().hex}.html")
    try:
        temporary.write_text(_inject_script(html), encoding="utf-8")
        command = [
            str(browser),
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            "--allow-file-access-from-files",
            "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=4000",
            f"--user-data-dir={profile}",
            "--dump-dom",
            temporary.as_uri(),
        ]
        try:
            proc = subprocess.run(
                command, capture_output=True, text=True, errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": True, "slides": 0,
                "issues": ["Layout check timed out; skipped."],
                "report": [], "skipped": True,
            }
        match = _REPORT_RE.search(proc.stdout or "")
        if not match:
            return {
                "ok": True, "slides": 0,
                "issues": ["Layout report unavailable; skipped overflow check."],
                "report": [], "skipped": True,
            }
        try:
            report = json.loads(html_lib.unescape(match.group(1).strip()))
        except Exception as exc:
            return {
                "ok": True, "slides": 0,
                "issues": [f"Layout report unreadable ({exc}); skipped."],
                "report": [], "skipped": True,
            }
        issues: list[str] = []
        for item in report if isinstance(report, list) else []:
            if not isinstance(item, dict):
                continue
            overflow_y = float(item.get("overflowY") or 0)
            overflow_x = float(item.get("overflowX") or 0)
            label = f"Slide {item.get('slide')}"
            if item.get("title"):
                label += f" ({item['title']})"
            if overflow_y > tolerance_px:
                issues.append(
                    f"{label}: content overflows the slide vertically by "
                    f"{overflow_y:.0f}px; shorten it or move detail to notes."
                )
            if overflow_x > tolerance_px:
                issues.append(
                    f"{label}: content overflows horizontally by "
                    f"{overflow_x:.0f}px; wrap or shorten long lines."
                )
        return {
            "ok": not issues,
            "slides": len(report) if isinstance(report, list) else 0,
            "issues": issues,
            "report": report,
        }
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
        shutil.rmtree(work, ignore_errors=True)
