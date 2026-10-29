"""Session context: artifact tracking and follow-up reference hints.

Regression for "where is the pdf?" after a presentation was created as HTML:
the session only kept a 200-char snippet, so the brain had no concrete path
and searched the whole disk instead of answering from the previous turn.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.brain.agent_loop import AgentBrain
from winbrow.brain.model_gateway import GatewayResponse
from winbrow.memory.session import SessionMemory
from winbrow.memory.store import MemoryStore

DECK = r"C:\Users\USER\Desktop\remote-execution-vulnerability\index.html"
PDF = r"C:\Users\USER\Desktop\kerala-vs-india\kerala-vs-india.pdf"


def _brain_turn(presentation_path: str, fmt_output: str = "") -> tuple[dict, dict]:
    route = {
        "kind": "brain",
        "tool_name": "brain",
        "args": {"goal": "create a presentation"},
    }
    execution = {
        "success": True,
        "output": (
            f"Created a complete presentation and saved it to "
            f"{presentation_path} {fmt_output}"
        ).strip(),
        "steps": [
            {
                "skill": "playbook_html_ppt",
                "args": {},
                "success": True,
                "output": "Playbook loaded.",
            },
            {
                "skill": "run_skill_script",
                "args": {
                    "skill": "html-ppt",
                    "script": "new_deck.py",
                    "spec": {
                        "name": "remote-execution-vulnerability",
                        "output_parent": r"C:\Users\USER\Desktop",
                    },
                },
                "success": True,
                "output": (
                    r"Scaffolded in C:\Users\USER\AppData\Local\Temp\wb\deck"
                ),
            },
            {
                "skill": "mcp_filesystem_write_file",
                "args": {
                    "path": presentation_path,
                    "content": "<!DOCTYPE html>",
                },
                "success": True,
                "output": f"Wrote {presentation_path}",
            },
        ],
    }
    return route, execution


def test_artifacts_are_recorded_from_brain_steps(tmp_path):
    session = SessionMemory(path=tmp_path / "s.json", session_id="ctx")
    route, execution = _brain_turn(DECK)
    session.record_turn("create the presentation", route, execution)

    assert session.turns[-1].artifacts == [DECK]
    assert DECK in session.context_block()
    assert any(e.value == DECK for e in session.entities["file"])


def test_reference_hint_flags_html_vs_pdf_mismatch(tmp_path):
    session = SessionMemory(path=tmp_path / "s.json", session_id="ctx")
    route, execution = _brain_turn(DECK)
    session.record_turn("create the presentation", route, execution)

    hint = session.reference_hint("where is the pdf?")

    assert DECK in hint
    assert "asked for 'pdf'" in hint
    assert "disk-wide search" in hint.lower()
    assert "offer to export/convert" in hint.lower()


def test_reference_hint_answers_matching_pdf(tmp_path):
    session = SessionMemory(path=tmp_path / "s.json", session_id="ctx")
    route, execution = _brain_turn(PDF, "exported as PDF")
    session.record_turn("create the pdf", route, execution)

    hint = session.reference_hint("where is the pdf?")

    assert PDF in hint
    assert "asked for 'pdf', but" not in hint
    assert "Answer with the exact path" in hint


def test_new_creation_request_is_not_a_followup(tmp_path):
    session = SessionMemory(path=tmp_path / "s.json", session_id="ctx")
    route, execution = _brain_turn(DECK)
    session.record_turn("create the presentation", route, execution)

    assert session.reference_hint(
        "now create me a presentation on remote execution vulnerability"
    ) == ""


def test_pronoun_action_uses_previous_artifact(tmp_path):
    session = SessionMemory(path=tmp_path / "s.json", session_id="ctx")
    route, execution = _brain_turn(DECK)
    session.record_turn("create the presentation", route, execution)

    hint = session.reference_hint("open it")
    assert DECK in hint


def test_artifacts_survive_persistence(tmp_path):
    path = tmp_path / "s.json"
    session = SessionMemory(path=path, session_id="ctx")
    route, execution = _brain_turn(DECK)
    session.record_turn("create the presentation", route, execution)

    loaded = SessionMemory(path=path, session_id="ctx")
    assert loaded.turns[-1].artifacts == [DECK]
    assert DECK in loaded.context_block()


class _Gateway:
    def __init__(self):
        self.seen: list[list[dict]] = []
        self.providers = [SimpleNamespace(name="fake", model="fake-1")]

    async def chat(self, messages, **kwargs):
        self.seen.append(messages)
        return GatewayResponse(
            content=json.dumps({
                "kind": "chat",
                "response": "The deck is HTML, not a PDF.",
            }),
            provider="fake",
            model="fake-1",
        )


class _Registry:
    def available(self, ctx):
        return []

    def all_tools(self):
        return []

    def get(self, name):
        return None


class _Ctx:
    def to_dict(self):
        return {"active_app": "Desktop", "running_apps": []}


def test_brain_prompt_contains_followup_artifact_hint(tmp_path):
    gateway = _Gateway()
    store = MemoryStore(
        db_path=tmp_path / "mem.db",
        chroma_path=tmp_path / "chroma",
    )
    brain = AgentBrain(gateway, _Registry(), store, executor=None)
    session = SessionMemory(path=tmp_path / "s.json", session_id="ctx")
    route, execution = _brain_turn(DECK)
    session.record_turn("create the presentation", route, execution)

    asyncio.run(brain.handle("where is the pdf?", _Ctx(), session))

    prompt_text = "\n".join(
        str(message.get("content") or "")
        for message in gateway.seen[0]
        if isinstance(message, dict)
    )
    assert "FOLLOW-UP ARTIFACT CONTEXT" in prompt_text
    assert DECK in prompt_text
    assert "asked for 'pdf'" in prompt_text
