"""History guards for repeated planner failures."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.utils.history import detect_repeated_failure


def _entry(skill: str, args: dict, success: bool, output: str) -> dict:
    return {
        "step": {"skill": skill, "args": args},
        "result": {"success": success, "output": output},
    }


def test_repeated_failure_is_detected_across_a_successful_install():
    failure = "Missing Python package 'PyMuPDF'."
    history = [
        _entry("run_skill_script", {"skill": "pdf"}, False, failure),
        _entry("run_command", {"command": "pip install PyMuPDF"}, True, "ok"),
        _entry("run_skill_script", {"skill": "pdf"}, False, failure),
    ]
    assert detect_repeated_failure(history) == failure


def test_repeated_failure_requires_the_same_args_and_error():
    history = [
        _entry("run_skill_script", {"skill": "pdf"}, False, "first error"),
        _entry("run_skill_script", {"skill": "docx"}, False, "first error"),
        _entry("run_skill_script", {"skill": "pdf"}, False, "second error"),
    ]
    assert detect_repeated_failure(history) == ""


def test_single_failure_is_not_a_repeat():
    history = [
        _entry("run_skill_script", {"skill": "pdf"}, False, "only once"),
    ]
    assert detect_repeated_failure(history) == ""
