from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class Arg:
    name: str
    type: str  # "text" or "enum"
    choices: Tuple[str, ...] = ()


RISKY_SKILLS = {
    "get_wifi_password",
    "kill_unresponsive_process",
    "clean_temp_files",
    "clean_downloads",
    "app_install",
    "app_uninstall",
    "app_upgrade_all",
    "shutdown_timer",
    "lock_screen",
    "organize_desktop",
    "set_wallpaper",
}


@dataclass(frozen=True)
class Skill:
    name: str
    args: Tuple[Arg, ...]
    description: str
    group: str
    risky: bool
