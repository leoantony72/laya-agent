"""Playbook skills and the bundled skill-script runner."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow.brain.agent_loop import AgentBrain
from winbrow.brain.skill_registry import (
    SkillRegistryError,
    build_skills_metadata,
    load_skill_registry,
)
from winbrow import skill_scripts


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _playbook(name: str = "demo", category: str = "data-analysis") -> str:
    return f"""---
name: {name}
skill: playbook_{name}
kind: playbook
category: {category}
description: >
  Use when a test asks for the demo workflow. Do not use for anything else.
args: []
---

## When to Use

Test only.

## When Not to Use

Everywhere else.

## Step Pattern

1. Do the demo action.
"""


def test_playbook_parses_and_metadata_groups(tmp_path):
    _write(tmp_path / "demo" / "SKILL.md", _playbook())
    registry = load_skill_registry(str(tmp_path), sync_learned=False)

    meta = registry["playbook_demo"]
    assert meta.kind == "playbook"
    assert meta.category == "data-analysis"
    metadata = build_skills_metadata(registry)
    assert "playbook_demo" in metadata
    assert "DATA & ANALYSIS" in metadata
    assert "PLAYBOOK SKILLS" in metadata
    assert len(metadata) < 12000


def test_playbook_requires_prefixed_key(tmp_path):
    text = _playbook().replace("skill: playbook_demo", "skill: demo")
    _write(tmp_path / "demo" / "SKILL.md", text)

    try:
        load_skill_registry(str(tmp_path), sync_learned=False)
    except Exception as exc:  # load_skill_registry logs and skips
        assert "playbook_" in str(exc)
    else:
        # The loader skips malformed files; the registry must stay empty.
        assert load_skill_registry(str(tmp_path), sync_learned=False) == {}


def test_playbook_step_is_acknowledgement_only(tmp_path):
    _write(tmp_path / "demo" / "SKILL.md", _playbook())
    registry = load_skill_registry(str(tmp_path), sync_learned=False)

    brain = AgentBrain.__new__(AgentBrain)
    brain.skill_registry = registry
    result = asyncio.run(brain._run_step("playbook_demo", {}))

    assert result.success is True
    assert "Playbook loaded" in result.output
    assert "not task completion" in result.output


def test_skill_script_runs_json_spec(tmp_path, monkeypatch):
    library = tmp_path / "library"
    _write(
        library / "demo" / "scripts" / "echo.py",
        "import argparse, json\n"
        "p=argparse.ArgumentParser(); p.add_argument('--spec-json', default='{}')\n"
        "a=p.parse_args(); s=json.loads(a.spec_json)\n"
        "print(json.dumps({'ok': True, 'message': 'hi ' + s.get('name',''), "
        "'data': s}))\n",
    )
    monkeypatch.setattr(skill_scripts, "_LIBRARY_ROOT", library)

    out = skill_scripts.execute_skill_script_tool(
        "run_skill_script",
        {"skill": "demo", "script": "echo.py", "spec": {"name": "world"}},
    )
    assert out["success"] is True
    assert out["output"] == "hi world"
    assert out["data"] == {"name": "world"}


def test_skill_script_rejects_traversal(tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir(parents=True)
    monkeypatch.setattr(skill_scripts, "_LIBRARY_ROOT", library)

    out = skill_scripts.execute_skill_script_tool(
        "run_skill_script",
        {"skill": "..", "script": "escape.py", "spec": {}},
    )
    assert out["success"] is False
    assert "Invalid skill name" in out["output"]


def test_skill_script_rejects_bad_json(tmp_path, monkeypatch):
    library = tmp_path / "library"
    _write(library / "demo" / "scripts" / "echo.py", "print('{}')\n")
    monkeypatch.setattr(skill_scripts, "_LIBRARY_ROOT", library)

    out = skill_scripts.execute_skill_script_tool(
        "run_skill_script",
        {"skill": "demo", "script": "echo.py", "spec": "{not json"},
    )
    assert out["success"] is False
    assert "valid JSON" in out["output"]
