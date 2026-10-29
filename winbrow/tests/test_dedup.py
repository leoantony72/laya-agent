"""Completed-step signatures: whitespace/enum normalisation, URL identity."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.brain.agent_loop import (
    REPEATABLE_SKILLS,
    _is_repeatable_skill,
    _step_signature,
)


def test_whitespace_is_ignored():
    a = _step_signature("run_command", {"command": "  dir  ", "cwd": " C:\\x "})
    b = _step_signature("run_command", {"command": "dir", "cwd": "C:\\x"})
    assert a == b


def test_enum_values_are_case_folded():
    a = _step_signature("window_action", {"action": " MINIMIZE "})
    b = _step_signature("window_action", {"action": "minimize"})
    assert a == b


def test_paths_and_urls_keep_case():
    a = _step_signature("open_file", {"target": "C:\\Users\\X\\Report.PDF"})
    b = _step_signature("open_file", {"target": "C:\\Users\\X\\report.pdf"})
    assert a != b
    a = _step_signature("web_fetch", {"url": "https://x.test/A"})
    b = _step_signature("web_fetch", {"url": "https://x.test/a"})
    assert a != b


def test_web_fetch_ignores_max_chars_but_keeps_url():
    a = _step_signature("web_fetch", {"url": "https://x.test/a", "max_chars": 12000})
    b = _step_signature("web_fetch", {"url": "https://x.test/a", "max_chars": 40000})
    c = _step_signature("web_fetch", {"url": "https://x.test/b"})
    assert a == b
    assert a != c


def test_repeatable_reads_stay_exempt():
    for name in ("browser_read_page", "browser_read_full", "browser_wait"):
        assert name in REPEATABLE_SKILLS


def test_filesystem_reads_can_be_repeated_for_verification():
    # After a save/edit/move the only way to verify is to read again.
    for name in (
        "mcp_filesystem_read_file",
        "mcp_filesystem_read_text_file",
        "mcp_filesystem_list_directory",
        "mcp_filesystem_get_file_info",
        "read_document",
        "find_files",
    ):
        assert _is_repeatable_skill(name) is True


def test_mutating_steps_are_not_repeatable():
    for name in (
        "mcp_filesystem_write_file",
        "mcp_filesystem_edit_file",
        "mcp_filesystem_move_file",
        "mcp_filesystem_create_directory",
        "type_text",
        "create_text_file",
        "run_command",
    ):
        assert _is_repeatable_skill(name) is False


def test_unknown_read_only_mcp_tool_is_classified_by_name():
    assert _is_repeatable_skill("mcp_filesystem_list_allowed_directories") is True
    assert _is_repeatable_skill("mcp_time_get_current_time") is True
    assert _is_repeatable_skill("mcp_filesystem_read_multiple_files") is True
    # A name mixing both signals stays protected.
    assert _is_repeatable_skill("mcp_filesystem_read_and_write_file") is False
