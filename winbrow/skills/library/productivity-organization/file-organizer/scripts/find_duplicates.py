"""Find duplicate files and preview/apply an organization plan."""

from __future__ import annotations

import hashlib
import shutil
from collections import defaultdict
from pathlib import Path

from winbrow.skill_script_utils import emit, fail, load_spec, resolve_path

_CATEGORIES = {
    "images": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg"},
    "documents": {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".txt", ".md", ".csv"},
    "audio": {".mp3", ".wav", ".flac", ".m4a", ".ogg"},
    "video": {".mp4", ".mov", ".mkv", ".avi", ".webm"},
    "archives": {".zip", ".7z", ".rar", ".tar", ".gz"},
    "code": {".py", ".js", ".ts", ".html", ".css", ".json", ".yaml", ".yml", ".ps1"},
}


def _files(root: Path, limit: int = 5000) -> list[Path]:
    paths = []
    for path in root.rglob("*"):
        if len(paths) >= limit:
            break
        if path.is_file() and not path.is_symlink():
            paths.append(path)
    return paths


def duplicates(spec: dict) -> None:
    root = resolve_path(spec.get("path") or spec.get("input_dir") or "")
    if not root.is_dir():
        fail(f"Folder not found: {root}")
    minimum = int(spec.get("min_size") or 1)
    by_size: dict[int, list[Path]] = defaultdict(list)
    for path in _files(root, int(spec.get("max_files") or 5000)):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size >= minimum:
            by_size[size].append(path)

    groups = []
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        by_hash: dict[str, list[Path]] = defaultdict(list)
        for path in paths:
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            by_hash[digest].append(path)
        for digest, matches in by_hash.items():
            if len(matches) > 1:
                groups.append({
                    "hash": digest,
                    "size": size,
                    "paths": [str(path) for path in matches],
                })
    groups.sort(key=lambda item: item["size"], reverse=True)
    total_wasted = sum(item["size"] * (len(item["paths"]) - 1) for item in groups)
    emit(
        True,
        f"Found {len(groups)} duplicate group(s); {total_wasted} bytes recoverable.",
        data={"groups": groups, "recoverable_bytes": total_wasted},
    )


def plan(spec: dict) -> None:
    root = resolve_path(spec.get("path") or spec.get("input_dir") or "")
    if not root.is_dir():
        fail(f"Folder not found: {root}")
    target = resolve_path(spec.get("target") or root / "Organized")
    moves = []
    for path in _files(root, int(spec.get("max_files") or 2000)):
        if str(path).startswith(str(target)):
            continue
        category = "other"
        for name, extensions in _CATEGORIES.items():
            if path.suffix.lower() in extensions:
                category = name
                break
        moves.append({
            "source": str(path),
            "destination": str(target / category / path.name),
            "category": category,
        })
    emit(True, f"Proposed {len(moves)} move(s).", data={"moves": moves, "target": str(target)})


def apply(spec: dict) -> None:
    moves = spec.get("moves") or []
    if not isinstance(moves, list) or not moves:
        fail("Provide a moves list from the plan operation.")
    dry_run = bool(spec.get("dry_run", True))
    completed = []
    for item in moves:
        if not isinstance(item, dict):
            continue
        source = resolve_path(item.get("source") or "")
        destination = resolve_path(item.get("destination") or "")
        if not source.is_file():
            fail(f"Source not found: {source}")
        if destination.exists() and not spec.get("overwrite"):
            fail(f"Destination already exists: {destination}")
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
        completed.append({"source": str(source), "destination": str(destination)})
    prefix = "Would move" if dry_run else "Moved"
    emit(True, f"{prefix} {len(completed)} file(s).", data={
        "dry_run": dry_run, "moves": completed,
    })


def main() -> None:
    spec = load_spec()
    operation = str(spec.get("operation") or "duplicates").lower()
    handlers = {"duplicates": duplicates, "dupes": duplicates, "plan": plan, "apply": apply}
    handler = handlers.get(operation)
    if handler is None:
        fail(f"Unknown file-organizer operation '{operation}'. Use duplicates, plan, or apply.")
    handler(spec)


if __name__ == "__main__":
    main()
