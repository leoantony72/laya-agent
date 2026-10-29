"""run_command limits: inline, temp script, hard reject, and policy."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from winbrow import shell


def _run(coro):
    return asyncio.run(coro)


def _fake_runner(captured):
    async def runner(wrapped, timeout, t0, script_path=None):
        captured["wrapped"] = wrapped
        captured["script_path"] = script_path
        return {"success": True, "stdout": "ok", "stderr": "",
                "returncode": 0, "elapsed_ms": 1.0}
    return runner


def test_six_k_command_runs_inline(tmp_path, monkeypatch):
    monkeypatch.setenv("WINBROW_SCRIPTS_DIR", str(tmp_path / "scripts"))
    captured = {}
    command = "Write-Output " + "x" * 6000
    with patch.object(shell, "_run_powershell_sync", _fake_runner(captured)):
        out = _run(shell.execute_shell_tool(
            "run_command", {"command": command}))
    assert out["success"]
    assert not list((tmp_path / "scripts").glob("*.ps1"))
    assert command in captured["wrapped"]
    assert out["script_path"] == ""


def test_twenty_k_command_uses_a_script_file(tmp_path, monkeypatch):
    monkeypatch.setenv("WINBROW_SCRIPTS_DIR", str(tmp_path / "scripts"))
    captured = {}
    command = "Write-Output " + "y" * 20000
    with patch.object(shell, "_run_powershell_sync", _fake_runner(captured)):
        out = _run(shell.execute_shell_tool(
            "run_command", {"command": command}))
    scripts = list((tmp_path / "scripts").glob("*.ps1"))
    assert len(scripts) == 1
    script_text = scripts[0].read_text(encoding="utf-8-sig")
    assert command in script_text
    assert captured["script_path"] == str(scripts[0])
    assert out["script_path"] == str(scripts[0])
    assert "long command ran as script" in out["output"]


def test_over_30k_is_rejected_with_guidance(tmp_path, monkeypatch):
    monkeypatch.setenv("WINBROW_SCRIPTS_DIR", str(tmp_path / "scripts"))
    command = "Write-Output " + "z" * 30001
    out = _run(shell.execute_shell_tool("run_command", {"command": command}))
    assert out["success"] is False
    assert "create_text_file" in out["output"]
    assert not list((tmp_path / "scripts").glob("*.ps1"))


def test_blocked_command_is_rejected_before_script_write(tmp_path, monkeypatch):
    monkeypatch.setenv("WINBROW_SCRIPTS_DIR", str(tmp_path / "scripts"))
    command = "Remove-Item C:\\Windows\\System32\\x -Recurse"
    out = _run(shell.execute_shell_tool("run_command", {"command": command}))
    assert out["success"] is False
    assert "blocked by policy" in out["output"].lower()
    assert not list((tmp_path / "scripts").glob("*.ps1"))
