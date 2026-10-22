"""Installed-application index and fuzzy launch resolution.

Builds a catalog from Start Menu shortcuts, Get-StartApps, the App Paths
registry and known install locations, then matches a spoken name with the
same pass order used by Windows launchers: exact, prefix/token, acronym,
substring, fuzzy. Resolution never invents a target: an unmatched name fails
with suggestions instead of launching a guess.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from typing import Iterable, Optional

log = logging.getLogger("winbrow.apps")

try:
    from rapidfuzz import fuzz as _rf_fuzz
except Exception:  # pragma: no cover - optional fast path
    _rf_fuzz = None

MATCH_MIN = 0.78
SUGGEST_MIN = 0.55
_CATALOG_TTL_S = 300.0

_NOISE_WORDS = {
    "app", "apps", "application", "program", "the", "my", "a", "an",
    "please", "pls", "for", "me", "thanks", "thank", "you", "now",
}
_TRAILING_NOISE = re.compile(
    r"\s+(?:please|pls|thanks|thank\s+you|for\s+me|now)\s*$", re.I)
_TRAILING_KIND = re.compile(r"\s+(?:app|application|program)\s*$", re.I)
_OUTER_JUNK = " \t.,!?;:\"'()[]{}"
_VERSION = re.compile(r"\s+(?:v?\d+(?:\.\d+)*)\s*$", re.I)
_PARENS = re.compile(r"\s*[\(\[][^)\]]*[\)\]]\s*")
_SKIP_SHORTCUT = re.compile(
    r"(?:^|\b)(?:uninstall|release notes|documentation|user guide|website|"
    r"help|readme|license|what'?s new|check for updates|repair|modify|"
    r"send to|samples|administrative tools|guide|manual|reference|"
    r"tutorial)\b", re.I)
_IDENTITY_SYMBOLS = re.compile(r"\+\+|#|\.net\b", re.I)
_GENERIC_APP_TOKENS = {
    "browser", "web", "internet", "online", "store", "files", "file",
    "music", "video", "mail", "email", "photos", "camera", "media",
    "player", "editor", "viewer", "game", "games", "help", "download",
    "downloads", "studio", "visual",
}
_SKIP_APP_PATHS = {
    "setup", "install", "uninstall", "idle", "pip", "pip3", "python",
    "pythonw", "wab", "wabmig", "store", "sdxhelper", "dfshim",
    "msoadfsb", "msoasb", "msohtmled", "msoxmled", "licensemanager",
    "fsquirt", "recoverydrive",
}
_SOURCE_PRIORITY = {"known": 0, "start_menu": 1, "start_apps": 2, "registry": 3}

_CATALOG: Optional[list["AppEntry"]] = None
_CATALOG_TS = 0.0
_START_APP_ENTRIES: Optional[list["AppEntry"]] = None
_LOCK = threading.Lock()
_LNK_CACHE: dict[str, Optional[str]] = {}


@dataclass(frozen=True)
class AppEntry:
    name: str
    target: str
    kind: str = "exe"  # exe | shortcut | appid | protocol
    exe_name: str = ""
    aliases: tuple[str, ...] = ()
    source: str = "known"


@dataclass(frozen=True)
class AppMatch:
    entry: AppEntry
    score: float
    alternatives: tuple[str, ...] = ()


def normalize(text: str) -> str:
    low = (text or "").lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", low).strip()


def clean_query(text: str) -> str:
    value = str(text or "").strip().strip(_OUTER_JUNK)
    value = _TRAILING_NOISE.sub("", value).strip(_OUTER_JUNK)
    value = _TRAILING_KIND.sub("", value).strip(_OUTER_JUNK)
    return value


def _tokens(text: str) -> list[str]:
    return [t for t in normalize(text).split() if t not in _NOISE_WORDS]


def _compact(text: str) -> str:
    return normalize(text).replace(" ", "")


def _acronym(tokens: Iterable[str]) -> str:
    return "".join(t[0] for t in tokens if t)


def _near_prefix(left: str, right: str, budget: int) -> bool:
    if left == right:
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    return (len(shorter) >= 3 and longer.startswith(shorter)
            and len(longer) - len(shorter) <= budget)


def _tokens_cover(left: list[str], right: list[str], budget: int = 0) -> bool:
    if not left or not right:
        return False
    used: set[int] = set()
    for token in left:
        hit = None
        for i, candidate in enumerate(right):
            if i in used:
                continue
            if _near_prefix(token, candidate, budget):
                hit = i
                break
        if hit is None:
            return False
        used.add(hit)
    return True


def _ratio(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if _rf_fuzz is not None:
        return max(
            float(_rf_fuzz.WRatio(left, right)),
            float(_rf_fuzz.token_sort_ratio(left, right)),
            float(_rf_fuzz.partial_ratio(left, right)) * 0.95,
        ) / 100.0
    return SequenceMatcher(None, left, right).ratio()


def _identity_signature(text: str) -> str:
    return "".join(dict.fromkeys(
        m.group(0).lower() for m in _IDENTITY_SYMBOLS.finditer(text or "")
    ))


def _alias_variants(name: str) -> tuple[str, ...]:
    out: list[str] = []
    base = _PARENS.sub(" ", name)
    base = _VERSION.sub("", base).strip()
    if base and base.lower() != name.lower():
        out.append(base)
    return tuple(dict.fromkeys(out))


def _score(query: str, entry: AppEntry) -> float:
    qn = normalize(query)
    if not qn:
        return 0.0
    qcompact = qn.replace(" ", "")
    qtokens = _tokens(query)
    generic_only = bool(qtokens) and all(
        t in _GENERIC_APP_TOKENS for t in qtokens)
    qsig = _identity_signature(query)
    best = 0.0
    for candidate in (entry.name, *entry.aliases):
        cn = normalize(candidate)
        if not cn:
            continue
        csig = _identity_signature(candidate)
        if qsig and csig != qsig:
            continue
        local = 0.0
        if qn == cn:
            local = 1.0
        else:
            ccompact = cn.replace(" ", "")
            if qcompact == ccompact:
                local = 0.99
            elif generic_only:
                local = 0.0
            else:
                ctokens = _tokens(candidate)
                if (qtokens and ctokens
                        and _tokens_cover(qtokens, ctokens, budget=0)):
                    local = max(local, 0.97)
                elif (qtokens and ctokens
                        and _tokens_cover(ctokens, qtokens, budget=0)):
                    local = max(local, 0.95)
                elif (qtokens and ctokens
                        and _tokens_cover(qtokens, ctokens, budget=2)):
                    local = max(local, 0.93)
                elif (qtokens and ctokens
                        and _tokens_cover(ctokens, qtokens, budget=2)):
                    local = max(local, 0.91)
                if len(qcompact) >= 2 and _acronym(ctokens) == qcompact:
                    local = max(local, 0.94)
                if (len(qcompact) >= 3
                        and abs(len(qcompact) - len(ccompact)) <= 2
                        and (qcompact in ccompact or ccompact in qcompact)):
                    local = max(local, 0.9)
                if abs(len(qcompact) - len(ccompact)) <= 2:
                    local = max(local, _ratio(qn, cn) * 0.95)
        if csig != qsig:
            local = min(local, 0.75)
        best = max(best, local)
    return best


def _best_match(query: str, entries: list[AppEntry]) -> Optional[AppMatch]:
    scored: list[tuple[float, AppEntry]] = []
    for entry in entries:
        score = _score(query, entry)
        if score >= SUGGEST_MIN:
            scored.append((score, entry))
    if not scored:
        return None
    scored.sort(key=lambda s: (
        -s[0],
        _SOURCE_PRIORITY.get(s[1].source, 9),
        len(s[1].name),
        s[1].name.lower(),
    ))
    top_score, top_entry = scored[0]
    alternatives = tuple(
        dict.fromkeys(
            e.name for _, e in scored[1:4]
            if e.name.lower() != top_entry.name.lower()
        )
    )
    if top_score < MATCH_MIN:
        return None
    return AppMatch(entry=top_entry, score=top_score, alternatives=alternatives)


def _first_file(paths: Iterable[str]) -> Optional[str]:
    for path in paths:
        if path and os.path.isfile(path):
            return os.path.expandvars(path)
    return None


def _known_entry(
    name: str,
    aliases: Iterable[str],
    paths: Iterable[str] = (),
    command: str = "",
    kind: str = "exe",
) -> Optional[AppEntry]:
    target = _first_file(paths)
    exe_name = ""
    if target:
        exe_name = os.path.basename(target)
    elif command:
        target = command
        if kind == "exe" and command.lower().endswith(".exe"):
            exe_name = command
    else:
        return None
    return AppEntry(
        name=name, target=target, kind=kind, exe_name=exe_name,
        aliases=tuple(dict.fromkeys(aliases)), source="known",
    )


def _known_entries() -> list[AppEntry]:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    lad = os.environ.get("LOCALAPPDATA", "")
    ad = os.environ.get("APPDATA", "")
    specs = [
        ("Google Chrome", ("chrome", "google chrome"), [
            os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(pf86, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(lad, "Google", "Chrome", "Application", "chrome.exe"),
        ], "", "exe"),
        ("Microsoft Edge", ("edge", "msedge", "microsoft edge"), [
            os.path.join(pf86, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
        ], "", "exe"),
        ("Mozilla Firefox", ("firefox", "mozilla firefox"), [
            os.path.join(pf, "Mozilla Firefox", "firefox.exe"),
            os.path.join(pf86, "Mozilla Firefox", "firefox.exe"),
        ], "", "exe"),
        ("Brave", ("brave", "brave browser"), [
            os.path.join(lad, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            os.path.join(pf, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
            os.path.join(pf86, "BraveSoftware", "Brave-Browser", "Application", "brave.exe"),
        ], "", "exe"),
        ("Visual Studio Code", ("code", "vscode", "vs code"), [
            os.path.join(lad, "Programs", "Microsoft VS Code", "Code.exe"),
            os.path.join(pf, "Microsoft VS Code", "Code.exe"),
        ], "", "exe"),
        ("Cursor", ("cursor",), [
            os.path.join(lad, "Programs", "cursor", "Cursor.exe"),
            os.path.join(lad, "Programs", "Cursor", "Cursor.exe"),
            os.path.join(pf, "Cursor", "Cursor.exe"),
        ], "", "exe"),
        ("OBS Studio", ("obs", "obs studio"), [
            os.path.join(pf, "obs-studio", "bin", "64bit", "obs64.exe"),
            os.path.join(pf86, "obs-studio", "bin", "64bit", "obs64.exe"),
            os.path.join(lad, "Programs", "obs-studio", "bin", "64bit", "obs64.exe"),
        ], "", "exe"),
        ("Notepad", ("notepad",), (), "notepad.exe", "exe"),
        ("Calculator", ("calc", "calculator"), (), "calc.exe", "exe"),
        ("Paint", ("paint", "mspaint"), (), "mspaint.exe", "exe"),
        ("File Explorer", ("explorer", "file explorer", "files"),
         (), "explorer.exe", "exe"),
        ("Microsoft Store", ("store", "microsoft store"),
         (), "ms-windows-store:", "protocol"),
        ("Photos", ("photos", "microsoft photos"), (), "ms-photos:", "protocol"),
        ("Camera", ("camera",), (), "microsoft.windows.camera:", "protocol"),
        ("Command Prompt", ("cmd", "command prompt"), (), "cmd.exe", "exe"),
        ("Windows PowerShell", ("powershell", "ps"), (), "powershell.exe", "exe"),
        ("Windows Terminal", ("terminal", "wt"), (), "wt.exe", "exe"),
        ("WordPad", ("wordpad",), (), "write.exe", "exe"),
        ("Control Panel", ("control", "control panel"), (), "control.exe", "exe"),
        ("Task Manager", ("taskmgr", "task manager"), (), "taskmgr.exe", "exe"),
        ("Registry Editor", ("regedit", "registry editor"), (), "regedit.exe", "exe"),
        ("System Configuration", ("msconfig", "system configuration"), (), "msconfig.exe", "exe"),
        ("Character Map", ("charmap", "character map"), (), "charmap.exe", "exe"),
        ("Magnifier", ("magnify", "magnifier"), (), "magnify.exe", "exe"),
        ("Settings", ("settings",), (), "ms-settings:", "protocol"),
        ("Spotify", ("spotify",), [
            os.path.join(ad, "Spotify", "Spotify.exe"),
            os.path.join(lad, "Microsoft", "WindowsApps", "Spotify.exe"),
        ], "spotify:", "protocol"),
        ("Telegram Desktop", ("telegram", "telegram desktop"), [
            os.path.join(ad, "Telegram Desktop", "Telegram.exe"),
            os.path.join(lad, "Programs", "Telegram Desktop", "Telegram.exe"),
        ], "tg://", "protocol"),
        ("Discord", ("discord",), [
            os.path.join(lad, "Discord", "Update.exe"),
        ], "discord:", "protocol"),
        ("WhatsApp", ("whatsapp",), (), "whatsapp:", "protocol"),
        ("Slack", ("slack",), (), "slack:", "protocol"),
    ]
    entries: list[AppEntry] = []
    for name, aliases, paths, command, kind in specs:
        entry = _known_entry(name, aliases, paths, command, kind)
        if entry is not None:
            entries.append(entry)
    return entries


def _start_menu_entries() -> list[AppEntry]:
    roots = [
        os.path.join(os.environ.get("APPDATA", ""),
                     r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("PROGRAMDATA", ""),
                     r"Microsoft\Windows\Start Menu\Programs"),
    ]
    entries: list[AppEntry] = []
    seen: set[str] = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for filename in files:
                if not filename.lower().endswith(".lnk"):
                    continue
                name = os.path.splitext(filename)[0].strip()
                if not name or _SKIP_SHORTCUT.search(name):
                    continue
                key = normalize(name)
                if not key or key in seen:
                    continue
                seen.add(key)
                entries.append(AppEntry(
                    name=name,
                    target=os.path.join(dirpath, filename),
                    kind="shortcut",
                    aliases=_alias_variants(name),
                    source="start_menu",
                ))
    return entries


def _registry_entries() -> list[AppEntry]:
    try:
        import winreg
    except ImportError:
        return []
    entries: list[AppEntry] = []
    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
    )
    for hive, key in roots:
        try:
            with winreg.OpenKey(hive, key) as base:
                count = winreg.QueryInfoKey(base)[0]
                for i in range(count):
                    try:
                        subname = winreg.EnumKey(base, i)
                        with winreg.OpenKey(base, subname) as sub:
                            target, _ = winreg.QueryValueEx(sub, "")
                    except Exception:
                        continue
                    if not target or not str(target).lower().endswith(".exe"):
                        continue
                    target = os.path.expandvars(str(target).strip('"'))
                    stem = os.path.splitext(os.path.basename(target))[0]
                    name = re.sub(r"[_-]+", " ", stem).strip()
                    if not name or name.lower() in _SKIP_APP_PATHS:
                        continue
                    if len(name) < 3 and not os.path.isfile(target):
                        continue
                    entries.append(AppEntry(
                        name=name,
                        target=target,
                        kind="exe",
                        exe_name=os.path.basename(target),
                        aliases=_alias_variants(name),
                        source="registry",
                    ))
        except Exception:
            continue
    return entries


def _start_app_entries() -> list[AppEntry]:
    global _START_APP_ENTRIES
    if _START_APP_ENTRIES is not None:
        return _START_APP_ENTRIES
    entries: list[AppEntry] = []
    try:
        from .windows import get_start_apps_cached
        for app in get_start_apps_cached():
            name = str(app.get("Name") or "").strip()
            app_id = str(app.get("AppID") or "").strip()
            if not name or not app_id or _SKIP_SHORTCUT.search(name):
                continue
            if app_id.lower().endswith(".exe"):
                if not os.path.isfile(app_id):
                    continue
                entries.append(AppEntry(
                    name=name,
                    target=app_id,
                    kind="exe",
                    exe_name=os.path.basename(app_id),
                    aliases=_alias_variants(name),
                    source="start_apps",
                ))
            else:
                entries.append(AppEntry(
                    name=name,
                    target=f"shell:AppsFolder\\{app_id}",
                    kind="appid",
                    aliases=_alias_variants(name),
                    source="start_apps",
                ))
    except Exception as exc:
        log.debug(f"Get-StartApps catalog load failed: {exc}")
    _START_APP_ENTRIES = entries
    return entries


def _merge(entries: Iterable[AppEntry]) -> list[AppEntry]:
    merged: dict[str, AppEntry] = {}
    order: list[str] = []
    for entry in entries:
        key = normalize(entry.name)
        if not key:
            continue
        if key in merged:
            current = merged[key]
            aliases = tuple(dict.fromkeys(
                current.aliases + entry.aliases + _alias_variants(entry.name)))
            merged[key] = replace(current, aliases=aliases)
        else:
            merged[key] = entry
            order.append(key)
    return [merged[key] for key in order]


def catalog(refresh: bool = False) -> list[AppEntry]:
    global _CATALOG, _CATALOG_TS, _START_APP_ENTRIES
    now = time.time()
    if (not refresh and _CATALOG is not None
            and now - _CATALOG_TS < _CATALOG_TTL_S):
        return _CATALOG
    with _LOCK:
        if (not refresh and _CATALOG is not None
                and now - _CATALOG_TS < _CATALOG_TTL_S):
            return _CATALOG
        if refresh:
            _START_APP_ENTRIES = None
        entries = _merge(
            _known_entries() + _start_menu_entries() + _registry_entries())
        _CATALOG = entries
        _CATALOG_TS = now
        return entries


def resolve_app(name: str, include_store: bool = True) -> Optional[AppMatch]:
    query = clean_query(name)
    if not query:
        return None
    match = _best_match(query, catalog())
    if match is not None and match.score >= MATCH_MIN:
        return match
    if not include_store:
        return match
    store_match = _best_match(query, _start_app_entries())
    if store_match is not None and (
            match is None or store_match.score > match.score):
        return store_match
    return match


def suggestions(name: str, limit: int = 3) -> list[str]:
    query = clean_query(name)
    if not query:
        return []
    entries = catalog() + _start_app_entries()
    scored = sorted(
        ((_score(query, e), e) for e in entries),
        key=lambda s: (
            -s[0],
            _SOURCE_PRIORITY.get(s[1].source, 9),
            len(s[1].name),
            s[1].name.lower(),
        ),
    )
    out: list[str] = []
    for score, entry in scored:
        if score < SUGGEST_MIN:
            break
        if entry.name not in out:
            out.append(entry.name)
        if len(out) >= limit:
            break
    return out


def resolve_shortcut_target(path: str) -> Optional[str]:
    if not path or not path.lower().endswith(".lnk"):
        return None
    if path in _LNK_CACHE:
        return _LNK_CACHE[path]
    target: Optional[str] = None
    try:
        import win32com.client  # type: ignore
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(path)
        raw = str(shortcut.TargetPath or "").strip()
        if raw:
            raw = os.path.expandvars(raw)
            target = raw if os.path.isfile(raw) else None
    except Exception:
        target = None
    if target is None:
        try:
            script = (
                "(New-Object -ComObject WScript.Shell).CreateShortcut("
                + _ps_single_quote(path)
                + ").TargetPath"
            )
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True, text=True, timeout=6,
            )
            raw = (proc.stdout or "").strip()
            if raw and os.path.isfile(raw):
                target = os.path.expandvars(raw)
        except Exception as exc:
            log.debug(f"Shortcut target resolve failed for {path}: {exc}")
    _LNK_CACHE[path] = target
    return target


def _ps_single_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"
