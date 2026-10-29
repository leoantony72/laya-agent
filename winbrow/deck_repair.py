"""Repair and validate html-ppt decks before exporting them to PDF.

The planner is good at writing slide *content* but sometimes replaces the
scaffolded ``index.html`` with hand-written markup that:

  * links a stylesheet that does not exist (``assets/index.css``),
  * drops ``assets/base.css`` / the theme / ``assets/runtime.js``,
  * omits the ``.deck`` wrapper or the ``tpl-*`` body class.

Chrome then prints unstyled HTML: no slide pagination, light text on white,
overflowing lines. This module restores the design system idempotently and
reports anything it cannot fix.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

# Assets every deck needs, in cascade order. The theme stylesheet comes after
# these so its token overrides win.
_REQUIRED_CSS = (
    "assets/fonts.css",
    "assets/base.css",
    "assets/animations/animations.css",
)
_RUNTIME = "assets/runtime.js"
_DEFAULT_THEME = "assets/themes/minimal-white.css"

_SKILL_ASSETS = (
    Path(__file__).resolve().parent
    / "skills" / "library" / "document-processing" / "html-ppt"
)

_LINK_RE = re.compile(r"<link\b[^>]*>", re.IGNORECASE)
_HREF_RE = re.compile(r'href\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
_SLIDE_RE = re.compile(
    r'<section\b[^>]*class\s*=\s*["\'][^"\']*\bslide\b[^"\']*["\'][^>]*>.*?</section\s*>',
    re.IGNORECASE | re.DOTALL,
)
_DECK_RE = re.compile(
    r'class\s*=\s*["\'][^"\']*\bdeck\b[^"\']*["\']', re.IGNORECASE
)
_BODY_RE = re.compile(r"<body\b([^>]*)>", re.IGNORECASE)
_THEME_CLASS_RE = re.compile(r"\.(tpl-[a-z0-9_-]+)\s*\{", re.IGNORECASE)
_H1_RE = re.compile(r"<h1\b([^>]*)>", re.IGNORECASE)
_H2_RE = re.compile(r"<h2\b([^>]*)>", re.IGNORECASE)
_CLASS_ATTR_RE = re.compile(r'class\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE)
_CSS_VAR_RE = re.compile(r"(--[a-z0-9_-]+)\s*:\s*(#[0-9a-fA-F]{3,8})\b")
_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _rgb(hex_color: str) -> tuple[int, int, int] | None:
    match = _HEX_RE.match(str(hex_color or "").strip())
    if not match:
        return None
    raw = match.group(1)
    if len(raw) == 3:
        raw = "".join(char * 2 for char in raw)
    return tuple(int(raw[index:index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def _luminance(rgb: tuple[int, int, int]) -> float:
    def channel(value: int) -> float:
        value = value / 255.0
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(part) for part in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(a: str, b: str) -> float | None:
    rgb_a, rgb_b = _rgb(a), _rgb(b)
    if rgb_a is None or rgb_b is None:
        return None
    lum_a, lum_b = _luminance(rgb_a), _luminance(rgb_b)
    lighter, darker = max(lum_a, lum_b), min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


def _contrast_warnings(out_dir: Path, css_hrefs: list[str]) -> list[str]:
    """Check the theme's bg/text/accent tokens (hex values only)."""
    variables: dict[str, str] = {}
    for href in css_hrefs:
        try:
            text = _read((out_dir / href).resolve())
        except OSError:
            continue
        for name, value in _CSS_VAR_RE.findall(text):
            variables[name.lower()] = value
    warnings: list[str] = []
    bg = variables.get("--bg") or variables.get("--background")
    text = variables.get("--text-1") or variables.get("--text")
    if bg and text:
        ratio = _contrast_ratio(bg, text)
        if ratio is not None and ratio < 4.5:
            warnings.append(
                f"Theme text/background contrast is {ratio:.1f}:1 "
                f"({text} on {bg}); WCAG AA needs 4.5:1 for body text."
            )
    accent = variables.get("--accent")
    accent_ink = variables.get("--accent-ink")
    if accent and accent_ink:
        ratio = _contrast_ratio(accent, accent_ink)
        if ratio is not None and ratio < 4.5:
            warnings.append(
                f"Theme accent text contrast is {ratio:.1f}:1 "
                f"({accent_ink} on {accent}); WCAG AA needs 4.5:1."
            )
    return warnings

