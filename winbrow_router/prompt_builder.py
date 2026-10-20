from __future__ import annotations

import re
from typing import Any

from .normalize import render_skill_line
from .models import Skill


CORE_PROMPT = """You are WinBrow's planner, the decision layer of a Windows desktop agent. Turn the user's request into a short plan using ONLY the skills listed under SKILLS.

You receive: the request (possibly a follow-up like "make it louder"), the live Windows context, recent conversation, and the available skills.

Reply with one JSON object and nothing else, in one of three shapes:
{"kind":"action","steps":[{"skill":"<name>","args":{...},"why":"<max 8 words>"}]}
{"kind":"chat","response":"<max 2 sentences>"}
{"kind":"stop"}

Choosing the kind:
- action: the request needs 1-3 skill steps. Use the fewest steps that finish the job.
- chat: a question or small talk that needs no skill, OR the request is ambiguous and the context cannot resolve it (ask ONE clarifying question).
- stop: the user wants you to stop or cancel.

General rules:
- Use only skills from SKILLS. Never invent skills or args. Fill every arg in the signature; for enum args pick the listed value that best fits.
- Resolve follow-ups ("it", "that", "louder", "close it") from CONVERSATION and Frontmost before asking anything.
- To pass a previous step's output into a text arg, use the exact string "$PREVIOUS_OUTPUT" as the whole value. Use it for nothing else.
- If a step is destructive or system-changing (marked [risky] in SKILLS), run it only when the user asked for that exact action, and add "confirm": true to that step."""


RULES: list[dict[str, Any]] = [
    {
        "text": "Typing into an app: first emit app_focus_or_launch with {\"app_name\": \"<app>\"}, then type_text. Skip the focus step only if that app is already Frontmost.",
        "requires": ["app_focus_or_launch", "type_text"],
        "requires_any": [],
    },
    {
        "text": "To create a note or save text as a file on the Desktop, use quick_note with {\"content\": \"...\"}.",
        "requires": ["quick_note"],
        "requires_any": [],
    },
    {
        "text": "Volume: use volume_set_level with a number 0-100 as a string. \"Louder\" or \"quieter\" with no number means the current Volume +15 or -15, clamped to 0-100.",
        "requires": ["volume_set_level"],
        "requires_any": [],
    },
    {
        "text": "Opening things: open_file for files, open_folder for folders, app_focus_or_launch for apps.",
        "requires": ["open_file", "open_folder", "app_focus_or_launch"],
        "requires_any": [],
    },
    {
        "text": "For facts, definitions and reading URLs, prefer the direct lookup skills (Wikipedia, dictionary, fetch) over browsing.",
        "requires": [],
        "requires_any": ["mcp_wikipedia_get_summary", "mcp_dictionary_define", "mcp_fetch_fetch"],
    },
    {
        "text": "For interactive web tasks (playing a specific YouTube video, booking, shopping, filling forms), use laya_ultrafast_browser with the full goal as one sentence.",
        "requires": ["laya_ultrafast_browser"],
        "requires_any": [],
    },
    {
        "text": "Weather: call the Open-Meteo search_locations skill first to get coordinates, then get_forecast.",
        "requires": [],
        "requires_any": ["mcp_open_meteo_openmeteo_get_forecast"],
    },
    {
        "text": "When the user states a lasting preference (\"my exports folder is X\"), call remember_fact.",
        "requires": ["remember_fact"],
        "requires_any": [],
    },
    {
        "text": "When the user asks about something they did before, call recall_memory.",
        "requires": ["recall_memory"],
        "requires_any": [],
    },
]


