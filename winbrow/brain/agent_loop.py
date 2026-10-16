"""
Agent Brain — Plan → Execute → Verify → Recover Loop (System 2)
================================================================
Handles everything the fast-path router can't:
  • contextual follow-ups ("make it louder", "and close it after")
  • multi-step tasks ("open photoshop, desaturate the image, save a copy")
  • questions about past activity ("what did I open yesterday?")
  • fact capture ("remember my exports folder is D:\\Exports")

The loop asks the ModelGateway for a JSON plan (action/chat/stop), executes
steps through the shared skill executor, verifies each step, and can re-plan
once with the failure context before giving up honestly.

The gateway is optional: without any provider configured the brain raises
GatewayUnavailable and the orchestrator falls back to the generator tier.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .model_gateway import (
    GatewayError,
    GatewayResponse,
    GatewayUnavailable,
    ModelGateway,
    extract_json_object,
)
from . import prompts

log = logging.getLogger("winbrow.brain.loop")

MAX_STEPS_DEFAULT = int(6)
MAX_REPLANS = 1

# Meta-skills the brain can call that are not registry tools.
# Implemented in-process against the memory store.
META_SKILLS: dict[str, dict] = {
    "remember_fact": {
        "description": "Save a durable user preference or environment fact for future commands.",
        "args": {"fact": "The fact to remember, phrased as a complete sentence."},
    },
    "recall_memory": {
        "description": "Search past activity/history. Use when the user asks about something they did before.",
        "args": {"query": "What to search for in past activity."},
    },
}


@dataclass
class BrainStepResult:
    skill: str
    args: dict
    success: bool
    output: str
    why: str = ""
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "skill": self.skill, "args": self.args, "success": self.success,
            "output": self.output[:400], "why": self.why, "elapsed_ms": self.elapsed_ms,
        }


@dataclass
class BrainResult:
    kind: str                       # "action" | "chat" | "stop"
    success: bool = False
    response: str = ""
    steps: List[BrainStepResult] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    plan_elapsed_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "type": "brain",
            "kind": self.kind,
            "success": self.success,
            "output": self.response,
            "steps": [s.to_dict() for s in self.steps],
            "provider": self.provider,
            "model": self.model,
            "plan_elapsed_ms": self.plan_elapsed_ms,
        }


class AgentBrain:
    """The System 2 planner/executor loop."""

    def __init__(
        self,
        gateway: ModelGateway,
        registry: Any,                     # ToolRegistry
        store: Any,                        # MemoryStore
        executor: Callable[[str, dict], Awaitable[dict]],
        max_steps: int = MAX_STEPS_DEFAULT,
    ):
        self.gateway = gateway
        self.registry = registry
        self.store = store
        self.executor = executor           # async (tool_name, args) -> execution dict
        self.max_steps = max_steps

    # ------------------------------------------------------------------ skills block
    def _skills_block(self, ctx: Any) -> str:
        lines = []
        for t in self.registry.available(ctx)[:48]:
            args_desc = ", ".join(
                f"{a.name}({a.kind})" for a in t.args
            ) or "no args"
            lines.append(f"- {t.name}({args_desc}): {t.description}")
        for name, meta in META_SKILLS.items():
            args_desc = ", ".join(f"{k}(text)" for k in meta["args"])
            lines.append(f"- {name}({args_desc}): {meta['description']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ main entry
    async def handle(
        self,
        utterance: str,
        ctx: Any,
        session: Any,                       # SessionMemory
        emit: Optional[Callable[[dict], Awaitable[None]]] = None,
    ) -> BrainResult:
        """Plan and execute. Raises GatewayUnavailable when no provider exists."""
        memory_block = session.context_block()
        # enrich with episodic hits when the user asks about the past
        if any(w in utterance.lower() for w in ("yesterday", "earlier", "last time", "before", "history", "did i")):
            hits = self.store.search_episodic(utterance, limit=5)
            if hits:
                memory_block += "\nRelevant past activity:\n" + "\n".join(
                    f"  {h.label[:120]}" for h in hits
                )

        skills_block = self._skills_block(ctx)
        messages = prompts.build_planner_messages(
            utterance, _ctx_block(ctx), memory_block, skills_block,
        )

        plan = await self._ask_plan(messages, emit)
        result = BrainResult(
            kind=str(plan.get("kind", "chat")),
            response=str(plan.get("response", "")),
            provider=self.gateway.providers[0].name if self.gateway.providers else "",
            model=self.gateway.providers[0].model if self.gateway.providers else "",
        )

        if result.kind == "stop":
            result.success = True
            result.response = result.response or "Standing by."
            return result

        if result.kind == "chat":
            result.success = True
            return result

        # ---- action: execute steps
        steps = plan.get("steps", [])[: self.max_steps]
        for i, step in enumerate(steps):
            skill = str(step.get("skill", ""))
            args = dict(step.get("args") or {})
            why = str(step.get("why", ""))

            step_res = await self._run_step(skill, args, why)
            result.steps.append(step_res)

            if emit:
                try:
                    await emit({
                        "type": "brain_step",
                        "index": i + 1,
                        "total": len(steps),
                        **step_res.to_dict(),
                    })
                except Exception:
                    pass

            if not step_res.success:
                # Recovery: one re-plan with the failure in context
                recovered = await self._try_recover(utterance, step_res, ctx, session, skills_block, emit)
                if recovered is not None:
                    result.steps.extend(recovered.steps)
                    if recovered.success:
                        result.response = recovered.response or result.response
                        result.success = True
                        return result
                    result.response = recovered.response or result.response
                result.success = False
                result.response = (
                    result.response
                    or f"Couldn't complete: {step_res.skill} failed. {step_res.output[:200]}"
                )
                return result

        result.success = True
        if not result.response:
            done = [s for s in result.steps if s.success]
            result.response = "; ".join(s.output[:80] for s in done[-2:]) or "Done."
        return result

    # ------------------------------------------------------------------ planning
    async def _ask_plan(self, messages: list, emit: Optional[Callable]) -> dict:
        """One planner call → parsed JSON plan. Retries once on parse failure."""
        t0 = time.perf_counter()
        for attempt in range(2):
            resp = await self.gateway.chat(
                messages,
                json_schema=prompts.PLANNER_JSON_SCHEMA,
                temperature=0.1 if attempt == 0 else 0.3,
                max_tokens=900,
            )
            plan = extract_json_object(resp.content)
            if plan and "kind" in plan:
                if emit:
                    try:
                        await emit({
                            "type": "brain_plan",
                            "kind": plan.get("kind"),
                            "steps": len(plan.get("steps", []) or []),
                            "provider": resp.provider,
                            "model": resp.model,
                            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                        })
                    except Exception:
                        pass
                return plan
            log.warning(f"Plan parse failed (attempt {attempt + 1}); retrying.")
            messages = messages + [
                {"role": "assistant", "content": resp.content[:500]},
                {"role": "user", "content": "That was not valid JSON per the schema. Output ONLY the JSON object."},
            ]
        # last resort: treat as chat with empty response
        return {"kind": "chat", "response": "I couldn't plan that. Try rephrasing."}

    async def _try_recover(
        self, utterance: str, failed: BrainStepResult, ctx: Any, session: Any,
        skills_block: str, emit: Optional[Callable],
    ) -> Optional[BrainResult]:
        try:
            messages = prompts.build_recovery_messages(
                utterance, failed.to_dict(), failed.output,
                _ctx_block(ctx), session.context_block(), skills_block,
            )
            plan = await self._ask_plan(messages, emit)
            if plan.get("kind") != "action":
                return BrainResult(kind="chat", response=str(plan.get("response", "")))
            result = BrainResult(kind="action")
            for step in (plan.get("steps") or [])[:2]:
                step_res = await self._run_step(
                    str(step.get("skill", "")), dict(step.get("args") or {}), str(step.get("why", "")),
                )
                result.steps.append(step_res)
                if not step_res.success:
                    return result
            result.success = True
            result.response = str(plan.get("response", "")) or "Done."
            return result
        except (GatewayUnavailable, GatewayError) as e:
            log.warning(f"Recovery plan failed: {e}")
            return None

    # ------------------------------------------------------------------ step execution
    async def _run_step(self, skill: str, args: dict, why: str = "") -> BrainStepResult:
        t0 = time.perf_counter()
        try:
            if skill == "remember_fact":
                fact = str(args.get("fact", "")).strip()
                if fact:
                    self.store.add_fact(fact)
                    return BrainStepResult(skill, args, True, f"Remembered: {fact}", why,
                                           round((time.perf_counter() - t0) * 1000, 1))
                return BrainStepResult(skill, args, False, "No fact provided", why)

            if skill == "recall_memory":
                query = str(args.get("query", "")).strip()
                hits = self.store.search_episodic(query or "recent", limit=5)
                if hits:
                    lines = "\n".join(f"- {h.label} ({_fmt_ts(h.timestamp)})" for h in hits)
                    return BrainStepResult(skill, args, True, lines, why,
                                           round((time.perf_counter() - t0) * 1000, 1))
                return BrainStepResult(skill, args, True, "No matching past activity found.", why,
                                       round((time.perf_counter() - t0) * 1000, 1))

            tool = self.registry.get(skill)
            if tool is None:
                return BrainStepResult(skill, args, False, f"Unknown skill '{skill}'", why)

            exec_out = await self.executor(skill, args)
            success = bool(exec_out.get("success", False))
            output = str(exec_out.get("output") or exec_out.get("stdout") or "Done")
            return BrainStepResult(skill, args, success, output, why,
                                   round((time.perf_counter() - t0) * 1000, 1))
        except Exception as e:
            log.warning(f"Brain step '{skill}' raised: {e}")
            return BrainStepResult(skill, args, False, f"Execution error: {e}", why,
                                   round((time.perf_counter() - t0) * 1000, 1))


def _ctx_block(ctx: Any) -> str:
    try:
        d = ctx.to_dict()
    except Exception:
        d = {}
    active = d.get("active_title") or d.get("active_app") or "Desktop"
    apps = ", ".join((d.get("running_apps") or [])[:10])
    vol = d.get("volume_level", "?")
    return f"Frontmost: {active}\nRunning apps: {apps}\nVolume: {vol}"


def _fmt_ts(ts: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