_FALLBACK_CSS = """
/* winbrow deck fallback: makes hand-authored semantic markup legible even when
   a template only styles .h1/.h2 utility classes. Uses theme tokens only. */
.deck .slide h1, .deck .slide .h1 {
  font-family: var(--font-display); font-size: clamp(44px, 4.2vw, 72px);
  line-height: 1.06; font-weight: 800; letter-spacing: var(--letter-tight);
  margin: 0 0 18px; color: var(--text-1);
}
.deck .slide h2, .deck .slide .h2 {
  font-size: clamp(34px, 3.2vw, 54px); line-height: 1.12; font-weight: 700;
  letter-spacing: var(--letter-tight); margin: 0 0 22px; color: var(--text-1);
}
.deck .slide h3 { font-size: 30px; line-height: 1.2; margin: 0 0 10px; color: var(--text-1); }
.deck .slide p { font-size: 21px; line-height: 1.5; margin: 0 0 14px; color: var(--text-2); }
.deck .slide .subtitle, .deck .slide .lede {
  font-size: 24px; line-height: 1.45; color: var(--text-2); max-width: 62ch;
}
.deck .slide .meta, .deck .slide .tags {
  font-family: var(--font-mono); font-size: 14px; color: var(--text-3);
  letter-spacing: .02em;
}
.deck .slide ul, .deck .slide ol { margin: 0 0 0 1.15em; padding: 0; }
.deck .slide li { font-size: 21px; line-height: 1.45; margin: 0 0 10px; color: var(--text-1); }
.deck .slide li > span { color: var(--text-1); }
.deck .slide strong { color: var(--text-1); }
.deck .slide em { color: var(--accent-2); font-style: italic; }
.deck .slide pre {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius-sm); padding: 18px 22px; margin: 12px 0 18px;
  font-family: var(--font-mono); font-size: 16px; line-height: 1.55;
  color: var(--text-1); overflow: hidden; white-space: pre-wrap;
}
.deck .slide code { font-family: var(--font-mono); color: var(--accent-2); }
.deck .slide pre code { color: var(--text-1); }
.deck .slide .note {
  border-left: 3px solid var(--accent); padding: 4px 0 4px 14px;
  color: var(--text-2); font-size: 18px;
}
.deck .slide.title, .deck .slide[data-layout="title"] { justify-content: center; }
"""


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _local_href(href: str) -> bool:
    raw = (href or "").strip()
    return bool(raw) and not raw.lower().startswith(
        ("http://", "https://", "//", "data:", "mailto:", "tel:", "javascript:", "#")
    )


def _ensure_asset(out_dir: Path, rel: str) -> bool:
    """Copy a missing design-system asset into the deck. Returns True if copied."""
    target = out_dir / rel
    if target.exists():
        return False
    source = _SKILL_ASSETS / rel
    if not source.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return True


def _stylesheet_links(html: str) -> list[re.Match]:
    links = []
    for match in _LINK_RE.finditer(html):
        tag = match.group(0)
        if "stylesheet" not in tag.lower():
            continue
        links.append(match)
    return links


def _href_of(tag: str) -> str:
    match = _HREF_RE.search(tag)
    return match.group(1).strip() if match else ""


def _theme_class_from_css(out_dir: Path, href: str) -> str:
    path = (out_dir / href).resolve()
    try:
        text = _read(path)
    except OSError:
        return ""
    match = _THEME_CLASS_RE.search(text)
    return match.group(1) if match else ""


def _insert_link(html: str, href: str) -> tuple[str, bool]:
    if re.search(
        r'<link\b[^>]*href\s*=\s*["\']' + re.escape(href) + r'["\']',
        html, re.IGNORECASE,
    ):
        return html, False
    tag = f'<link rel="stylesheet" href="{href}">'
    # Insert before the first stylesheet link so base/fonts load first; if
    # there is none, put it at the end of <head>.
    match = None
    for link in _stylesheet_links(html):
        match = link
        break
    if match is not None:
        return html[:match.start()] + tag + "\n" + html[match.start():], True
    if re.search(r"</head\s*>", html, re.IGNORECASE):
        return re.sub(r"</head\s*>", tag + "\n</head>", html, count=1, flags=re.I), True
    return tag + "\n" + html, True


