"""
Safety Policy Engine for WinBrow
=================================
Port of macbrow's policy.py for Windows.
Prevents catastrophic accidents (e.g. wiping directories, killing system processes,
unsafe registry writes) and determines if an action requires explicit confirmation.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

log = logging.getLogger("winbrow.policy")

# Regex patterns that indicate destructive operations on Windows
BLOCKED_PATTERNS = [
    re.compile(r"Format-Volume|format\s+[c-z]:", re.I),
    re.compile(r"Remove-Item\s+.*[C-Z]:\\(Windows|System32|Boot)", re.I),
    re.compile(r"del\s+/[sfeq]*\s+[C-Z]:\\(Windows|System32)", re.I),
    re.compile(r"Stop-Computer|Restart-Computer\s+-Force", re.I),
    re.compile(r"Clear-Disk|Initialize-Disk|Set-Disk", re.I),
    re.compile(r"bcdedit|reg\s+delete\s+HKLM", re.I),
    re.compile(r"(rmdir|rd)\s+/\s*[sq|\s]*\s*.*[C-Z]:\\(Windows|System32|Boot)", re.I),
    # Mass deletes of the user profile / whole drive roots. Scoped per-item
    # deletes (temp files, single paths) remain permitted — the permission
    # tiers (T2/T3) handle those with confirmation instead of hard denial.
    re.compile(
        r"(?:Remove-Item|Remove-ItemProperty|del\b|rmdir|rd)\b"
        r"[^\n]{0,240}(?:\$env:USERPROFILE|\$env:HOME|\$HOME(?![\w.]))", re.I),
    re.compile(
        r"(?:Remove-Item|Remove-ItemProperty|del\b|rmdir|rd)\b"
        r"[^\n]{0,240}[C-Z]:\\(?:\s|['\"]|$)", re.I),
]

# Patterns that modify user data and should be confirmed if confidence isn't high
CONFIRMATION_PATTERNS = [
    re.compile(r"Remove-Item|del\b|rmdir\b", re.I),
    re.compile(r"Stop-Process\s+-Name\s+(explorer|chrome|code)", re.I),
    re.compile(r"Empty-RecycleBin", re.I),
    re.compile(r"Move-Item\s+.*\*.*", re.I),
    re.compile(r"Set-Service|Stop-Service", re.I),
]


class PolicyError(Exception):
    """Raised when a script or action violates safety rules."""
    pass


def validate_script_safety(script: str) -> None:
    """
    Check if a generated or requested PowerShell/shell script violates strict system boundaries.
    Throws PolicyError if dangerous.
    """
    for pat in BLOCKED_PATTERNS:
        if pat.search(script):
            raise PolicyError(f"Script contains forbidden destructive command: {pat.pattern}")


def is_destructive_action(utterance: str, script: Optional[str] = None) -> bool:
    """Determine whether the requested action modifies or deletes files/processes."""
    text = utterance.lower()
    destructive_keywords = [
        "delete", "remove", "wipe", "clean", "kill all", "terminate",
        "format", "uninstall", "empty trash", "empty recycle bin", "reset"
    ]
    if any(k in text for k in destructive_keywords):
        return True

    if script:
        for pat in CONFIRMATION_PATTERNS:
            if pat.search(script):
                return True

    return False


# ---------------------------------------------------------------------------
# v2 script-tier classification (permissions funnel, M3)
# ---------------------------------------------------------------------------

# Windows path spans for destructive-side rules
_USER_PROFILES = r"[C-Z]:\\Users"
_USER_DATA_PATTERNS = [
    re.compile(r"Remove-Item|Remove-ItemProperty|del\b|erase\b|rmdir|rd\s+/", re.I),
    re.compile(r"Move-Item|Rename-Item|Copy-Item\s+-Force", re.I),
    re.compile(r"Set-Content|Add-Content|New-Item\s+-ItemType\s+(File|Directory)", re.I),
    re.compile(r"Out-File|Export-\w+", re.I),
    re.compile(r"Empty-RecycleBin|Clear-RecycleBin", re.I),
]

def _means_dangerous_system(script: str) -> bool:
    """True if the script would not have passed validate_script_safety, or
    touches system-class state (system service, HKLM, scheduled shutdown)."""
    try:
        validate_script_safety(script)
    except PolicyError:
        return True
    if re.search(r"Set-Service|Stop-Service|sc\.exe\s+(config|start|stop)", script, re.I):
        return True
    if re.search(r"HKLM|regedit|reg\s+(import|add)", script, re.I):
        return True
    if re.search(r"Stop-Process\s+-Name\s+(explorer|csrss|svchost|dwm)", script, re.I):
        return True
    return False


def _means_user_data_write(script: str) -> bool:
    for pat in _USER_DATA_PATTERNS:
        if pat.search(script):
            return True
    return False


def script_tier(script: str) -> str:
    """Tier a rendered script would require (T3 system, T2 user-data, else T1)."""
    s = script or ""
    if _means_dangerous_system(s):
        return "t3"
    if _means_user_data_write(s):
        return "t2"
    return "t1"