EXAMPLES: list[dict[str, Any]] = [
    {
        "text": 'Request: open notepad and type hello\n{"kind":"action","steps":[{"skill":"app_focus_or_launch","args":{"app_name":"notepad"},"why":"open Notepad"},{"skill":"type_text","args":{"text":"hello"},"why":"type the text"}]}',
        "requires": ["app_focus_or_launch", "type_text"],
    },
    {
        "text": 'Volume: 22, Request: make it louder\n{"kind":"action","steps":[{"skill":"volume_set_level","args":{"level":"37"},"why":"22 + 15"}]}',
        "requires": ["volume_set_level"],
    },
    {
        "text": 'Request: look up Ada Lovelace and write it in notepad\n{"kind":"action","steps":[{"skill":"mcp_wikipedia_get_summary","args":{"title":"Ada Lovelace"},"why":"fetch summary"},{"skill":"app_focus_or_launch","args":{"app_name":"notepad"},"why":"open Notepad"},{"skill":"type_text","args":{"text":"$PREVIOUS_OUTPUT"},"why":"type the summary"}]}',
        "requires": ["mcp_wikipedia_get_summary", "app_focus_or_launch", "type_text"],
    },
]


USER_TEMPLATE = """WINDOWS CONTEXT:
Frontmost: {frontmost_truncated_to_60_chars}
Open apps: {apps_with_visible_windows}
Volume: {volume}

CONVERSATION:
{recent_turns_or_"none"}

SKILLS:
{skills_list_rendered_one_per_line}

REQUEST: {utterance}"""


RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "winbrow_plan",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {"type": "string", "enum": ["action", "chat", "stop"]},
                "steps": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "skill": {"type": "string"},
                            "args": {"type": "object"},
                            "why": {"type": "string"},
                            "confirm": {"type": "boolean"},
                        },
                        "required": ["skill", "args"],
                    },
                },
                "response": {"type": "string"},
            },
            "required": ["kind"],
        },
    },
}


_BG_PATTERNS = [
    "Host$",
    "Service$",
    "Helper$",
    "Updater?",
    "Redist",
    "CompPkgSrv",
    "DSA",
    "Defender",
    "GameInput",
    "Aggregator",
    "Broker",
]


def _filter_apps(apps: list[str]) -> list[str]:
    out = []
    for app in apps:
        skip = False
        for pat in _BG_PATTERNS:
            if re.search(pat, app, re.IGNORECASE):
                skip = True
                break
        if not skip:
            out.append(app)
    return out[:8]


def build_messages(
    request: str,
    frontmost: str,
    running_apps: list[str],
    volume: Any,
    history: list[tuple[str, str]],
    routed: list[Skill],
) -> dict:
    routed_names = {s.name for s in routed}

    # frontmost truncate
    if len(frontmost) > 60:
        frontmost_trunc = frontmost[:60] + "..."
    else:
        frontmost_trunc = frontmost

    # apps
    filtered_apps = _filter_apps(list(running_apps))
    apps_str = ", ".join(filtered_apps)

    # history
    hist = list(history or [])[-4:]
    if not hist:
        history_str = "none"
    else:
        lines = []
        for role, text in hist:
            t = (text or "")[:200]
            lines.append(f"{role}: {t}")
        history_str = "\n".join(lines)

    # skills
    skills_str = "\n".join(render_skill_line(s) for s in routed)

    # user content from template
    user_content = USER_TEMPLATE
    user_content = user_content.replace("{frontmost_truncated_to_60_chars}", frontmost_trunc)
    user_content = user_content.replace("{apps_with_visible_windows}", apps_str)
    user_content = user_content.replace("{volume}", str(volume))
    user_content = user_content.replace('{recent_turns_or_"none"}', history_str)
    user_content = user_content.replace("{skills_list_rendered_one_per_line}", skills_str)
    user_content = user_content.replace("{utterance}", request)

    # system
    system = CORE_PROMPT

    # rules
    selected_rules = []
    for rule in RULES:
        reqs = rule.get("requires", []) or []
        req_any = rule.get("requires_any", []) or []
        if all(r in routed_names for r in reqs) and (not req_any or any(r in routed_names for r in req_any)):
            selected_rules.append(rule["text"])
    if selected_rules:
        system = system + "\n\n" + "\n".join(selected_rules)

    # examples
    selected_examples = []
    for ex in EXAMPLES:
        reqs = ex.get("requires", []) or []
        if all(r in routed_names for r in reqs):
            selected_examples.append(ex["text"])
    if selected_examples:
        system = system + "\n\n" + "\n\n".join(selected_examples)

    return {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
        "response_format": RESPONSE_FORMAT,
    }
