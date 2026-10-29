"""Voice mode as a server-managed service.

The existing ``VoiceLoop`` (VAD/wake/push-to-talk + local Whisper + SAPI) runs
as one asyncio task inside the sidecar instead of a separate CLI process, and
publishes transcript/speaking events so the island can show what was heard.

Voice stays local: no cloud realtime API, no audio leaves the machine.

Events consumed by the island (``/ws``):

    state.changed       {"state": "listening", "level": 0.42, "muted": false}
    transcript.partial  {"text": "open the …"}
    transcript.final    {"text": "open the downloads folder", "modality": "voice"}
    agent.speaking      {"text": "Opened Downloads"}

Partial transcripts are real: while VAD reports speech, the buffered audio is
re-transcribed every ~1s (opt out with ``WINBROW_PARTIAL_STT=0``). Peaks come
from the recorder, so the waveform follows the actual microphone.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

from . import events, tts
from .voice import VoiceLoop, build_prime_prompt, get_session, get_store

log = logging.getLogger("winbrow.voice.service")

# Partial transcription is CPU work; cap the window whisper sees so a long
# utterance does not get slower over time.
_PARTIAL_WINDOW_S = 6.0
_BYTES_PER_SECOND = 16000 * 2          # 16 kHz, 16-bit mono


def _partial_enabled() -> bool:
    return str(os.environ.get("WINBROW_PARTIAL_STT", "1")).lower() not in (
        "0", "false", "off",
    )


def _tail(frames: list[bytes], seconds: float = _PARTIAL_WINDOW_S) -> list[bytes]:
    """Keep roughly the last ``seconds`` of PCM frames."""
    budget = int(seconds * _BYTES_PER_SECOND)
    kept: list[bytes] = []
    total = 0
    for chunk in reversed(frames):
        kept.append(chunk)
        total += len(chunk)
        if total >= budget:
            break
    kept.reverse()
    return kept


class UIVoiceLoop(VoiceLoop):
    """VoiceLoop that mirrors its state into the UI event bus."""

    def __init__(self, *args: Any, service: "VoiceService", **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.service = service

    async def _capture(self, seconds: float):
        if self.service.muted:
            # Hard mute: run out the clock without keeping audio.
            await asyncio.sleep(min(max(seconds, 0.0), 1.0))
            return None
        self.service.on_speech_started()
        return await super()._capture(seconds)

    async def _iter_mic(self):
        # Hard mute for the VAD/wake loop too: drop frames before the VAD gate
        # so nothing is buffered, transcribed, or emitted.
        async for chunk, dt in super()._iter_mic():
            if self.service.muted:
                continue
            yield chunk, dt

    # Hooks from VoiceLoop -------------------------------------------------
    def _speech_started(self) -> None:
        self.service.on_speech_started()

    def _speech_tick(self, frames: list[bytes]) -> None:
        self.service.on_speech_tick(frames)

    def _speech_ended(self) -> None:
        self.service.on_speech_ended()

    def _speech_unrecognized(self) -> None:
        self.service.on_speech_unrecognized()

    def _on_level(self, level: float) -> None:
        self.service.on_level(level)

    async def process_text(self, text: str, modality: str = "voice") -> dict:
        self.service.on_final_text(text, modality)
        result = await super().process_text(text, modality=modality)
        spoken = (result.get("voice") or {}).get("spoken")
        self.service.on_result(spoken)
        return result


class VoiceService:
    def __init__(self) -> None:
        self.task: Optional[asyncio.Task] = None
        self.loop: Optional[UIVoiceLoop] = None
        self.agent: Any = None
        self.mode: str = ""
        self.muted: bool = False
        self.error: str = ""
        self.state: str = "idle"          # idle | listening | thinking | speaking
        self.level: float = 0.0
        self.partial: str = ""
        self.last_text: str = ""
        self.spoken: str = ""
        self._partial_task: Optional[asyncio.Task] = None
        self._speak_task: Optional[asyncio.Task] = None
        self._ptt_task: Optional[asyncio.Task] = None
        self._last_level_emit = 0.0

    # ------------------------------------------------------------------ status
    def status(self) -> dict:
        return {
            "running": bool(self.task and not self.task.done()),
            "mode": self.mode,
            "muted": self.muted,
            "error": self.error,
            "state": self.state,
            "level": round(self.level, 3),
            "partial": self.partial,
            "last_text": self.last_text,
            "spoken": self.spoken,
            "busy": bool(self._ptt_task and not self._ptt_task.done()),
        }

    # ------------------------------------------------------------------ ui events
    def _emit_state(self, state: Optional[str] = None, **extra: Any) -> None:
        if state is not None:
            self.state = state
        payload = {
            "state": self.state,
            "muted": self.muted,
            "level": round(self.level, 3),
        }
        payload.update(extra)
        try:
            events.emit("state.changed", payload)
        except Exception:
            pass

    def on_level(self, level: float) -> None:
        """Mic peak 0..1, throttled to ~10 Hz for the island waveform."""
        if self.muted:
            return
        self.level = max(0.0, min(1.0, float(level)))
        now = time.monotonic()
        if now - self._last_level_emit < 0.1:
            return
        self._last_level_emit = now
        self._emit_state()

    def on_speech_started(self) -> None:
        self.partial = ""
        self._emit_state("listening")
        try:
            events.emit("transcript.partial", {"text": ""})
        except Exception:
            pass

    def on_speech_tick(self, frames: list[bytes]) -> None:
        if self.muted or not _partial_enabled():
            return
        if self.mode != "vad":
            return
        if self._partial_task is not None and not self._partial_task.done():
            return
        self._partial_task = asyncio.create_task(
            self._partial_from_frames(_tail(frames)))

    def on_speech_ended(self) -> None:
        self.level = 0.0
        self._emit_state("thinking")

    def on_speech_unrecognized(self) -> None:
        self.partial = ""
        self.level = 0.0
        self._emit_state("idle")

    def on_final_text(self, text: str, modality: str) -> None:
        text = str(text or "").strip()
        self.partial = ""
        self.last_text = text
        if text:
            try:
                events.emit("transcript.final", {
                    "text": text, "modality": modality,
                })
            except Exception:
                pass

    def on_result(self, spoken: Optional[str]) -> None:
        if spoken:
            self.spoken = str(spoken)[:2000]
            try:
                events.emit("agent.speaking", {"text": self.spoken})
            except Exception:
                pass
            self._emit_state("speaking")
            if self._speak_task is not None and not self._speak_task.done():
                self._speak_task.cancel()
            self._speak_task = asyncio.create_task(
                self._speaking_window(self.spoken))
        else:
            self.spoken = ""
            self._emit_state("idle")

    async def _speaking_window(self, text: str) -> None:
        """TTS is fire-and-forget; wait for the process (or an estimate) then idle."""
        seconds = min(30.0, max(1.8, len(text) / 13.0))
        try:
            start = time.monotonic()
            deadline = start + seconds
            while time.monotonic() < deadline:
                # TTS failed to start or already finished: do not keep the
                # island in "speaking" for the whole estimate.
                if time.monotonic() - start > 0.5 and not tts.speaking():
                    break
                await asyncio.sleep(0.2)
            # SAPI may outlive the estimate (long sentences, slow voices).
            hard_stop = time.monotonic() + 45.0
            while tts.speaking() and time.monotonic() < hard_stop:
                await asyncio.sleep(0.5)
            # A new utterance may have started while the reply was finishing.
            if self.state == "speaking":
                self._emit_state("idle")
        except asyncio.CancelledError:
            raise

    async def _partial_from_frames(self, frames: list[bytes]) -> None:
        loop = self.loop
        if loop is None or not frames:
            return
        path: Optional[Path] = None
        try:
            path = loop._save_wav(frames)
            res = await loop._transcribe(path, "")
            text = (res.text or "").strip()
            if text and self.state == "listening" and not self.muted:
                self.partial = text
                events.emit("transcript.partial", {"text": text})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.debug(f"partial transcription skipped: {exc}")
        finally:
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    # ------------------------------------------------------------------ lifecycle
    async def start(self, agent: Any, mode: Optional[str] = None) -> dict:
        if self.task and not self.task.done():
            return self.status()
        self.agent = agent
        self.error = ""
        try:
            self.loop = UIVoiceLoop(
                agent=agent,
                mode=mode or None,
                service=self,
                session_id="default",
            )
            self.mode = self.loop.mode
            self.state = "idle"
            self.task = asyncio.create_task(self._run())
        except Exception as exc:  # missing mic/model, no audio device
            self.error = str(exc)
            log.warning(f"voice start failed: {exc}")
        return self.status()

    async def _run(self) -> None:
        try:
            assert self.loop is not None
            await self.loop.run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = str(exc)
            self._emit_state("idle", error=self.error)
            log.warning(f"voice loop stopped: {exc}")

    async def stop(self) -> dict:
        pending = [
            task
            for task in (self._ptt_task, self._partial_task, self._speak_task)
            if task is not None and not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._ptt_task = self._partial_task = self._speak_task = None
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
        self.task = None
        self.loop = None
        tts.stop_speaking()
        self.state = "idle"
        self.level = 0.0
        self.partial = ""
        self.spoken = ""
        events.emit("state.changed", {"state": "idle", "voice": "stopped"})
        return self.status()

    def set_muted(self, muted: bool) -> dict:
        self.muted = bool(muted)
        if self.muted:
            tts.stop_speaking()
            for name in ("_speak_task", "_ptt_task", "_partial_task"):
                task = getattr(self, name)
                if task is not None and not task.done():
                    task.cancel()
                setattr(self, name, None)
            self.level = 0.0
            self.partial = ""
            self.state = "idle"
        else:
            self.state = "listening" if (self.task and not self.task.done()) else "idle"
        self._emit_state()
        return self.status()

    async def toggle(self, agent: Any) -> dict:
        if self.task and not self.task.done():
            return await self.stop()
        return await self.start(agent)

    # ------------------------------------------------------------------ actions
    async def push_to_talk(self) -> dict:
        """One-shot capture for the island button (fallback to the wake word)."""
        if self.muted:
            return {**self.status(), "error": "Microphone is muted."}
        if self._ptt_task is not None and not self._ptt_task.done():
            return self.status()
        if self.loop is None:
            # PTT is a one-shot capture; do not start the continuous F9 loop.
            try:
                self.loop = UIVoiceLoop(
                    agent=self.agent, mode="ptt", service=self,
                    session_id="default",
                )
                self.mode = "ptt"
            except Exception as exc:
                self.error = str(exc)
                return {**self.status(), "error": self.error}
        self._ptt_task = asyncio.create_task(self._run_ptt())
        return self.status()

    async def _run_ptt(self) -> None:
        loop = self.loop
        if loop is None:
            return
        self.error = ""
        self._emit_state("listening")
        path: Optional[Path] = None
        try:
            path = await loop._capture(8.0)
            self._emit_state("thinking")
            if path is None:
                self._emit_state("idle")
                return
            prime = ""
            try:
                ctx = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: build_prime_prompt(
                        get_session("default"), get_store()))
                prime = ctx or ""
            except Exception:
                prime = ""
            res = await loop._transcribe(path, prime)
            text = (res.text or "").strip()
            if text:
                await loop.process_text(text, modality="voice")
            else:
                self._emit_state("idle")
        except asyncio.CancelledError:
            self._emit_state("idle")
            raise
        except Exception as exc:
            self.error = str(exc)
            self._emit_state("idle", error=self.error)
            log.warning(f"push-to-talk failed: {exc}")
        finally:
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    async def interrupt(self) -> dict:
        """Barge-in: stop the current spoken reply (or capture)."""
        tts.stop_speaking()
        pending = [
            task
            for task in (self._speak_task, self._ptt_task)
            if task is not None and not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._speak_task = None
        self._ptt_task = None
        self.spoken = ""
        if self.task and not self.task.done():
            self._emit_state("listening")
        else:
            self._emit_state("idle")
        return self.status()


_service: Optional[VoiceService] = None


def get_voice_service() -> VoiceService:
    global _service
    if _service is None:
        _service = VoiceService()
    return _service
