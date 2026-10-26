"""
Shared helpers for bundled converted-skill scripts.

Every script is launched by ``run_skill_script`` with ``--spec-json <json>``.
Scripts must print exactly one JSON object as their last output line:
    {"ok": true, "message": "...", "data": {...}, "path": "C:\\..."}
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def load_spec() -> dict[str, Any]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec-json", default="{}")
    args = parser.parse_args()
    try:
        spec = json.loads(args.spec_json or "{}")
    except Exception as exc:
        fail(f"Invalid JSON spec: {exc}")
    if not isinstance(spec, dict):
        fail("Spec must be a JSON object.")
    return spec


def emit(
    ok: bool,
    message: str,
    *,
    data: Any = None,
    path: str | Path | None = None,
    **extra: Any,
) -> None:
    payload: dict[str, Any] = {"ok": bool(ok), "message": str(message)}
    if data is not None:
        payload["data"] = data
    if path is not None:
        payload["path"] = str(path)
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(0 if ok else 1)


def fail(message: str, **extra: Any) -> None:
    emit(False, message, **extra)


def resolve_path(value: str | Path, *, default_dir: Path | None = None) -> Path:
    raw = os.path.expandvars(os.path.expanduser(str(value or "").strip()))
    if not raw:
        fail("A file path is required.")
    path = Path(raw)
    if not path.is_absolute() and default_dir is not None:
        path = default_dir / path
    return path.resolve()


def read_text_arg(spec: dict[str, Any], key: str = "content") -> str:
    """Return inline content or the contents of content_path."""
    inline = spec.get(key)
    if inline not in (None, ""):
        return str(inline)
    path_value = spec.get(f"{key}_path") or spec.get("input_path")
    if path_value:
        path = resolve_path(path_value)
        if not path.is_file():
            fail(f"Input file not found: {path}")
        return path.read_text(encoding="utf-8", errors="replace")
    return ""


def require_module(module: str, package: str) -> Any:
    try:
        import importlib
        return importlib.import_module(module)
    except Exception:
        fail(
            f"Missing Python package '{package}'. Install it with: "
            f"pip install {package}"
        )


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def clip(text: str, limit: int = 12000) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[TRUNCATED: {len(text) - limit} chars remain]"


def integration_secret(provider: str, key: str, default: str = "") -> str:
    """Read a value from the Integrations store, falling back to the default."""
    try:
        from winbrow.integrations import get_secret
        value = get_secret(provider, key)
        if value:
            return value
    except Exception:
        pass
    return default
