"""Island voice contract: state.changed / transcript.* / agent.speaking.

These are the events the Dynamic Island renders. The service is exercised
without a microphone so the contract stays stable while the audio loop changes.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow import events
from winbrow.permissions import PENDING_TTL_S, PendingAction
from winbrow.voice_service import VoiceService


def _capture(monkeypatch):
    captured: list[tuple[str, dict]] = []

    def fake_emit(type_: str, payload: dict | None = None, **kwargs):
        captured.append((type_, payload or {}))
        return {"type": type_, "payload": payload or {}}

    monkeypatch.setattr(events, "emit", fake_emit)
    return captured


def test_listening_transcript_and_final(monkeypatch):
    captured = _capture(monkeypatch)
    service = VoiceService()

    service.on_speech_started()
    assert service.state == "listening"
    assert captured[-2] == (
        "state.changed",
        {"state": "listening", "muted": False, "level": 0.0},
    )
    assert captured[-1] == ("transcript.partial", {"text": ""})

    service.on_speech_ended()
    assert service.state == "thinking"

    service.on_final_text("open the downloads folder", "voice")
    assert captured[-1] == (
        "transcript.final",
        {"text": "open the downloads folder", "modality": "voice"},
    )
    assert service.status()["partial"] == ""


def test_level_is_throttled_and_muted(monkeypatch):
    captured = _capture(monkeypatch)
    service = VoiceService()

    service.on_level(0.4)
    service.on_level(0.9)
    states = [p for t, p in captured if t == "state.changed"]
    assert len(states) == 1  # 10 Hz ceiling
    assert states[0]["level"] == 0.4

    service.set_muted(True)
    captured.clear()
    service.on_level(0.9)
    assert captured == []
    assert service.status()["muted"] is True


def test_unrecognized_speech_returns_to_idle(monkeypatch):
    captured = _capture(monkeypatch)
    service = VoiceService()
    service.on_speech_started()
    service.on_speech_ended()
    service.on_speech_unrecognized()
    assert service.state == "idle"
    assert captured[-1][0] == "state.changed"
    assert captured[-1][1]["state"] == "idle"


def test_result_announces_speaking(monkeypatch):
    captured = _capture(monkeypatch)

    async def scenario():
        service = VoiceService()
        service.on_result("Exported the deck to your Desktop.")
        assert service.state == "speaking"
        assert (
            "agent.speaking",
            {"text": "Exported the deck to your Desktop."},
        ) in captured
        assert captured[-1][0] == "state.changed"
        assert captured[-1][1]["state"] == "speaking"
        task = service._speak_task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())


def test_pending_confirmation_payload_has_message_and_expiry():
    pending = PendingAction(
        id="abc123",
        tier="t2",
        tool="create_text_file",
        args={"path": "notes.txt"},
        utterance="write a file",
        session_id="default",
        created=1000.0,
    )
    payload = pending.to_dict()
    assert payload["message"] == "Allow T2 action 'create_text_file'?"
    assert payload["expires_at"] == 1000.0 + PENDING_TTL_S
