"""Prompt layout: HISTORY early, volatile text after the stable prefix."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.brain.prompt import build_loop_messages


def _content(**overrides):
    args = {
        "utterance": "do the thing",
        "history_block": "Step 1: web_fetch",
        "context_block": "ctx",
        "memory_block": "memory",
        "skills_block": "",
        "tried_block": "ALREADY TRIED:",
        "steps_remaining": 7,
        "skill_detail": "detail",
        "volatile_block": "PLANNER FEEDBACK: look again",
    }
    args.update(overrides)
    return build_loop_messages(**args)[1]["content"]


def test_history_before_context_and_volatile_tail():
    content = _content()
    positions = [
        content.index("GOAL: do the thing"),
        content.index("HISTORY:"),
        content.index("ALREADY TRIED:"),
        content.index("WINDOWS CONTEXT:"),
        content.index("CONVERSATION:"),
        content.index("PLANNER FEEDBACK:"),
        content.index("STEPS REMAINING: 7"),
        content.index("GOAL (restated):"),
        content.index("SKILL DETAIL"),
        content.index("What is the single best next action?"),
    ]
    assert positions == sorted(positions)


def test_tried_block_is_after_history():
    content = _content()
    assert content.index("HISTORY:") < content.index("ALREADY TRIED:")


def test_legacy_planner_message_also_puts_history_before_steps():
    from winbrow.brain.prompt import build_planner_message
    message = build_planner_message("goal", [], 7)
    assert message.index("HISTORY:") < message.index("STEPS REMAINING: 7")
