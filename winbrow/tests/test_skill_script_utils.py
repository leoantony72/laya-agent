"""Dependency-error messages must distinguish missing packages from blocked
native imports, and must name the interpreter the agent actually uses."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(code: str, extra_path: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    paths = [str(ROOT)]
    if extra_path is not None:
        paths.append(str(extra_path))
    env["PYTHONPATH"] = os.pathsep.join(paths)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def test_missing_package_names_the_interpreter():
    proc = _run(
        "from winbrow.skill_script_utils import require_module; "
        "require_module('definitely_missing_winbrow_module', "
        "'definitely-missing-package')"
    )
    assert proc.returncode == 1
    message = json.loads(proc.stdout.strip())["message"]
    assert "Missing Python package 'definitely-missing-package'" in message
    assert "-m pip install definitely-missing-package" in message
    assert sys.executable in message


def test_import_failure_is_not_reported_as_missing_package(tmp_path):
    broken = tmp_path / "broken_native_module.py"
    broken.write_text(
        "raise ImportError('DLL load failed while importing _native: "
        "blocked by Application Control')\n",
        encoding="utf-8",
    )
    proc = _run(
        "from winbrow.skill_script_utils import require_module; "
        "require_module('broken_native_module', 'broken-native-package')",
        extra_path=tmp_path,
    )
    assert proc.returncode == 1
    message = json.loads(proc.stdout.strip())["message"]
    assert "Could not import 'broken-native-package'" in message
    assert "DLL load failed" in message
    assert "Missing Python package" not in message
    assert sys.executable in message
    assert "Application Control" in message
