"""Shared utility helpers for the WinBrow agent."""

from .history import (
    count_recent_stuck,
    detect_stuck,
    extract_tried_services,
    format_history,
    last_url,
    tried_domains,
)

__all__ = [
    "count_recent_stuck",
    "detect_stuck",
    "extract_tried_services",
    "format_history",
    "last_url",
    "tried_domains",
]