def _append_to_head(html: str, fragment: str) -> str:
    if re.search(r"</head\s*>", html, re.IGNORECASE):
        return re.sub(r"</head\s*>", fragment + "\n</head>", html,
                      count=1, flags=re.I)
    return fragment + "\n" + html


def _append_to_body(html: str, fragment: str) -> str:
    if re.search(r"</body\s*>", html, re.IGNORECASE):
        return re.sub(r"</body\s*>", fragment + "\n</body>", html,
                      count=1, flags=re.I)
    return html + "\n" + fragment


def _add_class(attr_text: str, class_name: str) -> str:
    """Add a class to a tag's attribute text, preserving an existing class."""
    class_match = _CLASS_ATTR_RE.search(attr_text)
    if class_match:
        classes = class_match.group(1).split()
        if class_name in classes:
            return attr_text
        classes.append(class_name)
        return (
            attr_text[:class_match.start(1)]
            + " ".join(classes)
            + attr_text[class_match.end(1):]
        )
    return attr_text.rstrip() + f' class="{class_name}"'


def _normalise_headings(html: str) -> tuple[str, int]:
    changed = 0

    def add(match: re.Match, cls: str) -> str:
        nonlocal changed
        attrs = match.group(1) or ""
        updated = _add_class(attrs, cls)
        if updated == attrs:
            return match.group(0)
        changed += 1
        return f"<{match.group(0)[1:3]}{updated}>"

    html = _H1_RE.sub(lambda m: add(m, "h1"), html)
    html = _H2_RE.sub(lambda m: add(m, "h2"), html)
    return html, changed


