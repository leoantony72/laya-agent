"""
Machine-Built File/Folder/App Listings
=======================================
Helpers that enumerate real things on this machine so the decision model can
*choose* among observed options instead of code parsing names out of the
sentence. Everything here reads the OS; nothing reads the utterance.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}
DOC_EXTS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".txt", ".csv"}
MAX_SCAN_ENTRIES = 400


def user_home() -> str:
    return os.path.expanduser("~")


def _special_folder(kind: str) -> Optional[str]:
    """Resolve a Windows special folder without PowerShell."""
    home = user_home()
    mapping = {
        "desktop": os.path.join(home, "Desktop"),
        "documents": os.path.join(home, "Documents"),
        "downloads": os.path.join(home, "Downloads"),
        "pictures": os.path.join(home, "Pictures"),
        "music": os.path.join(home, "Music"),
        "videos": os.path.join(home, "Videos"),
        "home": home,
    }
    path = mapping.get(kind.lower())
    if path and os.path.isdir(path):
        return path
    return None


def known_folder_paths() -> dict[str, str]:
    """Nickname -> absolute path for folders that exist right now."""
    folders: dict[str, str] = {}
    for nick in ("desktop", "documents", "downloads", "pictures",
                 "music", "videos", "home"):
        path = _special_folder(nick)
        if path:
            folders[nick] = path
    appdata = os.environ.get("APPDATA")
    if appdata and os.path.isdir(appdata):
        folders["appdata"] = appdata
    temp = os.environ.get("TEMP")
    if temp and os.path.isdir(temp):
        folders["temp"] = temp
    return folders


def list_folders(limit: int = 60) -> list[dict[str, Any]]:
    """One entry per known folder: {id, nickname, path}."""
    out = []
    for i, (nick, path) in enumerate(sorted(known_folder_paths().items())):
        out.append({"id": f"folder:{nick}", "nickname": nick, "path": path})
        if len(out) >= limit:
            break
    return out


def list_files(folder_path: str, exts: Optional[set[str]] = None,
               limit: int = 25) -> list[dict[str, Any]]:
    """Files directly inside a folder, newest first: {id, name, path}."""
    entries: list[dict[str, Any]] = []
    try:
        with os.scandir(folder_path) as it:
            scored = []
            for entry in it:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if exts and os.path.splitext(entry.name)[1].lower() not in exts:
                        continue
                    scored.append((entry.stat(follow_symlinks=False).st_mtime, entry))
                except Exception:
                    continue
                if len(scored) >= MAX_SCAN_ENTRIES:
                    break
    except Exception:
        return []
    scored.sort(key=lambda s: s[0], reverse=True)
    for _, entry in scored[:limit]:
        entries.append({"id": f"file:{entry.path}", "name": entry.name, "path": entry.path})
    return entries


def find_file_by_name(name: str, search_roots: Optional[list[str]] = None,
                      depth: int = 3) -> Optional[str]:
    """Exact (case-insensitive) filename match; most recent wins."""
    target = name.strip().strip('"').strip("'")
    if not target:
        return None
    if os.path.isfile(target):
        return os.path.abspath(target)
    roots = search_roots or [
        p for p in (
            _special_folder("desktop"),
            _special_folder("documents"),
            _special_folder("downloads"),
            _special_folder("pictures"),
            _special_folder("videos"),
            _special_folder("music"),
            user_home(),
        ) if p
    ]
    low = target.lower()
    base_low = os.path.splitext(low)[0]
    hits: list[tuple[float, str]] = []
    scanned = 0

    def walk(path: str, level: int) -> None:
        nonlocal scanned
        if level > depth or scanned > 4000:
            return
        try:
            with os.scandir(path) as it:
                for entry in it:
                    scanned += 1
                    try:
                        if entry.is_file(follow_symlinks=False):
                            en = entry.name.lower()
                            if en == low or os.path.splitext(en)[0] == base_low:
                                hits.append((
                                    entry.stat(follow_symlinks=False).st_mtime,
                                    entry.path,
                                ))
                        elif entry.is_dir(follow_symlinks=False) and level < depth:
                            walk(entry.path, level + 1)
                    except Exception:
                        continue
        except Exception:
            return

    for root in dict.fromkeys(roots):
        walk(root, 0)
    if not hits:
        return None
    hits.sort(key=lambda h: h[0], reverse=True)
    return hits[0][1]


def running_apps_simple() -> list[str]:
    """Pretty process names for option lists (best effort, capped)."""
    try:
        import psutil
    except ImportError:
        return []
    seen: list[str] = []
    try:
        for proc in psutil.process_iter(["name"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if not name.endswith(".exe"):
                    continue
                base = name[:-4]
                if base.startswith(("svchost", "system", "registry", "smss",
                                     "csrss", "wininit", "services", "lsass",
                                     "conhost", "fontdrvhost", "dwm", "sihost",
                                     "ctfmon", "taskhostw", "runtimebroker")):
                    continue
                if base not in seen:
                    seen.append(base)
            except Exception:
                continue
    except Exception:
        pass
    return seen[:40]


def installed_apps(limit: int = 40) -> list[str]:
    """App names from Start Menu shortcuts + Uninstall registry (capped)."""
    found: list[str] = []
    for var, sub in (("APPDATA", r"Microsoft\Windows\Start Menu\Programs"),
                     ("PROGRAMDATA", r"Microsoft\Windows\Start Menu\Programs")):
        base = os.environ.get(var)
        if not base:
            continue
        root = os.path.join(base, sub)
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if fn.lower().endswith(".lnk"):
                    name = os.path.splitext(fn)[0].strip()
                    if name and name not in found:
                        found.append(name)
            if len(found) >= limit:
                return found[:limit]
    try:
        import winreg
        for hive, key in (
            (winreg.HKEY_LOCAL_MACHINE,
             r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER,
             r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        ):
            try:
                with winreg.OpenKey(hive, key) as root:
                    for i in range(winreg.QueryInfoKey(root)[0]):
                        try:
                            with winreg.OpenKey(root, winreg.EnumKey(root, i)) as sub:
                                name, _ = winreg.QueryValueEx(sub, "DisplayName")
                                if name and str(name).strip() not in found:
                                    found.append(str(name).strip())
                        except Exception:
                            continue
                        if len(found) >= limit:
                            return found[:limit]
            except Exception:
                continue
    except ImportError:
        pass
    return found[:limit]


_MAX_TEXT_CONTENT_CHARS = 200_000
_TEXT_EXTENSIONS = r"\.(txt|md|log|csv|json|ps1|py|go|html|css|js|ini)$"


def _text_target_folder(value: str) -> Path:
    home = Path(user_home())
    key = (value or "Desktop").strip().lower()
    if key.startswith("download"):
        return home / "Downloads"
    if key.startswith("document"):
        return home / "Documents"
    if key.startswith(("picture", "image")):
        return home / "Pictures"
    if key.startswith("video"):
        return home / "Videos"
    return home / "Desktop"


def write_named_text_file(args: dict, base_dir: Optional[Path] = None) -> dict[str, Any]:
    """Create/overwrite a text file in a known folder without PowerShell."""
    filename = str(args.get("filename") or "").strip().strip("\"'")
    if not filename:
        return {"success": False, "output": "No filename provided."}
    if Path(filename).name != filename:
        return {"success": False, "output": "Invalid filename: path separators are not allowed."}
    if not re.search(_TEXT_EXTENSIONS, filename, re.IGNORECASE):
        filename += ".txt"

    content = str(args.get("content") or "")
    if len(content) > _MAX_TEXT_CONTENT_CHARS:
        return {
            "success": False,
            "output": f"Content too large ({len(content)} chars; max {_MAX_TEXT_CONTENT_CHARS}).",
        }

    folder = Path(base_dir) if base_dir is not None else _text_target_folder(
        str(args.get("folder") or "Desktop"))
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / filename
        if folder.resolve() not in path.resolve().parents:
            return {"success": False, "output": "Invalid filename: escapes the target folder."}
        path.write_text(content, encoding="utf-8", newline="\n")
    except OSError as exc:
        return {"success": False, "output": f"Could not create file: {exc}"}

    return {
        "success": True,
        "output": f"Created file: {path}",
        "path": str(path),
        "bytes": len(content.encode("utf-8")),
    }
