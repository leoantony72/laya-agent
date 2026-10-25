"""Unit tests for the post-task skill writer.

Every test uses a fake gateway, so no API key or network call is required.
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

from winbrow.brain import skill_writer as sw
from winbrow.brain.model_gateway import GatewayResponse
from winbrow.brain.skill_registry import build_skills_metadata, load_skill_registry


class FakeGateway:
    """Scripted gateway that records calls without touching a provider."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.calls = 0
        self.providers = [SimpleNamespace(name="fake", model="fake-1")]

    @property
    def available(self) -> bool:
        return True

    async def chat(self, messages, json_schema=None, temperature=0.2,
                   max_tokens=900, timeout=None):
        self.calls += 1
        return GatewayResponse(
            content=json.dumps(self.payload),
            provider="fake",
            model="fake-1",
        )


def _step(skill: str, args: dict, success: bool = True,
          output: str = "ok") -> dict:
    return {
        "skill": skill,
        "args": args,
        "success": success,
        "output": output,
    }


def _upload_history() -> list[dict]:
    return [
        _step("find_files", {"search_term": "LDM seminar.pptx"},
              output=r"Found C:\Users\User\Documents\LDM seminar.pptx"),
        _step("browser_navigate", {"url": "https://www.aspose.app/pdf/viewer"},
              output="Opened viewer"),
        _step("browser_interact", {"selector": "#upload"},
              output="Clicked upload control"),
        _step("browser_upload_file",
              {"file_path": r"C:\Users\User\Documents\LDM seminar.pptx"},
              output="Attached file"),
        _step("browser_wait", {"seconds": 2}, output="Upload finished"),
        _step("browser_read_page", {}, output="Viewer shows the document"),
    ]


def _good_payload() -> dict:
    return {
        "should_save": True,
        "reason": "Reusable upload workflow with a parameterised file path.",
        "name": "upload-file-to-online-viewer",
        "skill_key": "upload_file_to_online_viewer",
        "description": (
            "Upload a local file to an online viewer. Use when the user asks "
            "to attach a file to a web page. Do not use for downloads or "
            "local file moves."
        ),
        "args": [
            {
                "name": "file_path",
                "type": "string",
                "required": True,
                "default": None,
                "description": "Absolute path of the file to upload.",
            }
        ],
        "when_to_use": (
            "Use when the user wants to upload a file to an online viewer."
        ),
        "when_not_to_use": (
            "Do not use for downloads, local file moves or folder creation."
        ),
        "step_pattern": (
            "1. Open the viewer site.\n"
            "2. Click the upload control.\n"
            "3. Attach the file at the file_path argument.\n"
            "4. Wait for the upload to finish.\n"
            "5. Read the page and confirm the file appears."
        ),
        "recovery": (
            "If the file input is missing, click the upload control again "
            "and retry once. If the upload stalls, verify the page before "
            "claiming success."
        ),
    }


def _analyse(payload: dict, history: list[dict], existing=None):
    gateway = FakeGateway(payload)
    candidate = asyncio.run(sw.analyse_task_for_skill(
        goal="upload LDM seminar.pptx to Aspose",
        history=history,
        existing_skill_names=existing or [],
        gateway=gateway,
    ))
    return candidate, gateway


def test_rejects_specific_filename(monkeypatch):
    called = {"count": 0}

    async def _never_write(*args, **kwargs):
        called["count"] += 1
        raise AssertionError("write_skill_to_disk must not run for a bad skill")

    monkeypatch.setattr(sw, "write_skill_to_disk", _never_write)

    payload = _good_payload()
    payload["name"] = "upload-ldmseminar-to-aspose"
    payload["skill_key"] = "upload_ldmseminar_to_aspose"

    candidate, _gateway = _analyse(payload, _upload_history())

    assert candidate.should_save is False
    assert "specific" in candidate.reason.lower()
    assert called["count"] == 0


def test_rejects_single_step_task():
    gateway = FakeGateway(_good_payload())
    candidate = asyncio.run(sw.analyse_task_for_skill(
        goal="open the calculator",
        history=[_step("app_focus_or_launch", {"app_name": "calculator"})],
        existing_skill_names=[],
        gateway=gateway,
    ))

    assert candidate.should_save is False
    assert gateway.calls == 0


