"""Artifacts must belong to the request the user is actually talking about.

Regression for: "Open WordPad and type the AI document there" followed by
"there is no heading, or title block in the file". The WordPad turn only
reported a bare filename, so no artifact was tracked and the follow-up was
answered about a presentation produced days earlier (index.html).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.memory.session import SessionMemory

RTF_NAME = "AI document about Artificial Intelligence.rtf"
OLD_DECK = r"C:\Users\USER\Desktop\remote-execution-vulnerability\index.html"


def _session(tmp_path, monkeypatch, artifacts_dir: Path) -> SessionMemory:
    monkeypatch.setenv("WINBROW_ARTIFACT_DIRS", str(artifacts_dir))
    monkeypatch.setenv("WINBROW_ARTIFACT_CONTEXT_MIN", "120")
    return SessionMemory(path=tmp_path / "session.json", session_id="t")


def _route() -> dict:
    return {"kind": "brain", "tool_name": "brain", "args": {"goal": "..."}}


def test_wordpad_bare_filename_becomes_an_artifact(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"
    docs.mkdir()
    saved = docs / RTF_NAME
    saved.write_text("Artificial Intelligence", encoding="utf-8")
    session = _session(tmp_path, monkeypatch, docs)

    session.record_turn(
        "Open WordPad and type the AI document there",
        _route(),
        {
            "success": True,
            "output": (
                "Wrote the document and saved it as "
                f"'{RTF_NAME}' and it is open in WordPad."
            ),
            "steps": [{"skill": "type_text", "args": {}, "success": True,
                       "output": "typed"}],
        },
    )
    assert session.last_artifacts() == [str(saved)]


def test_follow_up_about_the_file_uses_the_wordpad_document(
    tmp_path, monkeypatch,
):
    docs = tmp_path / "Documents"
    docs.mkdir()
    saved = docs / RTF_NAME
    saved.write_text("Artificial Intelligence", encoding="utf-8")
    session = _session(tmp_path, monkeypatch, docs)

    # An older, unrelated deck from a previous task.
    session.record_turn(
        "create a presentation about remote execution",
        _route(),
        {
            "success": True,
            "output": f"Saved to {OLD_DECK}",
            "steps": [{"skill": "run_skill_script", "success": True,
                       "args": {"output_parent": str(Path(OLD_DECK).parent)},
                       "output": f"Created {OLD_DECK}"}],
        },
    )
    session.turns[-1].ts = time.time() - 3 * 86400  # days old

    session.record_turn(
        "Open WordPad and type the AI document there",
        _route(),
        {
            "success": True,
            "output": f"The document is saved as '{RTF_NAME}'.",
            "steps": [{"skill": "type_text", "args": {}, "success": True,
                       "output": "typed"}],
        },
    )

    hint = session.reference_hint("there is no heading, or title block in the file")
    assert str(saved) in hint
    assert "index.html" not in hint
    assert "remote-execution-vulnerability" not in session.context_block()


def test_stale_artifacts_are_not_presented_as_current(tmp_path, monkeypatch):
    docs = tmp_path / "Documents"
    docs.mkdir()
    session = _session(tmp_path, monkeypatch, docs)

    session.record_turn(
        "create a presentation about remote execution",
        _route(),
        {"success": True, "output": f"Saved to {OLD_DECK}", "steps": []},
    )
    session.turns[-1].ts = time.time() - 3 * 86400
    session.record_turn(
        "open wordpad",
        _route(),
        {"success": True, "output": "WordPad open", "steps": []},
    )

    assert session.reference_hint("the file has no heading") == ""
    assert "remote-execution-vulnerability" not in session.context_block()