def repair_deck(html_path: str | Path, *, write: bool = True) -> dict[str, Any]:
    """Make a deck exportable. Idempotent; never changes slide text.

    Returns ``{ok, repaired, issues, repairs, errors, warnings, slides}``.
    """
    path = Path(html_path).resolve()
    if not path.is_file():
        return {
            "ok": False, "repaired": False, "issues": [], "repairs": [],
            "errors": [f"Deck HTML not found: {path}"], "warnings": [], "slides": 0,
        }
    out_dir = path.parent
    original = _read(path)
    html = original
    issues: list[str] = []
    repairs: list[str] = []
    warnings: list[str] = []
    copied: list[str] = []

    # 1. Copy any missing design-system assets.
    for rel in (*_REQUIRED_CSS, _RUNTIME):
        if _ensure_asset(out_dir, rel):
            copied.append(rel)

    # 2. Remove stylesheet links whose local target is missing. This is the
    #    "assets/index.css" case: the planner invented a file it never wrote.
    for link in reversed(_stylesheet_links(html)):
        href = _href_of(link.group(0))
        if not href or not _local_href(href):
            continue
        if (out_dir / href).exists():
            continue
        issues.append(f"missing stylesheet: {href}")
        html = html[:link.start()] + html[link.end():]
        repairs.append(f"removed broken stylesheet link {href}")

    # 3. Identify a usable theme stylesheet. Prefer style.css, then an existing
    #    assets/themes/*.css, then link the bundled minimal theme.
    theme_href = ""
    for link in _stylesheet_links(html):
        href = _href_of(link.group(0))
        if href and _local_href(href) and (out_dir / href).exists():
            if href in _REQUIRED_CSS:
                continue
            if (out_dir / href).name == "style.css" or "/themes/" in href.replace("\\", "/"):
                theme_href = href
                break
            if not theme_href:
                theme_href = href
    if not theme_href and (out_dir / "style.css").is_file():
        theme_href = "style.css"
    if not theme_href:
        _ensure_asset(out_dir, _DEFAULT_THEME)
        if (out_dir / _DEFAULT_THEME).is_file():
            theme_href = _DEFAULT_THEME
            repairs.append(f"added default theme {_DEFAULT_THEME}")
        else:
            warnings.append(
                "No theme stylesheet found; base.css defaults (light, high "
                "contrast) will be used."
            )

    # 4. Ensure required CSS and the theme are linked, in cascade order.
    #    Inserting in reverse before the first stylesheet yields
    #    fonts -> base -> animations -> theme.
    for rel in reversed(_REQUIRED_CSS):
        if not (out_dir / rel).is_file():
            warnings.append(f"design asset missing and unavailable: {rel}")
            continue
        html, changed = _insert_link(html, rel)
        if changed:
            repairs.append(f"linked {rel}")
    if theme_href and not re.search(
        r'<link\b[^>]*href\s*=\s*["\']' + re.escape(theme_href) + r'["\']',
        html, re.IGNORECASE,
    ):
        html = _append_to_head(
            html, f'<link rel="stylesheet" href="{theme_href}">')
        repairs.append(f"linked theme {theme_href}")

    # 5. Ensure the runtime script (keyboard nav + print logo painting).
    if (out_dir / _RUNTIME).is_file() and not re.search(
        r'<script\b[^>]*src\s*=\s*["\']' + re.escape(_RUNTIME) + r'["\']',
        html, re.IGNORECASE,
    ):
        html = _append_to_body(html, f'<script src="{_RUNTIME}"></script>')
        repairs.append(f"added {_RUNTIME}")

    # 6. Ensure <div class="deck"> wraps the slides.
    slide_matches = list(_SLIDE_RE.finditer(html))
    if slide_matches and not _DECK_RE.search(html):
        start, end = slide_matches[0].start(), slide_matches[-1].end()
        html = html[:start] + '<div class="deck">\n' + html[start:end] + \
            "\n</div>" + html[end:]
        repairs.append("wrapped slides in .deck")

    # 7. Ensure the scoped theme class (style.css uses .tpl-<name>).
    if theme_href:
        theme_class = _theme_class_from_css(out_dir, theme_href)
        if theme_class:
            body_match = _BODY_RE.search(html)
            if body_match:
                attrs = body_match.group(1) or ""
                updated = _add_class(attrs, theme_class)
                if updated != attrs:
                    html = html[:body_match.start(1)] + updated + \
                        html[body_match.end(1):]
                    repairs.append(f"added body class {theme_class}")

    # 8. Give bare h1/h2 the theme utility class so templates that only style
    #    .h1/.h2 still format hand-authored slides.
    html, changed = _normalise_headings(html)
    if changed:
        repairs.append(f"normalised {changed} heading class(es)")

    # 9. Inject a token-based fallback for semantic markup (subtitle/meta/etc.).
    if "data-winbrow-deck-fallback" not in html:
        html = _append_to_head(
            html,
            '<style data-winbrow-deck-fallback>' + _FALLBACK_CSS + "</style>",
        )
        repairs.append("added semantic fallback CSS")

    # 10. Verify every local reference again.
    checked = 0
    missing: list[str] = []
    for link in _stylesheet_links(html):
        href = _href_of(link.group(0))
        if not href or not _local_href(href):
            continue
        checked += 1
        if not (out_dir / href).resolve().exists():
            missing.append(href)
    for match in re.finditer(
        r'<(?:script|img)\b[^>]*(?:src|href)\s*=\s*["\']([^"\']+)["\']',
        html, re.IGNORECASE,
    ):
        href = match.group(1).strip()
        if not href or not _local_href(href):
            continue
        checked += 1
        if not (out_dir / href).resolve().exists():
            missing.append(href)
    errors = [f"unresolved local reference: {item}" for item in sorted(set(missing))]

    css_hrefs: list[str] = []
    for link in _stylesheet_links(html):
        href = _href_of(link.group(0))
        if href and _local_href(href) and (out_dir / href).resolve().is_file():
            css_hrefs.append(href)
    warnings.extend(_contrast_warnings(out_dir, css_hrefs))

    if write and html != original:
        _write(path, html)

    return {
        "ok": not errors,
        "repaired": html != original,
        "issues": issues,
        "repairs": repairs,
        "errors": errors,
        "warnings": warnings,
        "slides": len(slide_matches),
        "links_checked": checked,
        "assets_copied": copied,
    }
