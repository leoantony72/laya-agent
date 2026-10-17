"""
M2 Tests — App Adapters + Procedural Memory (capture/replay)
=============================================================
Logic-level tests only:
  • SkillMemory: save/match/replay-support/delete/persistence/caps
  • Adapter ops registered uniformly, dispatcher arg validation, app grouping
  • Agent fast-path replay (<2s, no LLM) and post-brain capture
  • Server /api/skills endpoints

Real-world COM/CLI execution is gated behind WINBROW_M2_E2E=1.
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent))

# Isolate procedural memory BEFORE winbrow import
_SKILLS_DIR = tempfile.mkdtemp(prefix="winbrow_skills_m2_")
os.environ["WINBROW_SKILLS_DIR"] = _SKILLS_DIR

from winbrow.brain.model_gateway import GatewayResponse, ModelGateway
from winbrow.memory.skills import (MAX_STEPS, MATCH_THRESHOLD, SkillMemory,
                                   SkillRecord, SkillStep, match_score)
from winbrow.adapters.base import AdapterOp
from winbrow.adapters.manager import (ADAPTER_IMPL, ADAPTER_TOOLS,
                                      adapter_for_app, execute_adapter_tool,
                                      get_adapter_tools)

def run(coro):
    return asyncio.run(coro)


class FakeGateway(ModelGateway):
    def __init__(self, responses=None):
        self.providers = [SimpleNamespace(name="fake", model="fake-1")]
        self._responses = list(responses or [])
        self.calls = 0

    async def chat(self, messages, tools=None, json_schema=None,
                   temperature=0.2, max_tokens=900, timeout=None):
        import json
        self.calls += 1
        if not self._responses:
            return GatewayResponse(content=json.dumps(
                {"kind": "chat", "steps": [], "response": "()"}),
                provider="fake", model="fake-1")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return GatewayResponse(
            content=item if isinstance(item, str) else json.dumps(item),
            provider="fake", model="fake-1",
        )


class TestSkillMemory(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="skills_case_")
        self.store = SkillMemory(Path(self.dir) / "skills.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def steps(self, n=2):
        return [{"skill": f"tool_{i}", "args": {"k": str(i)}} for i in range(n)]

    def test_save_and_list(self):
        rec = self.store.save("Open Code", "open D:\\code in vscode",
                              self.steps(2), source="brain")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.id, "open-code")
        self.assertEqual(len(self.store), 1)
        self.assertEqual(len(rec.steps), 2)
        self.assertEqual(rec.steps[0].skill, "tool_0")
        self.assertEqual(rec.source, "brain")

    def test_dedup_same_slug_updates(self):
        self.store.save("x", "open code", self.steps(2))
        rec2 = self.store.save("x", "open code again", self.steps(3))
        self.assertEqual(len(self.store), 1)
        self.assertEqual(len(rec2.steps), 3)
        self.assertEqual(rec2.utterance, "open code again")

    def test_find_match_hit_and_threshold(self):
        self.store.save("ext install", "install the python extension in vscode", self.steps(1))
        hit = self.store.find_match("install the python extension in vscode")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0].id, "ext-install")
        self.assertGreaterEqual(hit[1], MATCH_THRESHOLD)
        # wordy-but-similar utterance still matches
        hit2 = self.store.find_match(
            "hey winbrow please install the python extension in vscode")
        self.assertIsNotNone(hit2)
        # unrelated utterance must NOT match
        self.assertIsNone(self.store.find_match("what time is it now"))

    def test_delete_and_mark_used(self):
        self.store.save("x", "open code", self.steps(1))
        self.store.mark_used("x")
        rec = self.store.get("x")
        self.assertEqual(rec.uses, 1)
        self.assertTrue(self.store.delete("x"))
        self.assertIsNone(self.store.get("x"))
        self.assertFalse(self.store.delete("x"))

    def test_persistence_across_instances(self):
        self.store.save("open code", "open code", self.steps(2))
        again = SkillMemory(Path(self.dir) / "skills.json")
        self.assertEqual(len(again), 1)
        self.assertEqual(again.find_match("open code")[0].id, "open-code")

    def test_max_steps_cap(self):
        big = [{"skill": f"t{i}", "args": {}} for i in range(MAX_STEPS + 5)]
        rec = self.store.save("big", "do the big flow", big)
        self.assertEqual(len(rec.steps), MAX_STEPS)

    def test_empty_steps_rejected(self):
        self.assertIsNone(self.store.save("n", "utterance", []))
        self.assertEqual(len(self.store), 0)

    def test_match_score(self):
        self.assertEqual(match_score("open code", "open code"), 1.0)
        self.assertLess(match_score("open code", "import os"), 1.0)
        self.assertEqual(match_score("", "open"), 0.0)


class TestAdapterOps(unittest.TestCase):
    def test_sixteen_ops_unique_names(self):
        names = [op.tool_name for op in ADAPTER_TOOLS]
        self.assertEqual(len(names), 16)
        self.assertEqual(len(names), len(set(names)))

    def test_ops_have_fns_and_args(self):
        for op in ADAPTER_TOOLS:
            self.assertIsNotNone(op.fn, op.tool_name)
            for spec in op.args.values():
                self.assertIn("instructions", spec)

    def test_get_adapter_tools_shape(self):
        tools = get_adapter_tools()
        self.assertEqual(len(tools), len(ADAPTER_TOOLS))
        for t in tools:
            self.assertEqual(t.impl, ADAPTER_IMPL)
            self.assertEqual(t.script, "")
            self.assertTrue(t.description)
            arg_names = {a.name for a in t.args}
            op = next(o for o in ADAPTER_TOOLS if o.tool_name == t.name)
            self.assertEqual(arg_names, set(op.args.keys()))

    def test_app_grouping(self):
        self.assertEqual(len(adapter_for_app("vscode")), 4)
        self.assertEqual(len(adapter_for_app("photoshop")), 3)
        self.assertEqual(len(adapter_for_app("notepad")), 2)
        self.assertEqual(len(adapter_for_app("paint")), 3)
        self.assertEqual(len(adapter_for_app("office")), 2)
        self.assertEqual(len(adapter_for_app("aftereffects")), 2)
        self.assertEqual(adapter_for_app("nosuchapp"), [])

    def test_dispatcher_unknown_op(self):
        res = run(execute_adapter_tool("vscode_nonexistent", {}))
        self.assertFalse(res["success"])

    def test_dispatcher_required_args(self):
        res = run(execute_adapter_tool("vscode_open", {}))
        self.assertFalse(res["success"])
        self.assertIn("target", res["output"])
        res2 = run(execute_adapter_tool("photoshop_save_as", {}))
        self.assertFalse(res2["success"])

    def test_dispatch_wrong_type_arg(self):
        res = run(execute_adapter_tool("vscode_open", {"target": "D:\\definitely\\not\\here\\x"}))
        self.assertFalse(res["success"])


class TestAgentWiring(unittest.TestCase):
    def setUp(self):
        from winbrow.memory.skills import reset_skills
        self.skills = reset_skills()
        self.skills.clear()      # isolation: wipe the shared json file
        from winbrow.agent import WinBrowAgent
        self.agent = WinBrowAgent(gateway=FakeGateway())
        self._stub_calls = []

        async def stub_run_skill(tool_name, args, **kw):
            self._stub_calls.append((tool_name, args))
            return {"success": True, "output": f"stub {tool_name}"}

        self._orig_run_skill = self.agent.run_skill
        self.agent.run_skill = stub_run_skill
        self.agent.brain.executor = stub_run_skill

        # instrumentation: brain questions the SAME skills store
        self.agent.skills = self.skills

    def tearDown(self):
        self.agent.run_skill = self._orig_run_skill

    def test_all_python_backed_registered(self):
        names = {t.name for t in self.agent.registry.all_tools()}
        for op in ADAPTER_TOOLS:
            self.assertIn(op.tool_name, names)

    def test_run_skill_adapter_dispatch(self):
        import winbrow.agent as A

        async def fake(name, args):
            return {"success": True, "output": "adapter-ok"}

        original = A.execute_adapter_tool
        real_run_skill = self._orig_run_skill
        self.agent.run_skill = real_run_skill        # need the real dispatcher
        A.execute_adapter_tool = fake
        try:
            out = run(self.agent.run_skill("vscode_open", {"target": "X:\\t"}))
        finally:
            A.execute_adapter_tool = original
        self.assertEqual(out["type"], "adapter")
        self.assertTrue(out["success"])

    def test_online_replay_quickpath(self):
        # M3: the replay step is T2 (user-data write) — pre-approve it the way
        # a real user would have (remembered allowlist from a prior confirmation).
        from winbrow.permissions import get_permissions, action_key
        get_permissions().remember(
            action_key("vscode_install_extension", {"extension_id": "ms-python.python"}),
            "t2", "vscode_install_extension")
        self.skills.save("ext", "install the python extension in vscode",
                         [{"skill": "vscode_install_extension",
                           "args": {"extension_id": "ms-python.python"}}])
        self.agent.skills = self.skills
        res = run(self.agent.execute(
            "install the python extension in vscode", session_id="m2t"))
        exe = res["execution"]
        self.assertEqual(exe["type"], "replayed_skill")
        self.assertTrue(exe["success"])
        self.assertLess(res["total_elapsed_ms"], 2000)   # acceptance: <2s
        self.assertEqual(res["route"]["tier"], "replay")
        # one stub op was executed, gateway never called
        self.assertEqual(len(self._stub_calls), 1)
        self.assertEqual(self.agent.gateway.calls, 0)

    def test_no_replay_on_unrelated(self):
        self.skills.save("ext", "install the python extension in vscode",
                         [{"skill": "vscode_install_extension", "args": {}}])
        self.agent.skills = self.skills
        res = run(self.agent.execute("what time is it", session_id="m2t"))
        self.assertNotEqual(res["execution"].get("type"), "replayed_skill")

    def test_capture_after_brain_success(self):
        plan = {"kind": "action", "steps": [
            {"skill": "vscode_open", "args": {"target": "D:\\x"}},
            {"skill": "computer_scroll", "args": {"direction": "down"}},
        ], "response": "done"}
        gw = FakeGateway([plan])
        self.agent.gateway = gw
        from winbrow.brain import AgentBrain
        from winbrow.brain.agent_loop import BrainResult
        # feed the brain the plan + fake executor already stubbed
        self.agent.brain.gateway = gw
        self.agent.brain.max_steps = 3
        res = run(self.agent.execute(
            "open project x in-vscode and prepare", session_id="m2t2"))
        self.assertTrue(res["execution"].get("success"))
        rec = self.skills.get("open-project-x-in-vscode-and-prepare".replace('_', '-')[:60])
        self.assertIsNotNone(rec)
        self.assertEqual(len(rec.steps), 2)
        self.assertEqual(rec.source, "brain")

    def test_capture_skips_meta_skills(self):
        plan = {"kind": "action", "steps": [
            {"skill": "remember_fact", "args": {"fact": "x"}},
        ], "response": "noted"}
        gw = FakeGateway([plan])
        self.agent.gateway = gw
        self.agent.brain.gateway = gw
        run(self.agent.execute("remember my name is pat", session_id="m2t3"))
        recs = [r.id for r in self.skills._records]
        self.assertEqual(recs, [])   # meta-only plan is NOT captured


class TestServerSkillsEndpoints(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("WINBROW_SKIP_MODEL_DL", "1")
        import server as srv
        from fastapi.testclient import TestClient
        cls.client = TestClient(srv.app)
        srv.agent.skills = SkillMemory(
            Path(_SKILLS_DIR) / "server_case.json")

    def test_endpoints(self):
        r = self.client.get("/api/skills")
        self.assertEqual(r.status_code, 200)
        self.assertIn("skills", r.json())

        # delete without id → 400
        r = self.client.post("/api/skills/delete", json={})
        self.assertEqual(r.status_code, 400)

        # delete unknown id → 200 deleted False
        r = self.client.post("/api/skills/delete", json={"skill_id": "nah"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["deleted"])

    def test_skill_roundtrip_on_store(self):
        self.client.app  # server context
        store = SkillMemory(Path(_SKILLS_DIR) / "roundtrip.json")
        store.save("fl", "open flash drive flow",
                   [{"skill": "notepad_open", "args": {}}])
        self.assertEqual(len(store), 1)


# --------------------------------------------------------------------------- E2E (opt-in)

@unittest.skipUnless(os.environ.get("WINBROW_M2_E2E"),
                     "E2E requires WINBROW_M2_E2E=1 (launches real apps)")
class TestE2E(unittest.TestCase):
    """
    Ground-truth flows on this machine. Run manually:
      set WINBROW_M2_E2E=1 && python -m pytest test_m2_adapters.py -k E2E -q
    """

    def test_vscode_available_and_list_extensions(self):
        from winbrow.adapters.vscode import list_extensions
        res = run(list_extensions({}))
        if not shutil.which("code"):
            self.skipTest("vscode CLI not on PATH")
        self.assertTrue(res["success"])
        self.assertTrue(res["extensions"])

    def test_paint_available(self):
        import shutil
        self.assertIsNotNone(shutil.which("mspaint"))

    def test_notepad_open_save_roundtrip(self):
        from winbrow.adapters.notepad import OPS
        fn = {op.op_id: op.fn for op in OPS}
        name = f"winbrow_m2_{int(__import__('time').time())}.txt"
        res1 = run(fn["open"]({}))
        self.assertTrue(res1["success"])
        res2 = run(fn["save_as"]({"filename": name}))
        self.assertTrue(res2["success"])
        self.assertIsNotNone(res2["saved_path"])
        os.remove(res2["saved_path"])


if __name__ == "__main__":
    unittest.main()
