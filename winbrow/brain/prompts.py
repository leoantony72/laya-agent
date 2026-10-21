"""
Compatibility shim for the legacy prompt module name.

The canonical planner prompt now lives in ``winbrow.brain.prompt`` and is split
into a cached static block plus dynamically injected skill metadata.
"""

from .prompt import (  # noqa: F401
    CHAT_SYSTEM,
    NORMALIZER_SYSTEM,
    PLANNER_JSON_SCHEMA,
    PLANNER_SYSTEM,
    PLANNER_SYSTEM_PROMPT,
    SYSTEM_PROMPT_STATIC,
    TOOL_FIXER_JSON_SCHEMA,
    TOOL_FIXER_SYSTEM,
    build_chat_messages,
    build_loop_messages,
    build_normalizer_messages,
    build_planner_message,
    build_system_blocks,
    build_tool_repair_messages,
)

__all__ = [
    "CHAT_SYSTEM",
    "NORMALIZER_SYSTEM",
    "PLANNER_JSON_SCHEMA",
    "PLANNER_SYSTEM",
    "PLANNER_SYSTEM_PROMPT",
    "SYSTEM_PROMPT_STATIC",
    "TOOL_FIXER_JSON_SCHEMA",
    "TOOL_FIXER_SYSTEM",
    "build_chat_messages",
    "build_loop_messages",
    "build_normalizer_messages",
    "build_planner_message",
    "build_system_blocks",
    "build_tool_repair_messages",
]
