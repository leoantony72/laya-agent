"""
Structured Trace Logging (M0 instrumentation)
=============================================
Append-only JSONL log of every executed command: utterance, routing tier,
decision, latencies, and outcome. This is the raw material for latency
tables, threshold calibration, and future fine-tuning. Survives restarts
(unlike the in-memory history ring buffer).

Log location: <repo>/logs/trace.jsonl (created on first write).
Failures to write a trace must never break command execution.
"""

from __future__ import annotations

import json
import logging
import pathlib
import threading
import time
from typing import Any

log = logging.getLogger("winbrow.trace")

TRACE_PATH = pathlib.Path(__file__).parent.parent / "logs" / "trace.jsonl"

_lock = threading.Lock()


def log_event(event: dict[str, Any]) -> None:
    """Append one event dict as a single JSON line. Never raises."""
    try:
        TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.time(), **event}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _lock:
            with open(TRACE_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as e:
        log.debug(f"Trace write failed (non-fatal): {e}")


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
