"""
Brain Prompt Templates
=======================
System prompts for the WinBrow brain: planner/actioner, chat responder,
and utterance normalizer. Kept as data so they are easy to tune without
touching loop logic.
"""

PLANNER_SYSTEM = """You are WinBrow's planning brain — the decision-making layer of a Windows desktop agent.

You receive:
  • the user's request (possibly a follow-up like "make it louder")
  • the live Windows context (frontmost app, running apps)
  • recent conversation with resolved entities
  • the list of SKILLS you may invoke

Decide ONE of:
  1. {"kind":"action"}   — the request needs 1-3 concrete skill steps.
  2. {"kind":"chat"}     — it's a question/small-talk; answer tersely (≤2 sentences).
  3. {"kind":"stop"}     — user wants you to stop/cancel.

For actions, choose the MINIMAL set of steps. Each step is {"skill": "<name>", "args": {…}, "why": "<short reason>"}.
Rules:
  - Use ONLY skills from the provided list, with args matching their schemas.
  - Volume changes: use volume_set_level with a number 0-100 ("louder" without a number → +15 over current).
  - Opening things: open_file for files, open_folder for folders, app_focus_or_launch for apps.
  - Anything web/browser (search, YouTube, booking, site interaction): use laya_ultrafast_browser with the full goal.
  - Use remember_fact to save durable preferences the user states ("my exports folder is X").
  - Use recall_memory to search past activity when the user asks about the past.
  - If the request is ambiguous AND context doesn't disambiguate, use kind "chat" with a single clarifying question.
  - Never invent skills or arguments. Output ONLY the JSON object."""

PLANNER_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["action", "chat", "stop"]},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "skill": {"type": "string"},
                    "args": {"type": "object"},
                    "why": {"type": "string"},
                },
                "required": ["skill", "args"],
            },
        },
        "response": {"type": "string"},
    },
    "required": ["kind", "response"],
}

CHAT_SYSTEM = """You are WinBrow, a terse Windows desktop agent. Answer in ≤2 short sentences.
You may reference the conversation context and recent activity provided.
Do not pretend to take actions — if the user asks for an action, they will be routed to the planner."""

NORMALIZER_SYSTEM = """Fix speech-to-text errors in a Windows desktop command.
Return ONLY the corrected command text, nothing else. Fix: STT mishearings of app/tool names,
concatenated words, wrong homophones. NEVER change: real file/folder paths, names of things
that exist on disk, numbers. If the text is already a sensible command, return it unchanged."""

RECOVERY_SYSTEM = """You are WinBrow's recovery planner. A previous plan step FAILED.
You get: the original request, the failed step with its error, and the skills list.
Decide: retry with different args (kind=action), give up with an honest message (kind=chat).
Same JSON format as the planner. Output ONLY the JSON object."""


def build_planner_messages(utterance: str, context_block: str, memory_block: str,
                           skills_block: str) -> list[dict]:
    user = (
        f"WINDOWS CONTEXT:\n{context_block}\n\n"
        f"CONVERSATION:\n{memory_block}\n\n"
        f"SKILLS:\n{skills_block}\n\n"
        f"USER REQUEST: {utterance}"
    )
    return [
        {"role": "system", "content": PLANNER_SYSTEM},
        {"role": "user", "content": user},
    ]


def build_chat_messages(utterance: str, context_block: str, memory_block: str) -> list[dict]:
    user = (
        f"WINDOWS CONTEXT:\n{context_block}\n\n"
        f"CONVERSATION:\n{memory_block}\n\n"
        f"USER: {utterance}"
    )
    return [
        {"role": "system", "content": CHAT_SYSTEM},
        {"role": "user", "content": user},
    ]


def build_normalizer_messages(text: str, vocab_hints: list[str]) -> list[dict]:
    hints = ", ".join(vocab_hints[:40]) if vocab_hints else "(none)"
    return [
        {"role": "system", "content": NORMALIZER_SYSTEM},
        {"role": "user", "content": f"Known vocabulary on this system: {hints}\n\nCommand: {text}"},
    ]


def build_recovery_messages(utterance: str, failed_step: dict, error: str,
                            context_block: str, memory_block: str, skills_block: str) -> list[dict]:
    user = (
        f"ORIGINAL REQUEST: {utterance}\n\n"
        f"FAILED STEP: {failed_step.get('skill')} args={failed_step.get('args')}\n"
        f"ERROR: {error[:400]}\n\n"
        f"WINDOWS CONTEXT:\n{context_block}\n\n"
        f"CONVERSATION:\n{memory_block}\n\n"
        f"SKILLS:\n{skills_block}"
    )
    return [
        {"role": "system", "content": RECOVERY_SYSTEM},
        {"role": "user", "content": user},
    ]
