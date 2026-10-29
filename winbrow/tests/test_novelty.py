"""Advisory no-new-information detection for the brain loop."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.brain.agent_loop import BrainStepResult, no_new_information


def _step(output, args=None, success=True):
    return BrainStepResult(
        "browser_wait", args or {}, success, output)


def test_same_page_reads_have_no_new_information():
    history = [_step("same page text", {"url": "https://x.test/a"})
               for _ in range(5)]
    assert no_new_information(history, 5) is True


def test_new_urls_count_as_information():
    history = [
        _step(f"page {i}", {"url": f"https://x.test/{i}"})
        for i in range(5)
    ]
    assert no_new_information(history, 5) is False


def test_new_paths_count_as_information():
    history = [
        _step(f"read {i}", {"path": f"C:\\tmp\\file{i}.txt"})
        for i in range(5)
    ]
    assert no_new_information(history, 5) is False


def test_failures_and_short_windows_do_not_trigger():
    history = [_step("same", {"url": "https://x.test/a"})
               for _ in range(4)]
    assert no_new_information(history, 5) is False
    history = [_step("same", {"url": "https://x.test/a"}) for _ in range(5)]
    history[-1].success = False
    assert no_new_information(history, 5) is False
