"""
TTS — Windows SAPI spoken output (M4)
======================================
Free, local, instant on this hardware: PowerShell System.Speech synthesizer.
Fire-and-forget speech (background process; never blocks the loop).

Env:
  WINBROW_TTS=0        disable entirely (default: enabled for voice mode only)
  WINBROW_TTS_VOICE=   e.g. "Zira" / "David" (default: system voice)
  WINBROW_TTS_RATE=    -10..10 (default 1)
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Optional
import subprocess

log = logging.getLogger("winbrow.tts")


def tts_enabled() -> bool:
    return str(os.environ.get("WINBROW_TTS", "1")) not in ("0", "false", "off")


def _escape(text: str) -> str:
    return text.replace("'", "''").replace('"', '`"')


def _ps_script(text: str, voice: Optional[str], rate: int) -> str:
    ps = "Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    if voice:
        ps += f" try {{ $s.SelectVoice('{_escape(voice)}') }} catch {{ }};"
    if rate:
        ps += f" $s.Rate = {max(-10, min(10, int(rate)))};"
    ps += f" $s.Speak('{_escape(text)}');"
    return ps


def speak(
    text: str,
    voice: Optional[str] = None,
    rate: Optional[int] = None,
    wait: bool = False,
    timeout: float = 12.0,
) -> bool:
    """Speak text via Windows SAPI in a background process. Non-blocking by
    default; `wait=True` joins. Never raises; False when disabled/failed."""
    text = (text or "").strip()
    if not text:
        return True
    if not tts_enabled():
        return False
    voice = voice or os.environ.get("WINBROW_TTS_VOICE") or None
    rate = int(os.environ.get("WINBROW_TTS_RATE", "1")) if rate is None else int(rate)
    cap = 400  # spoken messages stay terse; cap runaway output
    try:
        proc = subprocess.Popen(
            [
                "powershell", "-NoProfile", "-Command",
                _ps_script(text[:cap], voice, rate),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if wait:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                log.warning("TTS timed out")
        return True
    except Exception as e:
        log.debug(f"TTS failed (non-fatal): {e}")
        return False


def speak_result(execution: dict, success_prefix: str = "Done") -> Optional[str]:
    """Build a SPOKEN-LENGTH summary of an execution result and speak it.

    Long outputs are truncated to a single sentence — the screen shows details.
    Returns the spoken text (None when disabled)."""
    if isinstance(execution, str):
        execs = {"type": "chat", "output": execution}
    else:
        execs = dict(execution or {})
    out = str(execs.get("output") or execs.get("reason") or "")
    etype = execs.get("type", "")
    if etype == "permission_required":
        tool = str((execs.get("pending") or {}).get("tool") or execs.get("tool") or "this action")
        text = f"Approval needed for {tool.replace('_', ' ')}. Say confirm, or tap approve in the panel."
    elif not out:
        text = "Done." if execs.get("success") else "That didn't work."
    elif execs.get("success"):
        text = out.split("\n")[0][:220]
        # Strip markdown-ish noise for speech
        text = text.replace("*", "").replace("`", "")
    else:
        text = "That didn't work. " + out.split("\n")[0][:160]

    if speak(text):
        return text
    return None
