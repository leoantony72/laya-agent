"""
Structured Trace Logging (M0 instrumentation → M4 trace v2)
============================================================
Append-only JSONL log of every executed command: utterance, routing tier,
decision, latencies, and outcome. This is the raw material for latency
tables, threshold calibration, and future fine-tuning. Survives restarts
(unlike the in-memory history ring buffer).

Log location: <repo>/logs/trace.jsonl (created on first write).
Failures to write a trace must never break command execution.

v2 (M4): stage-level events — voice pipeline stages (vad/wake/stt/execute/
tts/confirm) carry a shared `command_id` so a single utterance can be joined
end-to-end; command events additionally carry per-stage latency breakdown.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
import time
import uuid
from typing import Any

log = logging.getLogger("winbrow.trace")

TRACE_PATH = pathlib.Path(
    os.environ.get("WINBROW_TRACE_PATH",
                   str(pathlib.Path(__file__).parent.parent / "logs" / "trace.jsonl")))

_lock = threading.Lock()


def trace_path() -> pathlib.Path:
    """Current trace path, read at call time so .env overrides apply.

    ``TRACE_PATH`` remains the import-time default for older callers.
    """
    raw = os.environ.get("WINBROW_TRACE_PATH", "").strip()
    return pathlib.Path(raw) if raw else TRACE_PATH


def new_command_id() -> str:
    """Correlate one command across all pipeline stages."""
    return uuid.uuid4().hex[:12]


def log_event(event: dict[str, Any]) -> None:
    """Append one event dict as a single JSON line. Never raises."""
    try:
        path = trace_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.time(), **event}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        log.debug(f"Trace write failed (non-fatal): {e}")


def stage_event(command_id: str, stage: str, elapsed_ms: float,
                meta: dict[str, Any] | None = None) -> None:
    """One pipeline-stage timing record (vad / wake / stt / execute / tts /
    confirm). `elapsed_ms` = duration of the stage; meta adds context."""
    event: dict[str, Any] = {
        "type": "stage",
        "command_id": command_id,
        "stage": stage,
        "elapsed_ms": round(float(elapsed_ms or 0.0), 1),
    }
    if meta:
        event.update(meta)
    log_event(event)


def command_event(
    utterance: str,
    tier: str,
    tool_name: str | None,
    confidence: float,
    route_latency_ms: float,
    success: bool | None,
    total_elapsed_ms: float,
    extra: dict[str, Any] | None = None,
) -> None:
    """Convenience wrapper for the per-command trace record."""
    event: dict[str, Any] = {
        "type": "command",
        "utterance": utterance,
        "tier": tier,
        "tool": tool_name,
        "confidence": round(float(confidence or 0.0), 3),
        "route_latency_ms": route_latency_ms,
        "success": success,
        "total_elapsed_ms": total_elapsed_ms,
    }
    if extra:
        event.update(extra)
    log_event(event)
