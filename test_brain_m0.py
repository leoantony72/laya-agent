"""
M0 Tests — Brain, Model Gateway, Session Memory, Memory Store
=============================================================
Covers:
  • ModelGateway: provider chain, response parsing, JSON extraction, fallback
  • SessionMemory: entity extraction, reference resolution, persistence
  • MemoryStore: episodic record/search, facts, corrections
  • AgentBrain: plan→execute, meta-skills, recovery, chat
  • WinBrowAgent integration: NEW_ACTION → brain, fallback without gateway
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))

from winbrow.brain.model_gateway import (
    GatewayError,
    GatewayResponse,
    GatewayUnavailable,
    ModelGateway,
    PlannerFailure,
    extract_json_object,
)
from winbrow.brain.agent_loop import AgentBrain, BrainResult, _merge_clarify_answer
from winbrow.memory import MemoryStore, SessionMemory
from winbrow.registry import ToolRegistry
from winbrow.utils.history import (
    count_recent_stuck,
    detect_stuck,
    extract_tried_services,
    format_history,
    tried_domains,
)
from winbrow.windows import WindowsContext


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


class FakeGateway(ModelGateway):
    """Gateway with no providers by default; can be given scripted responses."""

    def __init__(self, responses=None):
        from types import SimpleNamespace
        # truthy provider marker with .name/.model for BrainResult display
        self.providers = [SimpleNamespace(name="fake", model="fake-1")]
        self._responses = list(responses or [])
        self.calls = 0

    async def chat(self, messages, tools=None, json_schema=None,
                   temperature=0.2, max_tokens=900, timeout=None):
        self.calls += 1
        if not self._responses:
            raise GatewayUnavailable("no scripted responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return GatewayResponse(
            content=item if isinstance(item, str) else json.dumps(item),
            provider="fake", model="fake-1",
        )


def plan(kind="action", steps=None, response=""):
    return {"kind": kind, "steps": steps or [], "response": response}


class TestJsonExtraction(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(extract_json_object('{"a":1}'), {"a": 1})

    def test_fenced_json(self):
        self.assertEqual(extract_json_object('```json\n{"a": 1}\n```'), {"a": 1})

    def test_json_with_prose(self):
        self.assertEqual(extract_json_object('Sure! {"kind":"chat"} done'), {"kind": "chat"})

    def test_garbage(self):
        self.assertIsNone(extract_json_object("no json here"))
        self.assertIsNone(extract_json_object(""))


class TestGatewayChain(unittest.TestCase):
    def test_no_providers_raises_unavailable(self):
        gw = FakeGateway()
        with self.assertRaises(GatewayUnavailable):
            run(gw.chat([{"role": "user", "content": "hi"}]))

    def test_openrouter_request_format(self):
        """Verify the exact request sent to OpenRouter will be correct."""
        import winbrow.brain.model_gateway as gmod
        gw = ModelGateway.__new__(ModelGateway)
        from winbrow.brain.model_gateway import Provider
        gw.providers = [Provider(
            name="openrouter", base_url="https://openrouter.ai/api/v1",
            api_key="sk-or-test", model="deepseek/deepseek-v4.1-flash",
            extra_headers={"HTTP-Referer": "https://github.com/winbrow", "X-Title": "WinBrow"},
        )]
        captured = {}

        def fake_post(url, payload, headers, timeout):
            captured["url"] = url
            captured["payload"] = payload
            captured["headers"] = headers
            return {
                "choices": [{"message": {"content": '{"kind":"chat","response":"Hi!"}'}}],
                "model": "deepseek/deepseek-v4.1-flash",
            }

        with patch.object(gmod, "_post_json", fake_post):
            resp = run(gw.chat(
                [{"role": "user", "content": "hello"}],
                json_schema={"type": "object"},
            ))
        self.assertEqual(captured["url"], "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer sk-or-test")
        self.assertIn("HTTP-Referer", captured["headers"])
        self.assertEqual(captured["payload"]["model"], "deepseek/deepseek-v4.1-flash")
        self.assertEqual(captured["payload"]["response_format"]["type"], "json_schema")
        self.assertEqual(captured["payload"]["provider"]["only"], ["relace"])
        self.assertFalse(captured["payload"]["provider"]["allow_fallbacks"])
        self.assertEqual(resp.provider, "openrouter")

    def test_tool_calls_parsed(self):
        import winbrow.brain.model_gateway as gmod
        gw = ModelGateway.__new__(ModelGateway)
        from winbrow.brain.model_gateway import Provider
        gw.providers = [Provider(name="openrouter", base_url="u", api_key="k", model="m")]

        def fake_post(url, payload, headers, timeout):
            return {
                "choices": [{"message": {
                    "content": "",
                    "tool_calls": [{"function": {
                        "name": "volume_set_level",
                        "arguments": '{"level": "50"}',
                    }}],
                }}],
            }

        with patch.object(gmod, "_post_json", fake_post):
            resp = run(gw.chat([{"role": "user", "content": "volume 50"}]))
        self.assertTrue(resp.has_tool_calls)
        self.assertEqual(resp.tool_calls[0].name, "volume_set_level")
        self.assertEqual(resp.tool_calls[0].arguments["level"], "50")

    def test_parse_response_captures_reasoning_and_finish_reason(self):
        from winbrow.brain.model_gateway import Provider
        gw = ModelGateway.__new__(ModelGateway)
        provider = Provider(name="openrouter", base_url="u", api_key="k", model="m")
        resp = gw._parse_response({
            "choices": [{
                "message": {"content": "", "reasoning": "thinking..."},
                "finish_reason": "length",
            }],
        }, provider)
        self.assertEqual(resp.reasoning, "thinking...")
        self.assertEqual(resp.finish_reason, "length")
        self.assertTrue(resp.truncated)

    def test_empty_content_from_truncated_reasoning_retries_with_capped_then_disabled(self):
        from winbrow.brain.model_gateway import Provider
        gw = ModelGateway.__new__(ModelGateway)
        gw.providers = [Provider(
            name="openrouter", base_url="u", api_key="k",
            model="deepseek/deepseek-v4.1-flash")]
        calls = []

        async def fake(provider, messages, tools, schema, temperature,
                       max_tokens, timeout, reasoning=None):
            calls.append(reasoning)
            if len(calls) < 3:
                return GatewayResponse(
                    content="", reasoning="long think cut off",
                    finish_reason="length", provider="openrouter", model="m")
            return GatewayResponse(
                content='{"kind":"chat","response":"hi"}',
                finish_reason="stop", provider="openrouter", model="m")

        gw._chat_with_provider = fake
        resp = run(gw.chat(
            [{"role": "user", "content": "hi"}],
            json_schema={"type": "object"}))
        self.assertEqual(resp.content, '{"kind":"chat","response":"hi"}')
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[1], {"max_tokens": 400})
        self.assertEqual(calls[2], {"enabled": False})

    def test_empty_content_after_all_reasoning_retries_raises(self):
        from winbrow.brain.model_gateway import Provider
        gw = ModelGateway.__new__(ModelGateway)
        gw.providers = [Provider(
            name="openrouter", base_url="u", api_key="k",
            model="deepseek/deepseek-v4.1-flash")]

        async def fake(provider, messages, tools, schema, temperature,
                       max_tokens, timeout, reasoning=None):
            return GatewayResponse(
                content="", reasoning="truncated mid-word",
                finish_reason="length", provider="openrouter", model="m")

        gw._chat_with_provider = fake
        with self.assertRaises(PlannerFailure) as ctx:
            run(gw.chat(
                [{"role": "user", "content": "hi"}],
                json_schema={"type": "object"}))
        self.assertEqual(ctx.exception.finish_reason, "length")
        self.assertGreater(ctx.exception.reasoning_length, 0)


    def test_openrouter_reasoning_budget_for_json_and_chat(self):
        import winbrow.brain.model_gateway as gmod
        from winbrow.brain.model_gateway import Provider
        provider = Provider(
            name="openrouter", base_url="https://openrouter.ai/api/v1",
            api_key="k", model="deepseek/deepseek-v4.1-flash")
        gw = ModelGateway.__new__(ModelGateway)
        payloads = []

        def fake_post(url, payload, headers, timeout):
            payloads.append(payload)
            return {"choices": [{"message": {"content": "{}"}}], "model": "m"}

        with patch.object(gmod, "_post_json", fake_post):
            run(gw._chat_with_provider(
                provider, [{"role": "user", "content": "hi"}],
                None, {"type": "object"}, 0.1, 1200, 10))
            run(gw._chat_with_provider(
                provider, [{"role": "user", "content": "hi"}],
                None, None, 0.1, 300, 10))

        self.assertIn("max_tokens", payloads[0]["reasoning"])
        self.assertLessEqual(
            payloads[0]["reasoning"]["max_tokens"], gmod.REASONING_MAX_TOKENS)
        self.assertEqual(payloads[1]["reasoning"], {"enabled": False})

    def test_openrouter_chain_from_env(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-test-123"}):
            gw = ModelGateway()
        self.assertTrue(gw.available)
        p = gw.providers[0]
        self.assertEqual(p.name, "openrouter")
        self.assertEqual(p.model, "deepseek/deepseek-v4.1-flash")
        self.assertTrue(p.api_key.startswith("sk-test"))

    def test_fallback_to_second_provider(self):
        from winbrow.brain.model_gateway import Provider
        gw = ModelGateway.__new__(ModelGateway)   # skip env sniffing
        failing = Provider(name="failing", base_url="http://x", api_key="k", model="m")
        backup = Provider(name="backup", base_url="http://y", api_key="k2", model="m2")
        gw.providers = [failing, backup]

        async def fake_chat_with_provider(provider, *a, **k):
            if provider.name == "failing":
                raise GatewayError("boom")
            return GatewayResponse(content="ok", provider="backup", model="m2")

        gw._chat_with_provider = fake_chat_with_provider
        resp = run(gw.chat([{"role": "user", "content": "hi"}]))
        self.assertEqual(resp.provider, "backup")


class TestStuckDetection(unittest.TestCase):
    def test_identical_skill_and_output_is_stuck(self):
        history = [
            {"step": {"skill": "browser_get_accessibility_tree"},
             "result": {"output": "INTERACTIVE ELEMENTS"}},
            {"step": {"skill": "browser_get_accessibility_tree"},
             "result": {"output": "INTERACTIVE ELEMENTS"}},
        ]
        self.assertTrue(detect_stuck(history))

    def test_different_output_is_progress(self):
        history = [
            {"step": {"skill": "browser_read_page"}, "result": {"output": "before"}},
            {"step": {"skill": "browser_read_page"}, "result": {"output": "after"}},
        ]
        self.assertFalse(detect_stuck(history))

    def test_brain_step_results_are_supported(self):
        from winbrow.brain.agent_loop import BrainStepResult
        history = [
            BrainStepResult("browser_get_accessibility_tree", {}, True, "same"),
            BrainStepResult("browser_get_accessibility_tree", {}, True, "same"),
        ]
        self.assertTrue(detect_stuck(history))

    def test_short_history_is_not_stuck(self):
        self.assertFalse(detect_stuck([
            {"step": {"skill": "browser_read_page"}, "result": {"output": "x"}},
        ]))

    def test_count_recent_stuck(self):
        history = [
            {"step": {"skill": "browser_read_page"},
             "result": {"output": "same",
                        "_warning": "STUCK: identical output"}},
            {"step": {"skill": "browser_read_page"},
             "result": {"output": "same",
                        "_warning": "STUCK: identical output"}},
            {"step": {"skill": "browser_read_page"},
             "result": {"output": "different"}},
        ]
        self.assertEqual(count_recent_stuck(history), 2)


class TestHistoryCompression(unittest.TestCase):
    def _history(self, n=8):
        return [{
            "step": {"skill": "browser_read_page",
                     "args": {"url": f"https://example.com/{i}"}},
            "result": {"success": True, "output": f"page {i} " + "x" * 2000},
        } for i in range(n)]

    def test_old_steps_compressed_recent_steps_full(self):
        block = format_history(self._history())
        self.assertIn("Step 1: browser_read_page → ✓", block)
        self.assertIn("--- last 5 steps in full ---", block)
        self.assertIn("Step 8:", block)
        # Older browser steps keep URL/title only; the last three keep up to
        # 2000 (and the newest 3000) characters of page state.
        self.assertLess(len(block), 8000)

    def test_old_file_search_keeps_matching_paths(self):
        history = [{
            "step": {"skill": "find_files",
                     "args": {"search_term": "LDMseminar"}},
            "result": {
                "success": True,
                "output": (
                    "Found 2 file(s) matching 'LDMseminar':\n"
                    "C:\\Users\\USER\\Downloads\\LDM seminar.pptx  (13.2 MB)\n"
                    "C:\\Users\\USER\\Desktop\\LDMseminar_report.txt  (9.2 KB)"
                ),
            },
        }]
        history.extend(self._history(6))
        block = format_history(history)
        self.assertIn("Step 1: find_files", block)
        self.assertIn("LDM seminar.pptx", block)
        self.assertIn("LDMseminar_report.txt", block)

    def test_tried_services_tracks_dead_ends(self):
        history = [
            {"step": {"skill": "browser_navigate",
                      "args": {"url": "https://www.example.com/a"}},
             "result": {"success": True, "output": "ok"}},
            {"step": {"skill": "browser_navigate",
                      "args": {"url": "https://bad.test/x"}},
             "result": {"success": False,
                        "output": "chrome-error://chromewebdata"}},
        ]
        block = extract_tried_services(history)
        self.assertIn("example.com → visited", block)
        self.assertIn("ERROR — chrome privacy/cert error", block)
        self.assertIn("Do not navigate to these again", block)
        self.assertEqual(tried_domains(history), ["example.com", "bad.test"])

    def test_planner_message_includes_tried_and_remaining(self):
        from winbrow.brain.prompts import build_loop_messages, build_planner_message

        history = [{
            "step": {"skill": "browser_navigate",
                     "args": {"url": "https://example.com/upload"}},
            "result": {"success": True, "output": "loaded"},
        }]
        messages = build_loop_messages(
            "finish the upload", format_history(history), "ctx", "mem", "skills",
            tried_block=extract_tried_services(history), steps_remaining=7)
        content = messages[1]["content"]
        self.assertIn("STEPS REMAINING: 7", content)
        self.assertIn("ALREADY TRIED:", content)
        direct = build_planner_message("finish the upload", history, 7)
        self.assertIn("STEPS REMAINING: 7", direct)
        self.assertIn("ALREADY TRIED:", direct)


class TestSessionMemory(unittest.TestCase):
    def _sess(self):
        return SessionMemory(path=Path(tempfile.gettempdir()) / f"sess_{id(self)}.json",
                             session_id=f"t{id(self)}")

    def test_entity_extraction_from_route(self):
        s = self._sess()
        route = {"kind": "tool", "tool_name": "open_file", "args": {"target": "leoantony.png"}}
        s.record_turn("open leoantony.png", route, {"success": True, "output": "Opened"})
        self.assertEqual(s.last_entity("file").value, "leoantony.png")

    def test_reference_resolution_that_file(self):
        s = self._sess()
        s.add_entity("file", r"C:\Users\X\Downloads\leoantony.png", "leoantony.png")
        out, resolutions = s.resolve_references("open that file")
        self.assertIn("leoantony.png", out)
        self.assertTrue(resolutions)

    def test_pronoun_resolution_short_command(self):
        s = self._sess()
        s.add_entity("folder", r"C:\Users\X\Downloads")
        out, res = s.resolve_references("close it")
        self.assertIn("Downloads", out)

    def test_pronoun_not_resolved_in_long_command(self):
        s = self._sess()
        s.add_entity("file", "x.png")
        out, res = s.resolve_references("open the file explorer and minimize every window on it")
        # 10+ words — pronoun substitution must NOT fire (would corrupt meaning)
        self.assertNotIn("x.png", out)

    def test_persistence_roundtrip(self):
        p = Path(tempfile.gettempdir()) / f"sess_rt_{id(self)}.json"
        s1 = SessionMemory(path=p, session_id="rt")
        s1.add_entity("app", "chrome")
        s1.record_turn("open chrome", {"kind": "tool", "tool_name": "app_focus_or_launch",
                                       "args": {"app_name": "chrome"}},
                       {"success": True, "output": "ok"})
        s2 = SessionMemory(path=p, session_id="rt")
        self.assertEqual(s2.last_entity("app").value, "chrome")
        self.assertEqual(len(s2.turns), 1)

    def test_context_block(self):
        s = self._sess()
        s.add_entity("file", "a.png")
        s.record_turn("open a.png", {"kind": "tool", "tool_name": "open_file",
                                     "args": {"target": "a.png"}},
                      {"success": True, "output": "Opened"})
        block = s.context_block()
        self.assertIn("open a.png", block)
        self.assertIn("file=a.png", block)

    def test_pending_clarification_options_roundtrip(self):
        s = self._sess()
        s.record_turn("open it", {"kind": "chat"},
                      {"success": True, "needs_input": True,
                       "question": "Which file?", "options": ["a.pptx", "b.pptx"]})
        self.assertEqual(s.pending_clarification["options"], ["a.pptx", "b.pptx"])
        s2 = SessionMemory(path=s.path, session_id=s.session_id)
        self.assertEqual(s2.pending_clarification["options"], ["a.pptx", "b.pptx"])
        self.assertIn("a.pptx", s2.context_block())
        s.record_turn("1", {"kind": "chat"}, {"success": True})
        self.assertIsNone(s.pending_clarification)

    def test_clarify_answer_can_be_option_text(self):
        s = self._sess()
        s.pending_clarification = {
            "goal": "open the pptx online",
            "question": "Which file?",
            "options": [r"C:\Users\X\a.pptx", r"C:\Users\X\b.pptx"],
        }
        merged = _merge_clarify_answer(r"C:\Users\X\b.pptx", s)
        self.assertIn("open the pptx online", merged)
        self.assertIn(r"C:\Users\X\b.pptx", merged)


class TestMemoryStore(unittest.TestCase):
    def _store(self):
        return MemoryStore(
            db_path=Path(tempfile.gettempdir()) / f"mem_{id(self)}.db",
            chroma_path=Path(tempfile.gettempdir()) / f"chroma_{id(self)}",
        )

    def test_record_and_search(self):
        st = self._store()
        st.record_command("open notepad and type hello",
                          {"tool_name": "open_common_app", "args": {"app": "notepad"}, "tier": "heuristic"},
                          {"success": True, "output": "Opened notepad"})
        hits = st.search_episodic("notepad type")
        self.assertTrue(any("notepad" in h.label for h in hits))

    def test_facts(self):
        st = self._store()
        st.add_fact("My exports folder is D:\\Exports")
        facts = st.get_facts()
        self.assertTrue(any("Exports" in f.label for f in facts))

    def test_corrections(self):
        st = self._store()
        st.add_correction("crome", "chrome")
        self.assertEqual(st.get_correction("CROME"), "chrome")
        self.assertIsNone(st.get_correction("unknown-token"))

    def test_search_empty_is_safe(self):
        st = self._store()
        self.assertEqual(st.search_episodic(""), [])
        self.assertEqual(st.search_episodic("zzz-nonexistent"), [])


class _FakeRegistry:
    """Minimal registry stub for the brain loop."""
    _tools = {
        "volume_set_level": ("Set master volume", {"level": "text"}),
        "app_focus_or_launch": ("Launch or focus app", {"app_name": "text"}),
        "window_action": ("Window min/max/close", {"action": "text"}),
    }

    def available(self, ctx):
        return [type("T", (), {"name": n, "description": d,
                               "args": [type("A", (), {"name": a, "kind": k})()
                                         for a, k in args.items()]})()
                for n, (d, args) in self._tools.items()]

    def get(self, name):
        return object() if name in self._tools else None


class _FakeCtx:
    def to_dict(self):
        return {"active_app": "Desktop", "active_title": "", "running_apps": [],
                "volume_level": 40}


class _FakeSession:
    def context_block(self):
        return "No prior conversation."


class TestAgentBrain(unittest.TestCase):
    def _brain(self, gateway, executed=None):
        async def executor(tool_name, args):
            if executed is not None:
                executed.append((tool_name, args))
            return {"success": True, "output": f"did {tool_name} with {args}"}
        return AgentBrain(gateway or FakeGateway(), _FakeRegistry(),
                          MemoryStore(db_path=Path(tempfile.gettempdir()) / f"bmem_{id(self)}.db"),
                          executor)

    def test_action_plan_executes_steps(self):
        executed = []
        gw_responses = [
            plan("action", steps=[
                {"skill": "app_focus_or_launch", "args": {"app_name": "notepad"}, "why": "open"},
                {"skill": "volume_set_level", "args": {"level": "40"}, "why": "set"},
            ], response="Opened notepad and set volume."),
        ]
        brain = self._brain(FakeGateway(gw_responses), executed)
        res = run(brain.handle("open notepad and set volume", _FakeCtx(), _FakeSession()))
        self.assertEqual(res.kind, "action")
        self.assertTrue(res.success)
        self.assertEqual(len(res.steps), 2)
        self.assertTrue(all(s.success for s in res.steps))
        self.assertEqual(executed[0], ("app_focus_or_launch", {"app_name": "notepad"}))

    def test_chat_plan_no_execution(self):
        brain = self._brain(FakeGateway([plan("chat", response="You have 3 files open.")]))
        res = run(brain.handle("how many files are open?", _FakeCtx(), _FakeSession()))
        self.assertEqual(res.kind, "chat")
        self.assertTrue(res.success)
        self.assertEqual(len(res.steps), 0)

    def test_remember_fact_meta_skill(self):
        brain = self._brain(FakeGateway([plan("action", steps=[
            {"skill": "remember_fact", "args": {"fact": "Exports folder is D:\\Exports"}},
        ])]))
        res = run(brain.handle("remember my exports folder is D:\\Exports", _FakeCtx(), _FakeSession()))
        self.assertTrue(res.success)
        facts = brain.store.get_facts()
        self.assertTrue(any("Exports" in f.label for f in facts))

    def test_recall_memory_meta_skill(self):
        brain = self._brain(FakeGateway([plan("action", steps=[
            {"skill": "recall_memory", "args": {"query": "notepad"}},
        ])]))
        brain.store.record_command("open notepad", {"tool_name": "open_common_app"},
                                   {"success": True, "output": "ok"})
        res = run(brain.handle("did i open notepad?", _FakeCtx(), _FakeSession()))
        self.assertTrue(res.success)
        self.assertIn("notepad", res.steps[0].output.lower())

    def test_failed_step_fails_honestly(self):
        brain = self._brain(None)

        async def failing_executor(tool_name, args):
            return {"success": False, "output": "app not found"}
        brain.executor = failing_executor
        brain.gateway._responses = [plan("action", steps=[
            {"skill": "app_focus_or_launch", "args": {"app_name": "ghostapp"}},
        ])]
        res = run(brain.handle("open ghostapp", _FakeCtx(), _FakeSession()))
        self.assertFalse(res.success)
        self.assertIn("failed", res.response.lower())

    def test_identical_outputs_get_stuck_warning(self):
        brain = self._brain(None)

        async def same_output(tool_name, args):
            return {"success": True, "output": "page unchanged"}

        brain.executor = same_output
        brain.gateway._responses = [
            plan("action", steps=[
                {"skill": "volume_set_level", "args": {"level": "40"}}]),
            plan("action", steps=[
                {"skill": "volume_set_level", "args": {"level": "41"}}]),
            plan("done", response="Done."),
        ]
        res = run(brain.handle("set volume twice", _FakeCtx(), _FakeSession()))
        self.assertEqual(len(res.steps), 2)
        self.assertIn("same skill twice", res.steps[-1].warning)

    def test_two_stuck_warnings_force_clarify(self):
        brain = self._brain(None)

        async def same_output(tool_name, args):
            return {"success": True, "output": "page unchanged"}

        brain.executor = same_output
        brain.gateway._responses = [
            plan("action", steps=[
                {"skill": "volume_set_level", "args": {"level": str(i)}}])
            for i in range(3)
        ]
        res = run(brain.handle("keep trying", _FakeCtx(), _FakeSession()))
        self.assertEqual(len(res.steps), 3)
        self.assertTrue(res.needs_input)
        self.assertIn("going in circles", res.question)

    def test_clarify_surfaces_options(self):
        options = [r"C:\Users\X\LDM seminar.pptx",
                   r"C:\Users\X\Latent_Diffusion_Models_Seminar.pptx"]
        brain = self._brain(FakeGateway([{
            "kind": "clarify",
            "question": "Which PowerPoint file should I open?",
            "options": options,
        }]))
        res = run(brain.handle("open it online", _FakeCtx(), _FakeSession()))
        self.assertTrue(res.needs_input)
        self.assertEqual(res.options, options)
        self.assertEqual(res.to_dict()["options"], options)

    def test_clarify_recovers_options_from_numbered_question(self):
        brain = self._brain(FakeGateway([{
            "kind": "clarify",
            "question": "Which one?\n1) LDM seminar.pptx\n2) Latent models.pptx",
        }]))
        res = run(brain.handle("open it online", _FakeCtx(), _FakeSession()))
        self.assertEqual(res.options, ["LDM seminar.pptx", "Latent models.pptx"])

    def test_max_steps_cap(self):
        brain = self._brain(None)
        brain.max_steps = 2
        steps = [{"skill": "volume_set_level", "args": {"level": str(i)}} for i in range(10)]
        brain.gateway._responses = [plan("action", steps=steps)]
        res = run(brain.handle("do many things", _FakeCtx(), _FakeSession()))
        self.assertLessEqual(len(res.steps), 2)


class TestAgentIntegration(unittest.TestCase):
    """NEW_ACTION routes to the brain; without a provider it falls back."""

    def _agent(self):
        from winbrow.agent import WinBrowAgent
        agent = WinBrowAgent(gateway=FakeGateway())
        agent.skills.find_match = lambda u: None
        # stub skill execution — no real system side effects in tests
        async def fake_skill(name, args):
            return {"success": True, "output": f"stub {name}"}
        agent.brain.executor = fake_skill
        return agent

    def test_new_action_uses_brain_when_gateway_available(self):
        agent = self._agent()
        agent.session.reset()
        agent.gateway._responses = [plan("action", steps=[
            {"skill": "volume_set_level", "args": {"level": "55"}},
        ])]
        # "make it a bit louder" needs conversation memory — heuristics miss it
        res = run(agent.execute("and then make it a bit louder", session_id="test"))
        self.assertEqual(res["execution"].get("type"), "brain")

    def test_chat_with_gateway_answers_from_context(self):
        agent = self._agent()
        agent.gateway._responses = [plan("chat", response="You have 2 files open.")]
        res = run(agent.execute("how many files are open?", session_id="test"))
        # Router classifies as NEW_ACTION → brain handles it and decides chat
        self.assertEqual(res["execution"].get("type"), "brain")
        self.assertEqual(res["execution"].get("kind"), "chat")
        self.assertIn("2 files", res["execution"]["output"])

    def test_chat_without_responses_falls_back_gracefully(self):
        agent = self._agent()
        agent.gateway._responses = [GatewayUnavailable("provider down")]
        res = run(agent.execute("how many files are open?", session_id="test"))
        # provider died → falls back to generator tier, never crashes
        self.assertIn(res["execution"].get("type"), ("generated_tool", "chat", "error"))

    def test_session_records_turn(self):
        agent = self._agent()
        run(agent.execute("who are you?", session_id="test"))
        from winbrow.memory import get_session
        s = get_session("test")
        self.assertTrue(any(t.utterance == "who are you?" for t in s.turns))


if __name__ == "__main__":
    unittest.main(verbosity=2)
