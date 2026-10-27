"""Scaffold an html-ppt deck from a bundled template.

Spec:
  {"name": "q3-review",
   "output_parent": "C:\\Users\\me\\Desktop",   # optional
   "template": "deck",                          # deck | pitch-deck | tech-sharing | ...
   "overwrite": false}

The deck is copied out of the skill, asset links are rewritten for its depth,
and every local reference is verified before success is reported.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from winbrow.skill_script_utils import emit, fail, load_spec, resolve_path

SKILL_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = SKILL_ROOT / "templates"
ASSET_REF = re.compile(r"([\"'(])(?:\.\./)*assets/")
HTML_REF = re.compile(r"(?:href|src)\s*=\s*[\"']([^\"']+)[\"']", re.I)
CSS_REF = re.compile(r"url\(\s*[\"']?([^\"')]+)[\"']?\s*\)", re.I)
SKIP_PREFIXES = ("data:", "mailto:", "tel:", "javascript:", "#", "//")


def _default_parent() -> Path:
    home = Path.home()
    for name in ("Desktop", "Documents"):
        candidate = home / name
        if candidate.is_dir():
            return candidate
    return home


def _resolve_template(name: str) -> Path:
    raw = (name or "deck").strip()
    direct = Path(raw)
    if direct.exists():
        return direct.resolve()
    if raw == "deck" and (TEMPLATES / "deck.html").is_file():
        return TEMPLATES / "deck.html"
    full = TEMPLATES / "full-decks" / raw
    if full.is_dir():
        return full
    single = TEMPLATES / "single-page" / f"{raw}.html"
    if single.is_file():
        return single
    showcase = TEMPLATES / f"{raw}.html"
    if showcase.is_file():
        return showcase
    fail(f"No template named '{raw}'. Use deck, a full-deck name, or a single-page layout.")


def _rewrite_assets(path: Path, prefix: str) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    rewritten = ASSET_REF.sub(lambda match: match.group(1) + prefix + "assets/", text)
    if rewritten != text:
        path.write_text(rewritten, encoding="utf-8")


def _local_refs(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    refs = HTML_REF.findall(text) if path.suffix.lower() in {".html", ".htm"} else []
    refs += CSS_REF.findall(text) if path.suffix.lower() == ".css" else []
    cleaned = []
    for ref in refs:
        ref = ref.strip()
        if not ref or ref.lower().startswith(SKIP_PREFIXES):
            continue
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", ref):
            continue
        ref = re.split(r"[?#]", ref, 1)[0]
        if ref:
            cleaned.append(ref)
    return sorted(set(cleaned))


def _verify(out_dir: Path) -> tuple[int, list[str]]:
    checked = 0
    missing: list[str] = []
    for path in out_dir.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".html", ".htm", ".css"}:
            continue
        for ref in _local_refs(path):
            checked += 1
            target = (path.parent / ref).resolve()
            if not target.exists():
                missing.append(f"{path.relative_to(out_dir)} -> {ref}")
    return checked, missing


def main() -> None:
    spec = load_spec()
    name = str(spec.get("name") or "").strip()
    if not name:
        fail("'name' is required, e.g. 'q3-review'.")
    if any(char in name for char in '<>:"/\\|?*'):
        fail("'name' contains characters that are invalid in a folder name.")

    template = _resolve_template(str(spec.get("template") or "deck"))
    parent = resolve_path(spec.get("output_parent") or _default_parent())
    out_dir = parent / name
    overwrite = bool(spec.get("overwrite", False))
    if out_dir.exists():
        if not overwrite:
            fail(f"{out_dir} already exists. Pass overwrite=true only after confirming.")
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if template.is_dir():
        shutil.copytree(template, out_dir, dirs_exist_ok=True)
    else:
        shutil.copy2(template, out_dir / "index.html")

    # Copy the design system into the deck so the folder is portable and works
    # even when the skill lives on a different drive (Windows relpath fails
    # across drives).
    assets = SKILL_ROOT / "assets"
    if assets.is_dir():
        shutil.copytree(assets, out_dir / "assets", dirs_exist_ok=True)
    prefix = ""

    for path in out_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".html", ".htm", ".css", ".js"}:
            _rewrite_assets(path, prefix)

    index = out_dir / "index.html"
    if not index.is_file():
        fail(f"Template produced no index.html in {out_dir}")

    checked, missing = _verify(out_dir)
    if missing:
        fail(
            "Unresolved local references after scaffolding:\n"
            + "\n".join(f"- {item}" for item in missing[:15])
        )

    emit(
        True,
        f"Created deck: {index}\n"
        f"template: {template}\n"
        f"assets: assets/ (copied into the deck)\n"
        f"references checked: {checked}\n"
        "Open index.html, then press T for themes and O for the slide overview.",
        path=index,
        paths=[str(index)],
        data={"assets_prefix": "assets/", "references": checked},
    )


if __name__ == "__main__":
    main()
