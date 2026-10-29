"""
Shell command execution — the build/test/verify capability (M3-safe)
====================================================================
Provides `run_command`, a Python-backed tool the brain can use to compile,
build, test, or verify code inside a project folder without authoring a new
tool through `build_tool`.

Safety:
  • Commands are validated by `policy.validate_script_safety` before execution.
  • The permission gate classifies the command content: system-class commands
    require T3, user-data/plain commands stay T2.
  • A hard timeout caps long-running processes so a mistaken server start
    cannot wedge the task loop.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .policy import PolicyError, validate_script_safety
from .utils.clip import clip_middle, scripts_dir
from .windows import _run_powershell_sync

log = logging.getLogger("winbrow.shell")

SHELL_IMPL = "shell"

_MARKER = "__WINBROW_EXIT__:"
_MAX_TIMEOUT_S = 120
# Windows' -Command limit is 32,767 chars, and the wrapper adds quoting.
# Keep 12k inline; write a .ps1 between 12k and 30k; reject above 30k with an
# actionable message (create_text_file handles 200k if the user really needs it).
_INLINE_COMMAND_CHARS = 12_000
_MAX_COMMAND_CHARS = 30_000
_MAX_OUTPUT_CHARS = 100_000
_SCRIPT_RETENTION_DAYS = 7


def get_shell_tools():
    """Tool list registered into the ToolRegistry at init."""
    from .registry import ArgSpec, Tool

    return [
        Tool(
            name="run_command",
            description=(
                "Run a shell/PowerShell command in a working directory and return "
                "its stdout, stderr and exit code. Use it to compile, build, test "
                "or verify code in a project folder (e.g. 'go build ./...', "
                "'npm test', 'python -m pytest'). Never use it to start a "
                "long-running server or GUI app — run the build/check command "
                "instead."
            ),
            script="",
            impl=SHELL_IMPL,
            args=[
                ArgSpec(
                    name="command", kind="text",
                    instructions="Exact shell command to run, e.g. 'go build ./...'",
                ),
                ArgSpec(
                    name="cwd", kind="text",
                    instructions="Absolute working directory for the command",
                    default="",
                ),
                ArgSpec(
                    name="timeout", kind="text",
                    instructions="Seconds before the command is aborted (max 120)",
                    default="60",
                ),
            ],
            speak="result",
            examples=[
                "run go build in that folder",
                "compile the project",
                "run the tests in the project directory",
                "verify the go program builds",
            ],
        ),
    ]


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _prune_scripts(directory: Path) -> None:
    cutoff = time.time() - _SCRIPT_RETENTION_DAYS * 86400
    try:
        for path in directory.glob("*.ps1"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)
    except OSError:
        pass


def _write_command_script(command: str, cwd: Optional[str] = None) -> Path:
    """Persist a long command as a UTF-8 BOM .ps1 for ``powershell -File``.

    The permission gate and validate_script_safety already ran on the full
    original text in ``run_skill`` / ``execute_shell_tool`` before this point.
    """
    directory = scripts_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (
        f"step_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.ps1"
    )
    lines = [
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
        "$OutputEncoding = [System.Text.Encoding]::UTF8",
        "$ErrorActionPreference = 'Continue'",
        "$global:LASTEXITCODE = 0",
    ]
    if cwd:
        lines.append(f"Set-Location -LiteralPath {_ps_quote(cwd)}")
    lines.append(command)
    # UTF-8 with BOM is what Windows PowerShell 5.1 detects reliably.
    path.write_text("\n".join(lines), encoding="utf-8-sig")
    _prune_scripts(directory)
    return path


def _resolve_dir(value: str) -> Optional[str]:
    raw = (value or "").strip().strip('"').strip("'")
    if not raw:
        return None
    expanded = os.path.expandvars(os.path.expanduser(raw))
    path = Path(expanded)
    if path.is_dir():
        return str(path)
    return None


async def execute_shell_tool(tool_name: str, args: dict) -> dict[str, Any]:
    if tool_name != "run_command":
        return {"success": False, "output": f"Unknown shell op '{tool_name}'"}

    command = str(
        args.get("command") or args.get("cmd") or args.get("script") or ""
    ).strip()
    if not command:
        return {"success": False, "output": "No command provided for run_command."}
    if len(command) > _MAX_COMMAND_CHARS:
        return {
            "success": False,
            "output": (
                f"Command too long ({len(command)} chars; max "
                f"{_MAX_COMMAND_CHARS}). Write the script with "
                "create_text_file, then run that file instead."
            ),
        }
    try:
        validate_script_safety(command)
    except PolicyError as exc:
        return {"success": False, "output": f"Command blocked by policy: {exc}"}

    cwd_arg = str(
        args.get("cwd") or args.get("workdir") or args.get("working_directory")
        or args.get("folder") or args.get("path") or ""
    )
    cwd = _resolve_dir(cwd_arg)
    if cwd_arg.strip() and cwd is None:
        return {"success": False, "output": f"Working directory not found: {cwd_arg}"}

    try:
        timeout = int(str(args.get("timeout") or "60").strip() or "60")
    except ValueError:
        timeout = 60
    timeout = max(5, min(timeout, _MAX_TIMEOUT_S))

    script_path: Optional[Path] = None
    if len(command) > _INLINE_COMMAND_CHARS:
        try:
            script_path = _write_command_script(command, cwd)
        except OSError as exc:
            return {
                "success": False,
                "output": (
                    f"Command is too long for inline execution and the script "
                    f"could not be written: {exc}. Write it with "
                    "create_text_file and run that file instead."
                ),
            }

    if script_path is not None:
        res = await _run_powershell_sync(
            "", timeout, time.perf_counter(), script_path=str(script_path))
    else:
        lines = [
            "$ErrorActionPreference = 'Continue'",
            "$global:LASTEXITCODE = 0",
        ]
        if cwd:
            lines.append(f"Push-Location -LiteralPath {_ps_quote(cwd)}")
        lines += [
            "try {",
            f"  (& {{ {command} }} 2>&1 | Out-String) -split \"`r?`n\" | "
            "Where-Object { $_ -ne '' } | Write-Output",
            f"  Write-Output ({_ps_quote(_MARKER)} + $LASTEXITCODE)",
            "} finally {",
        ]
        if cwd:
            lines.append("  Pop-Location")
        lines.append("}")

        wrapped = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$OutputEncoding = [System.Text.Encoding]::UTF8; "
            + "\n".join(lines)
        )
        res = await _run_powershell_sync(wrapped, timeout, time.perf_counter())
    raw_out = (res.get("stdout") or "").strip()
    stderr = (res.get("stderr") or "").strip()

    exit_code: Optional[int] = None
    out_lines: list[str] = []
    for line in raw_out.splitlines():
        if line.strip().startswith(_MARKER):
            try:
                exit_code = int(line.strip()[len(_MARKER):].strip())
            except ValueError:
                pass
        else:
            out_lines.append(line)
    output = "\n".join(out_lines).strip()
    if stderr:
        output = f"{output}\n{stderr}".strip() if output else stderr

    if exit_code is None:
        returncode = res.get("returncode")
        if isinstance(returncode, int):
            # -File has no marker line; the process return code is the result.
            exit_code = returncode
            success = returncode == 0
            code_text = str(returncode)
        else:
            success = bool(res.get("success", False))
            code_text = "unknown"
    else:
        success = exit_code == 0
        code_text = str(exit_code)

    if not output:
        output = (
            f"Command completed with exit code {code_text}."
            if success else f"Command failed with exit code {code_text}."
        )
    # Tail-heavy: build/test errors are usually at the end of the log.
    output = clip_middle(
        output,
        _MAX_OUTPUT_CHARS,
        label="run_command",
        head_ratio=0.2,
        tail_ratio=0.5,
    )
    if script_path is not None:
        output = (
            f"[long command ran as script: {script_path}]\n{output}"
        ).strip()

    return {
        "success": success,
        "output": output,
        "exit_code": exit_code,
        "command": command,
        "script_path": str(script_path) if script_path else "",
        "cwd": cwd or os.getcwd(),
        "elapsed_ms": res.get("elapsed_ms", 0),
    }
