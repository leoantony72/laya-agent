"""
Autocorrect v3 — Stage 2 (brain-gateway normalize)
===================================================
When the heuristic/Laya route has low confidence and nothing fired, the
gateway gets ONE cheap "normalize" call: rewrite the utterance against a
live vocabulary block (running apps, tool names, recent entities, learned
corrections). DeepSeek-class models handle this trivially and replace the
whole "closest-word guess" class of error.

Stage 3: when a rewrite X→Y leads to a successful execution, the changed
token pairs are persisted via store.add_correction so future X tokens
normalize in Stage 1 (no LLM call).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Optional, Tuple

log = logging.getLogger("winbrow.normalize")

SYSTEM_PROMPT = """You normalize speech-to-text output for a Windows desktop assistant.
You receive a garbled utterance and a vocabulary block of true device names.
Fix ONLY the parts that match (or near-match) vocabulary: app names, file
names, folder names, tool names. Do NOT invent targets, do NOT change the
command's meaning or verbs.
Reply as JSON: {"normalized": "...", "changes": [{"wrong": "x", "right": "y"}, ...]}
If nothing needs fixing, return the original utterance with an empty changes list."""


def normalize_enabled() -> bool:
    return os.environ.get("WINBROW_NORMALIZE", "1") not in ("0", "false", "off")


def build_vocab_block(ctx: Any, session: Any = None, store: Any = None) -> str:
    """Live vocabulary hint block: apps, recent entities, tool-adjacent names."""
    lines: list[str] = []
    try:
        d = ctx.to_dict() if hasattr(ctx, "to_dict") else (ctx if isinstance(ctx, dict) else {})
        apps = d.get("running_apps") or []
        if apps:
            lines.append("Running apps: " + ", ".join(apps[:12]))
        if d.get("active_app"):
            lines.append(f"Active app: {d['active_app']}")
    except Exception:
        pass
    try:
        if session is not None:
            ents = []
            for kind, vals in getattr(session, "entities", {}).items():
                for e in vals[-3:]:
                    ents.append(getattr(e, "value", str(e)))
            if ents:
                lines.append("Recent entities: " + ", ".join(ents[:12]))
    except Exception:
        pass
    try:
        if store is not None and hasattr(store, "get_recent_files"):
            names = [m.label for m in store.get_recent_files(8)]
            if names:
                lines.append("Recent files: " + ", ".join(names))
    except Exception:
        pass
    try:
        if store is not None and hasattr(store, "corrections_map"):
            corr = store.corrections_map(50)
            if corr:
                lines.append("Known user words: " + ", ".join(corr.values()))
    except Exception:
        pass
    return "\n".join(lines) or "(no vocabulary available)"


def build_normalize_messages(utterance: str, vocab_block: str) -> list:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content":
            f"Vocabulary block:\n{vocab_block}\n\n"
            f"Utterance to normalize: '{utterance}'"},
    ]


def _extract_json(content: str) -> Optional[dict]:
    try:
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if not m:
            return None
        return json.loads(m.group(0))
    except Exception:
        return None


async def normalize_utterance(
    gateway: Any,
    utterance: str,
    ctx: Any = None,
    session: Any = None,
    store: Any = None,
    low_confidence: bool = True,
) -> Tuple[str, dict]:
    """Return (utterance, meta). Never raises; returns input on any failure."""
    meta: dict = {"attempted": False, "changed": False, "changes": []}
    if not normalize_enabled() or not low_confidence:
        return utterance, meta
    if gateway is None or not getattr(gateway, "providers", None):
        return utterance, meta

    meta["attempted"] = True
    t0 = time.perf_counter()
    try:
        messages = build_normalize_messages(
            utterance, build_vocab_block(ctx, session, store))
        resp = await gateway.chat(messages, temperature=0.1, max_tokens=200)
        data = _extract_json(resp.content)
        if not data:
            meta["error"] = "unparseable"
            return utterance, meta
        normalized = str(data.get("normalized") or utterance).strip()
        changes = data.get("changes") or []
        if not normalized or normalized.lower() == (utterance or "").lower():
            return utterance, meta
        # Safety: the rewrite must not be wildly longer or empty of content
        if len(normalized) > max(3 * len(utterance), 120):
            return utterance, meta
        meta["changed"] = True
        meta["changes"] = [
            {"wrong": str(c.get("wrong", ""))[:40], "right": str(c.get("right", ""))[:40]}
            for c in changes if isinstance(c, dict)
        ]
        meta["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        meta["provider"] = getattr(resp, "provider", "?")
        log.info(f"Stage-2 normalize: '{utterance}' → '{normalized}'")
        return normalized, meta
    except Exception as e:
        meta["error"] = str(e)[:120]
        return utterance, meta


def learn_corrections(changes: list[dict], store: Any) -> int:
    """Persist learned X→Y token pairs (Stage 3). Only for successfully
    executed commands. Returns count stored."""
    if store is None:
        return 0
    n = 0
    for c in changes or []:
        wrong, right = (c.get("wrong") or "").strip().lower(), (c.get("right") or "").strip().lower()
        if wrong and right and wrong != right and len(wrong) >= 3:
            try:
                store.add_correction(wrong, right)
                n += 1
            except Exception:
                pass
    return n


def token_changes_from_pair(utterance: str, normalized: str) -> list[dict]:
    """Fallback diff when the model returns no changes list."""
    a, b = utterance.split(), normalized.split()
    if len(a) != len(b):
        return []
    out = []
    for x, y in zip(a, b):
        if x.strip(".,!?;:\"'").lower() != y.strip(".,!?;:\"'").lower() and x.strip(".,!?;:\"'"):
            out.append({"wrong": x, "right": y})
    return out
