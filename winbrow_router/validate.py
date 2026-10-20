from __future__ import annotations

import json
import re
from typing import Callable

from .models import Skill
from .router import SkillRouter
from .prompt_builder import build_messages


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return text


def validate_plan(raw_text: str, routed: list[Skill], all_skills: list[Skill]) -> tuple[dict | None, list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    text = _strip_fences(raw_text)
    try:
        plan = json.loads(text)
    except json.JSONDecodeError as e:
        return None, [f"invalid JSON: {e}"]
    if not isinstance(plan, dict):
        return None, ["invalid JSON: plan must be an object"]

    kind = plan.get("kind")
    if kind not in ("action", "chat", "stop"):
        errors.append(f"invalid kind: {kind!r}")

    routed_names = {s.name for s in routed}
    all_map = {s.name: s for s in all_skills}

    if kind == "action":
        steps = plan.get("steps")
        if not isinstance(steps, list) or len(steps) == 0:
            errors.append("action requires at least 1 step")
        if isinstance(steps, list) and len(steps) > 3:
            errors.append("action allows at most 3 steps")
        if isinstance(steps, list):
            for idx, step in enumerate(steps):
                if not isinstance(step, dict):
                    errors.append(f"step {idx} is not an object")
                    continue
                skill = step.get("skill")
                if skill not in all_map:
                    errors.append(f"hallucinated skill: {skill}")
                    continue
                # router miss
                if skill not in routed_names:
                    errors.append(f"router_miss:{skill}")
                sk = all_map[skill]
                args = step.get("args", {})
                if not isinstance(args, dict):
                    errors.append(f"step {idx} args must be an object")
                    args = {}
                sig_names = [a.name for a in sk.args]
                # unknown args
                for key in args.keys():
                    if key not in sig_names:
                        errors.append(f"unknown arg '{key}' for skill {skill}")
                # missing required args
                for a in sk.args:
                    if a.name not in args:
                        errors.append(f"missing required arg '{a.name}' for skill {skill}")
                # risky auto-fix
                if sk.risky and step.get("confirm") is not True:
                    step["confirm"] = True
                    warnings.append(f"warning: auto-fixed confirm for risky skill {skill}")
    elif kind == "chat":
        resp = plan.get("response")
        if not (isinstance(resp, str) and resp.strip()):
            errors.append("chat requires non-empty response")
    # stop: no further checks

    final_errors = errors + warnings
    if errors:
        return None, final_errors
    return plan, final_errors


def plan_with_retry(
    call_llm: Callable[[list[dict], float], str],
    build_args: dict,
    router: SkillRouter,
    all_skills: list[Skill],
    max_retries: int = 1,
) -> dict:
    request = build_args.get("request", build_args.get("utterance", ""))
    history = build_args.get("history", [])
    frontmost = build_args.get("frontmost", "")
    running_apps = build_args.get("running_apps", [])
    volume = build_args.get("volume", "")
    history_skills = build_args.get("history_skills")

    use_full = False
    last_errors: list[str] = []
    for attempt in range(max_retries + 1):
        temperature = 0.1 if attempt == 0 else 0.3
        if use_full:
            routed = list(all_skills)
        else:
            routed = router.route(request, history_skills)
        msgs = build_messages(request, frontmost, running_apps, volume, history, routed)
        # append errors to user message on retry
        if last_errors and attempt > 0:
            user_msg = msgs["messages"][1]["content"]
            user_msg = user_msg + "\n\nYour last reply was invalid: " + "; ".join(last_errors)
            msgs["messages"][1]["content"] = user_msg
        raw = call_llm(msgs["messages"], temperature)
        plan, errors = validate_plan(raw, routed, all_skills)
        hard_errors = [e for e in errors if not e.startswith("warning:")]
        if plan is not None and not hard_errors:
            return plan
        last_errors = errors if errors else ["invalid plan"]
        if any("router_miss:" in e for e in errors):
            use_full = True
    return {"kind": "chat", "response": "I couldn't build a reliable plan for that. Could you rephrase?"}
