"""
Bundled skill-script runner
===========================
The converted skill library ships small, focused Python scripts next to each
SKILL.md (``winbrow/skills/library/<section>/<skill>/scripts/*.py``). The brain
runs them through this one native tool instead of interpolating user content
into a PowerShell command string.

Safety properties:
  * only files under the skill library are executable;
  * the skill and script names are validated as slug/file names;
  * path traversal and symlinks are rejected;
  * the spec travels as a JSON argument, not through a shell;
  * a hard timeout caps every run;
  * stdout/stderr are bounded before they reach planner history.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .registry import ArgSpec, Tool
from .utils.clip import clip_middle

SKILL_SCRIPTS_IMPL = "skill_script"
MAX_SPEC_CHARS = 200_000
MAX_OUTPUT_CHARS = 60_000
DEFAULT_TIMEOUT_S = 90
MAX_TIMEOUT_S = 120

_LIBRARY_ROOT = Path(__file__).resolve().parent / "skills" / "library"


def _library_root() -> Path:
    return _LIBRARY_ROOT.resolve()


def _valid_slug(value: str) -> bool:
    return bool(value) and all(
        char.isalnum() or char in "-_" for char in value
    )


def _resolve_script(skill: str, script: str) -> tuple[Path | None, str]:
    skill = (skill or "").strip()
    script = (script or "").strip()
    if not _valid_slug(skill):
        return None, "Invalid skill name. Use the folder name, e.g. 'docx'."
    if not script.lower().endswith(".py") or not all(
        char.isalnum() or char in "-_." for char in script
    ):
        return None, "Invalid script name. Expected a .py file from the skill."
    root = _library_root()
    direct = root / skill / "scripts" / script
    if direct.is_file() and not direct.is_symlink():
        candidate = direct.resolve()
    else:
        matches = [
            path.resolve()
            for path in root.rglob(f"{skill}/scripts/{script}")
            if path.is_file() and not path.is_symlink()
        ]
        # A skill name must be unique across the six category folders.
        unique = sorted(set(matches))
        if not unique:
            return None, f"Skill script not found: {skill}/scripts/{script}"
        if len(unique) > 1:
            return None, f"Ambiguous skill name '{skill}'; found {len(unique)} scripts."
        candidate = unique[0]
    try:
        candidate.relative_to(root)
    except ValueError:
        return None, "Script path escapes the skill library."
    if not candidate.is_file():
        return None, f"Skill script not found: {skill}/scripts/{script}"
    return candidate, ""


def _normalize_spec(raw: Any) -> tuple[str, str]:
    if raw in (None, ""):
        return "{}", ""
    if isinstance(raw, dict):
        text = json.dumps(raw, ensure_ascii=False)
    else:
        text = str(raw)
    if len(text) > MAX_SPEC_CHARS:
        return "", f"Spec is too large ({len(text)} chars; max {MAX_SPEC_CHARS})."
    try:
        parsed = json.loads(text)
    except Exception as exc:
        return "", f"Spec must be valid JSON: {exc}"
    if not isinstance(parsed, dict):
        return "", "Spec must be a JSON object."
    return json.dumps(parsed, ensure_ascii=False), ""


def _parse_script_output(stdout: str, stderr: str, returncode: int) -> dict[str, Any]:
    text = (stdout or "").strip()
    parsed: dict[str, Any] | None = None
    if text:
        # Scripts are expected to print one JSON object. Be forgiving about a
        # trailing newline or a log line before it.
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                candidate = json.loads(line)
            except Exception:
                continue
            if isinstance(candidate, dict):
                parsed = candidate
                break
    if parsed is None:
        output = text or (stderr or "").strip() or f"Script exited with code {returncode}."
        if returncode != 0 and stderr:
            # Surface the exception line, not the whole traceback, so planner
            # history stays readable. Full stderr is still available below.
            lines = [line.strip() for line in stderr.splitlines() if line.strip()]
            if lines:
                output = f"Script failed: {lines[-1]}"
        return {
            "success": returncode == 0,
            "output": clip_middle(
                output, MAX_OUTPUT_CHARS, label="skill_script"),
        }

    success = parsed.get("ok", parsed.get("success", returncode == 0))
    message = str(
        parsed.get("message")
        or parsed.get("output")
        or ("Script completed." if success else "Script failed.")
    )
    if not success and stderr:
        message = f"{message}\n{stderr.strip()}"
    result: dict[str, Any] = {
        "success": bool(success),
        "output": clip_middle(
            message, MAX_OUTPUT_CHARS, label="skill_script"),
    }
    data = parsed.get("data")
    if isinstance(data, (dict, list)):
        result["data"] = data
    for key in ("path", "paths", "files", "count"):
        if key in parsed:
            result[key] = parsed[key]
    return result


def execute_skill_script_tool(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one allowlisted skill script. Never raises."""
    args = args or {}
    if tool_name != "run_skill_script":
        return {"success": False, "output": f"Unknown skill-script op '{tool_name}'"}

    script_path, error = _resolve_script(
        str(args.get("skill") or ""), str(args.get("script") or ""),
    )
    if script_path is None:
        return {"success": False, "output": error}

    spec, error = _normalize_spec(args.get("spec"))
    if error:
        return {"success": False, "output": error}

    try:
        timeout = int(str(args.get("timeout") or DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_S
    timeout = max(5, min(timeout, MAX_TIMEOUT_S))

    repo_root = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo_root), env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)

    command = [
        sys.executable,
        str(script_path),
        "--spec-json",
        spec,
    ]
    try:
        proc = subprocess.run(
            command,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "output": f"Skill script timed out after {timeout}s.",
        }
    except Exception as exc:
        return {"success": False, "output": f"Could not run skill script: {exc}"}

    return _parse_script_output(proc.stdout or "", proc.stderr or "", proc.returncode)


async def execute_skill_script_tool_async(
    tool_name: str, args: dict[str, Any],
) -> dict[str, Any]:
    return await asyncio.to_thread(execute_skill_script_tool, tool_name, args)


def get_skill_script_tools() -> list[Tool]:
    """Registry records for the bundled skill-script runner."""
    return [
        Tool(
            name="run_skill_script",
            description=(
                "Run a bundled Python helper that belongs to a converted skill. "
                "Use only after a playbook's SKILL DETAIL names the exact skill, "
                "script and JSON spec. The script path is allowlisted to the "
                "skill library; the spec is passed as JSON, never through a shell."
            ),
            script="",
            impl=SKILL_SCRIPTS_IMPL,
            args=[
                ArgSpec(
                    name="skill", kind="text",
                    instructions=(
                        "Skill folder name from SKILL DETAIL, e.g. docx or "
                        "csv-summarizer."
                    ),
                ),
                ArgSpec(
                    name="script", kind="text",
                    instructions=(
                        "Exact script filename from SKILL DETAIL, e.g. "
                        "docx_tool.py."
                    ),
                ),
                ArgSpec(
                    name="spec", kind="text",
                    instructions=(
                        "JSON object with the operation and arguments named in "
                        "SKILL DETAIL."
                    ),
                    default="{}",
                ),
                ArgSpec(
                    name="timeout", kind="text",
                    instructions="Seconds before the script is aborted (5-120)",
                    default=str(DEFAULT_TIMEOUT_S),
                ),
            ],
            speak="result",
            examples=[
                "run the docx skill script",
                "use the bundled csv summarizer",
            ],
        ),
    ]