def test_rejects_specific_entity_name():
    payload = _good_payload()
    payload["name"] = "open-pewdiepie-channel"
    payload["skill_key"] = "open_pewdiepie_channel"

    history = [
        _step("browser_navigate", {"url": "https://www.youtube.com/@PewDiePie"},
              output="Opened channel"),
        _step("browser_read_full", {}, output="PewDiePie channel page"),
        _step("browser_read_page", {}, output="Channel loaded"),
    ]
    gateway = FakeGateway(payload)
    candidate = asyncio.run(sw.analyse_task_for_skill(
        goal="open the PewDiePie channel on YouTube",
        history=history,
        existing_skill_names=[],
        gateway=gateway,
    ))

    assert candidate.should_save is False
    assert "specific" in candidate.reason.lower()


def test_extracts_file_path_as_arg(tmp_path):
    candidate, _gateway = _analyse(_good_payload(), _upload_history())

    assert candidate.should_save is True
    arg_names = [arg["name"] for arg in candidate.args]
    assert "file_path" in arg_names
    assert "seminar" not in candidate.step_pattern.lower()

    path = asyncio.run(sw.write_skill_to_disk(candidate, str(tmp_path)))
    content = Path(path).read_text(encoding="utf-8")

    assert "file_path" in content
    assert "LDM seminar" not in content
    assert "upload-file-to-online-viewer" in content

    registry = load_skill_registry(str(tmp_path))
    assert "upload_file_to_online_viewer" in registry


def test_no_duplicate_skill():
    candidate, _gateway = _analyse(
        _good_payload(),
        _upload_history(),
        existing=["upload_file_to_online_viewer"],
    )

    assert candidate.should_save is False
    assert "already exists" in candidate.reason.lower()


def test_hot_reload_updates_registry(tmp_path, monkeypatch):
    candidate, _gateway = _analyse(_good_payload(), _upload_history())
    assert candidate.should_save is True
    asyncio.run(sw.write_skill_to_disk(candidate, str(tmp_path)))

    from winbrow.brain import agent_loop

    registry: dict = {}
    monkeypatch.setattr(agent_loop, "SKILL_REGISTRY", registry)
    monkeypatch.setattr(agent_loop, "SKILLS_METADATA", "")

    asyncio.run(sw.register_skill_in_registry(
        candidate.skill_key, registry, str(tmp_path),
    ))

    assert candidate.skill_key in registry
    assert "online viewer" in agent_loop.SKILLS_METADATA
    assert "..." not in agent_loop.SKILLS_METADATA
    assert "..." not in build_skills_metadata(registry)


def test_hook_runs_in_background_after_done(monkeypatch):
    from winbrow.brain import agent_loop

    monkeypatch.setenv("WINBROW_SKILL_WRITER", "1")

    async def scenario():
        saved: list[str] = []
        release = asyncio.Event()

        class FakeBrain:
            async def _maybe_save_skill(self, goal, history):
                await release.wait()
                saved.append(goal)

        fake = FakeBrain()
        history = [
            SimpleNamespace(
                skill=f"tool_{index}", success=True, args={}, output="ok",
            )
            for index in range(3)
        ]
        before = set(agent_loop._SKILL_WRITER_TASKS)

        # Two successful steps are below the threshold: no model call, no task.
        agent_loop.AgentBrain._fire_skill_writer(fake, "goal", history[:2])
        assert agent_loop._SKILL_WRITER_TASKS - before == set()

        # Three successful steps schedule the writer and return immediately.
        agent_loop.AgentBrain._fire_skill_writer(fake, "goal", history)
        new_tasks = agent_loop._SKILL_WRITER_TASKS - before
        assert len(new_tasks) == 1
        task = next(iter(new_tasks))
        assert saved == []
        assert not task.done()

        release.set()
        await asyncio.gather(task)
        await asyncio.sleep(0)
        return saved

    assert asyncio.run(scenario()) == ["goal"]


def test_metadata_has_complete_sentences():
    registry = load_skill_registry(
        str(ROOT / "winbrow" / "skills"), sync_learned=False,
    )
    metadata = build_skills_metadata(registry)

    assert registry
    assert "..." not in metadata
    assert len(metadata) < 4000
