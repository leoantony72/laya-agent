"""
Voice pipeline v3 (M4) — VAD-gated hands-free loop
===================================================
Architecture (UPGRADE_PLAN §12.2):

    mic frames (16k mono int16)
      │
      ▼
    [VAD gate] silero ONNX (or energy fallback) — standby CPU ~2–5%
      │ only speech frames advance
      ▼
    [entry] wake-word phrase in stream  |  push-to-talk hotkey  |  file input
      │
      ▼
    [utterance capture] VAD end-of-speech (0.8s trailing silence), ≤30s
      │
      ▼
    [whisper.cpp / cloud STT] prompt primed from memory (recent files, apps,
      last session entities)  → proper nouns transcribe correctly
      │
      ├─ optional cloud STT (WINBROW_CLOUD_STT=1, Groq/openai whisper)
      ▼
    { text, confidence } → agent.execute() — same path as typed input
      │
      ▼
    [TTS — Windows SAPI] spoken result; spoken confirmations for T2/T3
      permission gates (say "confirm" / "cancel").

Stage latencies (vad/wake/stt/execute/tts/confirm) are traced via trace.stage_event
under a shared command_id.

Entry points:
  • python -m winbrow.voice            — default: push-to-talk (F9) or VAD+wake env
  • WINBROW_VOICE_MODE=vad|ptt|file    — mode selection
  • Headless/test: VoiceLoop(...).process_text("...") directly
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import wave
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import tts
from .agent import WinBrowAgent
from .memory import get_session, get_store
from .stt import AudioRecorder, TranscriptionResult, transcribe_best
from .trace import new_command_id, stage_event
from .vad import VADGate, ensure_silero_model
from .windows import get_current_windows_context

log = logging.getLogger("winbrow.voice")

WAKE_PHRASES = ("hey winbrow", "hey win brow", "hey winbro")

# End-of-speech / bounds
EOS_SILENCE_S = 0.8
MAX_UTTERANCE_S = 30.0
PRE_ROLL_S = 1.2               # audio kept before speech start (for fast starters)

# Push-to-talk default: F9
PTT_KEY_DEFAULT = 0x78


def build_prime_prompt(session: Any = None, store: Any = None,
                       ctx: Any = None) -> str:
    """Whisper decoder prompt from working memory: recent files, apps, entities.

    Prime list is rebuilt per utterance — the single highest-leverage fix for
    "it doesn't understand file names".
    """
    bits: list[str] = []
    try:
        d = ctx.to_dict() if hasattr(ctx, "to_dict") else (ctx or {})
        apps = (d.get("running_apps") or [])[:8]
        if apps:
            bits.append("Apps: " + ", ".join(apps))
    except Exception:
        pass
    try:
        if store is not None:
            bits.extend("File: " + m.label for m in store.get_recent_files(6))
    except Exception:
        pass
    try:
        if session is not None:
            ents: list[str] = []
            for vals in getattr(session, "entities", {}).values():
                ents.extend(getattr(e, "value", str(e)) for e in vals[-3:])
            if ents:
                bits.append("Names: " + ", ".join(ents[:8]))
    except Exception:
        pass
    return "Possible words: " + "; ".join(bits) if bits else ""


class VoiceLoop:
    """The orchestrator: mic frames → VAD → STT → agent → TTS.

    All external touches are injectable for headless tests:
      mic             — iterable/async-iterable of (bytes_pcm16, dt) frames
      transcriber     — async fn(path, initial_prompt) -> TranscriptionResult
      agent           — WinBrowAgent with .execute(text)
      key_state       — async fn() -> bool (push-to-talk key down?)
      confirm_fn      — async fn(description: dict) -> bool (voice confirm)
    """

    def __init__(
        self,
        agent: Optional[WinBrowAgent] = None,
        mode: Optional[str] = None,
        mic: Optional[Any] = None,
        transcriber: Optional[Callable] = None,
        key_state: Optional[Callable] = None,
        vad: Optional[VADGate] = None,
        speak_fn: Optional[Callable] = None,
        session_id: str = "default",
    ):
        self.mode = mode or os.environ.get("WINBROW_VOICE_MODE", "ptt").lower()
        self.agent = agent or WinBrowAgent()
        self.mic = mic                      # None = real AudioRecorder
        self.transcriber = transcriber
        self.vad = vad or VADGate()
        self.speak_fn = speak_fn or tts.speak_result
        self.key_state = key_state
        self.session_id = session_id
        self._wake_ready = False            # wake word spoken since last command?
        self.last_stages: list[dict] = []   # per-utterance stage report (tests/observability)

    # ------------------------------------------------------------------ UI hooks
    # Overridden by the server-side UIVoiceLoop. Base loop stays headless.
    def _speech_started(self) -> None:
        """VAD saw speech begin (island: enter the listening state)."""

    def _speech_tick(self, frames: list[bytes]) -> None:
        """~1/s while speech is in progress (island: partial transcript)."""

    def _speech_ended(self) -> None:
        """VAD saw end-of-speech, before transcription starts."""

    def _speech_unrecognized(self) -> None:
        """Speech ended but Whisper returned no text (UI returns to idle)."""

    def _on_level(self, level: float) -> None:
        """Microphone peak 0..1 from the recorder (island: waveform)."""

    # ------------------------------------------------------------------ helpers
    def _stages_reset(self) -> None:
        self.last_stages = []

    def _stage(self, cid: str, name: str, t0: float, **meta) -> float:
        ms = (time.perf_counter() - t0) * 1000
        stage_event(cid, name, ms, meta or None)
        self.last_stages.append({"stage": name, "elapsed_ms": round(ms, 1), **meta})
        return ms

    async def _transcribe(self, path: Path, prime: str) -> TranscriptionResult:
        t0 = time.perf_counter()

        async def _run() -> TranscriptionResult:
            if self.transcriber is not None:
                res = self.transcriber(path, initial_prompt=prime)
                if hasattr(res, "__await__"):
                    res = await res
                return res
            return await transcribe_best(path, initial_prompt=prime)

        try:
            res = await _run()
        except Exception as e:
            log.error(f"STT failed: {e}")
            res = TranscriptionResult(text="", language="en", duration_ms=0.0, segments=[])
        finally:
            _ms = (time.perf_counter() - t0) * 1000
        self.last_stages.append({"stage": "stt", "elapsed_ms": round(_ms, 1)})
        return res

    def _save_wav(self, frames: list[bytes]) -> Path:
        import tempfile
        fd = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        fd.close()
        with wave.open(fd.name, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(b"".join(frames))
        return Path(fd.name)

    @staticmethod
    def strip_wake(text: str) -> str:
        """Remove the wake phrase + a leading pause marker if present."""
        low = (text or "").strip().lower()
        for w in WAKE_PHRASES:
            if w in low:
                low = low.split(w, 1)[1].strip(" ,.!?")
                return low.strip()
        return low

    # ------------------------------------------------------------------ confirm hook
    async def voice_confirm(self, desc: dict) -> bool:
        """Spoken confirmation for T2/T3 actions: say the ask, listen for
        'confirm/yes' vs 'cancel/stop'. Wired via permissions.set_confirm_fn."""
        from .trace import stage_event as _st
        cid = new_command_id()
        t0 = time.perf_counter()
        tool = str(desc.get("tool") or "action")
        ask = (f"Approval needed: {tool.replace('_', ' ')}. "
               f"Say confirm to allow, or cancel.")
        tts.speak(ask)
        path = await self._capture(5.0)
        if path is None:
            _st(cid, "confirm", (time.perf_counter() - t0) * 1000,
                {"ok": False, "mode": "no-input"})
            return False
        try:
            res = await self._transcribe(path, prime="")
            answer = res.text.lower()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        allowed = any(w in answer for w in
                      ("confirm", "approve", "yes", "allow", "do it"))
        _st(cid, "confirm", (time.perf_counter() - t0) * 1000,
            {"ok": allowed, "answer": answer[:60]})
        if allowed:
            tts.speak("Allowed.")
            return True
        tts.speak("Denied.")
        return False

    async def _capture(self, seconds: float) -> Optional[Path]:
        """Voice capture: injected mic (frames iter) or the real recorder."""
        if self.mic is not None:
            frames: list[bytes] = []
            if isinstance(self.mic, (str, Path)):      # file mode: no live capture
                return Path(self.mic)
            start = time.perf_counter()
            try:
                async for chunk, _dt in self._iter_mic():
                    frames.append(chunk)
                    if time.perf_counter() - start >= seconds:
                        break
            except Exception:
                return None
            return self._save_wav(frames) if frames else None
        rec = AudioRecorder(on_level=self._on_level)
        try:
            rec.start()
            await asyncio.sleep(seconds)
            return rec.stop()
        except Exception as e:
            log.warning(f"Confirm capture failed ({e})")
            return None

    async def _iter_mic(self) -> Iterable:
        if hasattr(self.mic, "__aiter__"):
            async for item in self.mic:
                yield item
        elif hasattr(self.mic, "__iter__"):
            for item in self.mic:
                yield item
                await asyncio.sleep(0)
        else:
            return

    # ------------------------------------------------------------------ core utterance
    async def process_text(self, text: str, modality: str = "voice") -> dict:
        """One utterance → agent → spoken reply. The single merged entry point."""
        cid = new_command_id()
        self._stages_reset()
        t0 = time.perf_counter()
        result = await self.agent.execute(text, session_id=self.session_id,
                                          emit=None)
        self._stage(cid, "execute", t0, modality=modality, type=result.get("execution", {}).get("type"))
        exe = result.get("execution", {})
        spoken = None
        if modality == "voice":
            t1 = time.perf_counter()
            try:
                spoken = self.speak_fn(exe)
            except Exception as e:
                log.debug(f"Speak failed (non-fatal): {e}")
                spoken = None
            self._stage(cid, "tts", t1, ok=bool(spoken))
        result["voice"] = {"command_id": cid, "spoken": spoken,
                           "stages": list(self.last_stages)}
        return result

    # ------------------------------------------------------------------ main loop
    async def run(self) -> None:
        """Continuous loop. vad = wake-word in stream; ptt = hold hotkey."""
        from .permissions import get_permissions
        get_permissions().set_confirm_fn(self.voice_confirm)   # spoken T2/T3 asks
        ensure_silero_model()
        if self.mode == "file":
            if self.mic is None or not isinstance(self.mic, (str, Path)):
                raise ValueError("file mode requires mic=Path to a wav file")
            session = get_session(self.session_id)
            store = get_store()
            ctx = await asyncio.get_event_loop().run_in_executor(
                None, get_current_windows_context)
            res = await self._transcribe(Path(self.mic), build_prime_prompt(session, store, ctx))
            await self.process_text(res.text or "")
            return

        if self.mode == "vad":
            tts.speak("Voice mode on. Say 'hey winbrow' followed by a command.")
            await self._run_vad_wake()
            return

        tts.speak("Push to talk ready. Hold F9 and speak.")
        while True:
            down = await self._key_down()
            if not down:
                await asyncio.sleep(0.05)
                continue
            await asyncio.sleep(1.2)   # let the user press + start speaking
            self._speech_started()
            path = await self._capture(MAX_UTTERANCE_S)
            self._speech_ended()
            if path is None:
                continue
            prime = build_prime_prompt(get_session(self.session_id), get_store())
            res = await self._transcribe(path, prime)
            try:
                os.unlink(path)
            except OSError:
                pass
            text = res.text.strip()
            if not text:
                continue
            await self.process_text(text)

    async def _run_vad_wake(self) -> None:
        """VAD-gated wake word loop: transcribe ONLY while speech is present."""
        speaking = False
        silence_after = 0.0
        last_tick = 0.0
        utter_frames: list[bytes] = []
        pre_roll: list[bytes] = []
        async for chunk, dt in self._iter_mic():
            if self.vad.feed(chunk):
                if not speaking:
                    speaking = True
                    silence_after = 0.0
                    last_tick = time.perf_counter()
                    utter_frames = list(pre_roll[-max(1, int(PRE_ROLL_S / max(dt, 0.05))):])
                    stage_event(new_command_id(), "vad", 0,
                                {"event": "speech_start", "speech": True})
                    self._speech_started()
                utter_frames.append(chunk)
                silence_after = 0.0
                now = time.perf_counter()
                if now - last_tick >= 0.9:
                    last_tick = now
                    self._speech_tick(list(utter_frames))
            else:
                if speaking:                    # inside an utterance
                    utter_frames.append(chunk)
                    silence_after += dt
                    if silence_after >= EOS_SILENCE_S:
                        speaking = False
                        self._speech_ended()
                        await self._on_utterance(utter_frames)
                        utter_frames = []
                else:
                    pre_roll.append(chunk)
                    if len(pre_roll) > int(3.0 / max(dt, 0.05)):
                        pre_roll.pop(0)

    async def _on_utterance(self, frames: list[bytes]) -> None:
        path = self._save_wav(frames)
        try:
            session = get_session(self.session_id)
            store = get_store()
            ctx = await asyncio.get_event_loop().run_in_executor(
                None, get_current_windows_context)
            prime = build_prime_prompt(session, store, ctx)
            res = await self._transcribe(path, prime)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        text = self.strip_wake(res.text)
        if not text:
            self._speech_unrecognized()
            return
        stage_event(new_command_id(), "wake", res.duration_ms,
                    {"wake_matched": res.text.lower() != text, "text": text[:80]})
        await self.process_text(text)

    async def _key_down(self) -> bool:
        """Push-to-talk key policy (injectable)."""
        if self.key_state is not None:
            res = self.key_state()
            if hasattr(res, "__await__"):
                res = await res
            return bool(res)
        # Real fallback: GetAsyncKeyState polls (Windows)
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(PTT_KEY_DEFAULT) & 0x8000)


def _main() -> None:
    """CLI: python -m winbrow.voice [--mode vad|ptt|file] [wav files...]"""
    import argparse
    from .agent import WinBrowAgent

    ap = argparse.ArgumentParser(prog="winbrow.voice",
                                 description="WinBrow voice loop (VAD wake, push-to-talk, or file e2e)")
    ap.add_argument("--mode", default=os.environ.get("WINBROW_VOICE_MODE", "ptt"),
                    choices=["vad", "ptt", "file"],
                    help="vad: wake-gated mic; ptt: hold F9; file: process WAVs then exit")
    ap.add_argument("--session", default="voice")
    ap.add_argument("wavs", nargs="*", type=Path, help="WAV files (file mode)")
    args = ap.parse_args()

    if args.mode == "file" and not args.wavs:
        ap.error("file mode needs at least one wav path")

    agent = WinBrowAgent()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _process_files() -> None:
        for wav in args.wavs:
            vl = VoiceLoop(agent=agent, mode="file", mic=wav,
                           session_id=args.session)
            await vl.run()

    try:
        if args.mode == "file":
            loop.run_until_complete(_process_files())
        else:
            vl = VoiceLoop(agent=agent, mode=args.mode,
                           session_id=args.session)
            loop.run_until_complete(vl.run())
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    _main()
