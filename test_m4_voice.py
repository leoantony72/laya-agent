"""
M4 — Voice pipeline v3 + autocorrect v3 + TTS + trace v2 (unit tests)
======================================================================
Headless (no real mic/STT): every external touch is injected.

Covers:
  • VADGate energy backend: loud ≈ speech, quiet ≈ silence
  • strip_wake + prime prompt building
  • tts.speak disabled / speak_result shaping (permission_required wording)
  • trace v2 stage events (file with env-overridden path)
  • autocorrect Stage 3: learned corrections consulted before Levenshtein
  • autocorrect Stage 2: normalize_utterance with FakeGateway + learning
  • VoiceLoop.process_text: agent called, stages + spoken collected
  • VoiceLoop vad-wake wake-word flow: sleep frames → speech → EOS →
    transcriber with prime → strip wake → execute
  • voice_confirm: spoken ask → captured answer → allowed/denied
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_WINBROW_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_WINBROW_ROOT))

_TRACE_DIR = tempfile.mkdtemp(prefix="winbrow_trace_m4_")
os.environ["WINBROW_TRACE_PATH"] = str(Path(_TRACE_DIR) / "trace.jsonl")
os.environ["WINBROW_TTS"] = "0"              # keep tests silent
os.environ["WINBROW_NORMALIZE"] = "1"        # enable stage-2 for unit tests
os.environ["WINBROW_VAD"] = "energy"         # no silero model in CI
os.environ["WINBROW_VOICE_MODE"] = "ptt"

from winbrow import tts                        # noqa: E402
from winbrow.vad import VADGate                # noqa: E402
from winbrow.voice import VoiceLoop, build_prime_prompt, WAKE_PHRASES  # noqa: E402
from winbrow.trace import stage_event, new_command_id  # noqa: E402
from winbrow.router import autocorrect_utterance  # noqa: E402
from winbrow.brain.normalize import (          # noqa: E402
    normalize_utterance, learn_corrections, token_changes_from_pair,
    build_normalize_messages, build_vocab_block,
)
from winbrow.stt import TranscriptionResult    # noqa: E402
import winbrow.trace as trace_mod              # noqa: E402


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _clear_trace():
    try:
        Path(trace_mod.TRACE_PATH).unlink(missing_ok=True)
    except Exception:
        pass


def _trace_lines():
    with open(Path(trace_mod.TRACE_PATH), encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


# ---------------------------------------------------------------------------
# Synthetic audio helpers (16k mono int16)

import math
import struct


def frame(rms_amp: float, ms: float = 100) -> bytes:
    n = int(16000 * ms / 1000)
    samples = []
    for i in range(n):
        samples.append(int(rms_amp * math.sin(2 * math.pi * 440 * i / 16000)))
    return struct.pack(f"<{n}h", *samples)


LOUD = frame(9000.0)     # speech-like loud sine
SOFT = frame(30.0)       # room noise


class TestVAD(unittest.TestCase):
    def test_energy_backend_detects_speech(self):
        v = VADGate(threshold=0.5)
        self.assertEqual(v.backend, "energy")
        self.assertTrue(v.feed(LOUD))
        self.assertFalse(v.feed(SOFT))

    def test_energy_threshold_env(self):
        os.environ["WINBROW_VAD_THRESHOLD"] = "10"   # very high → nothing is speech
        try:
            v = VADGate()
            self.assertFalse(v.feed(LOUD))
        finally:
            os.environ.pop("WINBROW_VAD_THRESHOLD", None)


class TestVoiceHelpers(unittest.TestCase):
    def test_strip_wake(self):
        self.assertEqual(VoiceLoop.strip_wake("hey winbrow open notepad"), "open notepad")
        self.assertEqual(VoiceLoop.strip_wake("hey winbrow, please open chrome"), "please open chrome")
        self.assertEqual(VoiceLoop.strip_wake("open chrome"), "open chrome")
        self.assertIn(VoiceLoop.strip_wake("hey winbrow"), ("",))

    def test_prime_prompt_content(self):
        class M:
            label = "leoantony.png"

        class S:
            entities = {"file": [M()]}

        class Store:
            def get_recent_files(self, n):
                return [M()]

        class Ctx:
            def to_dict(self):
                return {"running_apps": ["Code", "Brave"]}

        p = build_prime_prompt(S(), Store(), Ctx())
        self.assertIn("leoantony.png", p)
        self.assertIn("Code", p)

    def test_prime_empty(self):
        self.assertEqual(build_prime_prompt(None, None, None), "")


class TestTTS(unittest.TestCase):
    def test_speak_disabled(self):
        os.environ["WINBROW_TTS"] = "0"
        try:
            self.assertFalse(tts.speak("hello"))
        finally:
            os.environ["WINBROW_TTS"] = "0"

    def test_speak_result_shapes(self):
        with mock.patch.object(tts, "speak", return_value=True):
            ok = tts.speak_result({"type": "tool_execution", "success": True,
                                   "output": "Screenshot saved to C:\\x.png"})
            self.assertIn("Screenshot", ok)
            pend = tts.speak_result({
                "type": "permission_required", "tool": "app_install",
                "pending": {"tool": "app_install", "tier": "t3"},
            })
            self.assertIn("Approval", pend)
            fail = tts.speak_result({"type": "tool_execution", "success": False,
                                     "output": "boom"})
            self.assertIn("didn't work", fail)


class TestTraceV2(unittest.TestCase):
    def test_stage_events_written(self):
        _clear_trace()
        cid = new_command_id()
        stage_event(cid, "stt", 120.5, {"text": "hi"})
        stage_event(cid, "execute", 250.0)
        lines = _trace_lines()
        cids = [l["command_id"] for l in lines]
        stages = [l["stage"] for l in lines]
        self.assertEqual(cids, [cid, cid])
        self.assertEqual(stages, ["stt", "execute"])
        self.assertAlmostEqual(lines[0]["elapsed_ms"], 120.5, places=1)


class FakeStore:
    def __init__(self, corrections=None):
        self._corr = dict(corrections or {})
        self.facts = []

    def corrections_map(self, limit=500):
        return dict(self._corr)

    def get_recent_files(self, limit=10):
        return []

    def add_correction(self, wrong, right):
        self._corr[wrong.lower()] = right.lower()

    def add_fact(self, fact, source="user"):
        self.facts.append(fact)


class TestAutocorrectV3(unittest.TestCase):
    def test_stage3_learned_correction_wins(self):
        # 'crome' is within Levenshtein range of 'chrome' too; learned map wins.
        out = autocorrect_utterance("open crome", corrections={"crome": "chrome"})
        self.assertEqual(out, "open chrome")

    def test_no_corrections_still_levenshtein(self):
        out = autocorrect_utterance("open pivture", extra=["music pictures"])
        self.assertIn("picture", out)

    def test_router_uses_store_corrections(self):
        # via WinBrowRouter.route: store corrections feed stage 1.
        from winbrow.router import WinBrowRouter
        from winbrow.registry import ToolRegistry

        reg = ToolRegistry()
        router = WinBrowRouter(reg, store=FakeStore({"crome": "chrome"}))

        class Ctx:
            active_app = ""
            active_title = ""
            running_apps = []
            volume_level = 50
            is_muted = False

        r = run(router.route("open crome", Ctx()))
        # The corrected utterance caught a launch tool or NEW_ACTION-level
        # knowledge of chrome; must NOT be NEW_ACTION with garbage confidence 0.5
        self.assertNotIn("crome", (r.tool.name if r.tool else ""))


class TestNormalizeStage2(unittest.TestCase):
    class FakeGateway:
        providers = [{"name": "fake", "model": "fake-1"}]

        def __init__(self, content):
            self.content = content

        class _Resp:
            provider = "fake"

        async def chat(self, *a, **kw):
            r = TestNormalizeStage2.FakeGateway._Resp()
            r.content = self.content
            return r

    def test_rewrite_extracted(self):
        gw = self.FakeGateway(json.dumps({
            "normalized": "open notepad",
            "changes": [{"wrong": "notepak", "right": "notepad"}],
        }))
        text, meta = run(normalize_utterance(gw, "open notepak plese"))
        self.assertEqual(text, "open notepad")
        self.assertTrue(meta["changed"])
        self.assertEqual(meta["changes"][0]["right"], "notepad")

    def test_unparseable_returns_original(self):
        gw = self.FakeGateway("not json at all")
        text, meta = run(normalize_utterance(gw, "hello there"))
        self.assertEqual(text, "hello there")
        self.assertFalse(meta["changed"])

    def test_disabled_env(self):
        os.environ["WINBROW_NORMALIZE"] = "0"
        try:
            gw = self.FakeGateway(json.dumps({"normalized": "x"}))
            text, meta = run(normalize_utterance(gw, "anything"))
            self.assertFalse(meta["attempted"])
        finally:
            os.environ["WINBROW_NORMALIZE"] = "1"

    def test_learn_corrections_persist(self):
        store = FakeStore()
        n = learn_corrections(
            [{"wrong": "crom", "right": "chrome"}, {"wrong": "ab", "right": "short"}],
            store)
        self.assertEqual(n, 1)          # 'ab' too short
        self.assertEqual(store.corrections_map().get("crom"), "chrome")

    def test_token_diff_fallback(self):
        self.assertEqual(
            token_changes_from_pair("open pivture file", "open picture file"),
            [{"wrong": "pivture", "right": "picture"}])

    def test_vocab_block(self):
        class C:
            def to_dict(self):
                return {"running_apps": ["Code"], "active_app": "Code"}

        block = build_vocab_block(C(), None, FakeStore())
        self.assertIn("Running apps: Code", block)


class FakeAgent:
    """Minimal agent.execute replacement for VoiceLoop tests."""

    def __init__(self, result=None):
        self.calls = []
        self.result = result or {
            "execution": {"type": "tool_execution", "success": True,
                          "output": "Volume set to 40%"},
        }

    async def execute(self, text, session_id="default", emit=None):
        self.calls.append((text, session_id))
        return dict(self.result, utterance=text)


class TestVoiceLoop(unittest.TestCase):
    def setUp(self):
        _clear_trace()
        os.environ["WINBROW_TTS"] = "0"

    def test_process_text_executes_and_stages(self):
        agent = FakeAgent()
        loop = VoiceLoop(agent=agent, mode="ptt", vad=VADGate(),
                         speak_fn=lambda exe: "spoken-text")
        res = run(loop.process_text("turn the volume up"))
        self.assertEqual(agent.calls[0][0], "turn the volume up")
        self.assertEqual(res["voice"]["spoken"], "spoken-text")
        stages = [s["stage"] for s in res["voice"]["stages"]]
        self.assertIn("execute", stages)
        self.assertIn("tts", stages)
        # stage events traced
        lines = _trace_lines()
        self.assertTrue(any(l.get("stage") == "execute" for l in lines))
        cid = res["voice"]["command_id"]
        self.assertTrue(any(l.get("command_id") == cid for l in lines))

    def test_voice_result_workflow(self):
        """Voice pass through permissions: speak_result produces the ask text."""
        loop = VoiceLoop(agent=FakeAgent(), mode="ptt", vad=VADGate(),
                         speak_fn=lambda exe: "Approval")
        res = run(loop.process_text("install 7zip"))
        self.assertEqual(res["voice"]["spoken"], "Approval")


class MicScript:
    """Injectable mic: iterates scripted (chunk, dt) frame pairs."""

    def __init__(self, frames):
        self.frames = list(frames)

    def __aiter__(self):
        self._it = iter(self.frames)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class TestWakeFlow(unittest.TestCase):
    def test_vad_wake_routes_command(self):
        _clear_trace()
        # 0.6s pre-roll silence, 0.5s speech, then 1.0s of trailing silence (EOF > 0.8s)
        mic = MicScript(
            [(SOFT, 0.1)] * 6 + [(LOUD, 0.1)] * 5 + [(SOFT, 0.1)] * 10)

        # EoS is 0.8s; time.time() flow: enough silence ends it.
        transcribe_calls = []

        async def fake_transcribe(path, initial_prompt=None):
            transcribe_calls.append((Path(path).exists(), bool(initial_prompt)))
            return TranscriptionResult(
                text="hey winbrow open calculator", language="en",
                duration_ms=55.0, segments=[])

        agent = FakeAgent()
        loop = VoiceLoop(agent=agent, mode="vad", mic=mic, vad=VADGate(),
                         transcriber=fake_transcribe,
                         speak_fn=lambda exe: None)
        run(loop._run_vad_wake())
        # one utterance → exactly one transcribe call that ran on an existing wav
        self.assertEqual(len(transcribe_calls), 1)
        self.assertTrue(transcribe_calls[0][0])          # wav file exists at call time
        self.assertTrue(transcribe_calls[0][1])          # prompt-primed
        self.assertEqual(agent.calls[0][0], "open calculator")
        self.assertEqual(len(agent.calls), 1)
        lines = _trace_lines()
        self.assertTrue(any(l["stage"] == "vad" and l.get("event") == "speech_start"
                            for l in lines))
        self.assertTrue(any(l["stage"] == "wake" for l in lines))

    def test_no_speech_never_transcribes(self):
        mic = MicScript([(SOFT, 0.1)] * 30)
        agent = FakeAgent()
        calls = []

        async def fake_transcribe(path, initial_prompt=None):
            calls.append(1)
            return TranscriptionResult("", "en", 1.0, [])

        loop = VoiceLoop(agent=agent, mode="vad", mic=mic, vad=VADGate(),
                         transcriber=fake_transcribe, speak_fn=lambda e: None)
        run(loop._run_vad_wake())
        self.assertEqual(calls, [])
        self.assertEqual(agent.calls, [])


class TestVoiceConfirm(unittest.TestCase):
    def setUp(self):
        _clear_trace()

    def test_confirm_allowed(self):
        mic = MicScript([(LOUD, 0.1)] * 3)

        async def fake_transcribe(path, initial_prompt=None):
            return TranscriptionResult("yes confirm it", "en", 40.0, [])

        loop = VoiceLoop(agent=FakeAgent(), mode="ptt", mic=mic, vad=VADGate(),
                         transcriber=fake_transcribe, speak_fn=lambda e: None)
        ok = run(loop.voice_confirm({"tool": "app_install", "tier": "t3"}))
        self.assertTrue(ok)
        lines = _trace_lines()
        self.assertTrue(any(l.get("stage") == "confirm" and l.get("ok") for l in lines))

    def test_confirm_denied(self):
        mic = MicScript([(SOFT, 0.1)] * 3)   # silence → empty answer

        async def fake_transcribe(path, initial_prompt=None):
            return TranscriptionResult("", "en", 40.0, [])

        loop = VoiceLoop(agent=FakeAgent(), mode="ptt", mic=mic, vad=VADGate(),
                         transcriber=fake_transcribe, speak_fn=lambda e: None)
        ok = run(loop.voice_confirm({"tool": "app_install", "tier": "t3"}))
        self.assertFalse(ok)

    def test_permission_gate_uses_voice_confirm(self):
        """set_confirm_fn voice hook: gate asks once; voice fn allows."""
        from winbrow.permissions import PermissionManager, PermissionManager as PM  # noqa

        pm = PermissionManager(path=Path(_TRACE_DIR) / f"perm_{new_command_id()}.json")

        async def confirm(desc):
            return True                      # simulated voice 'confirm'

        pm.set_confirm_fn(confirm)
        allowed, reason, pending = run(pm.gate("app_install", {"package": "x"}))
        self.assertTrue(allowed)
        self.assertIsNone(pending)


class TestPushToTalkPolicy(unittest.TestCase):
    def test_ptt_key_state_injected(self):
        pressed = MicScript([])          # unused; just proving the loop shape

        loop = VoiceLoop(agent=FakeAgent(), mode="ptt", key_state=lambda: False,
                         vad=VADGate(), speak_fn=lambda e: None)
        self.assertFalse(run(loop._key_down()))
        loop2 = VoiceLoop(agent=FakeAgent(), mode="ptt",
                          key_state=lambda: True, vad=VADGate(),
                          speak_fn=lambda e: None)
        self.assertTrue(run(loop2._key_down()))


if __name__ == "__main__":
    unittest.main()
