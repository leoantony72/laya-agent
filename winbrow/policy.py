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
    re.compile(r"rmdir\s+/[sq]\s+[C-Z]:\\$", re.I),
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
