"""
Installer — winget-first package management with streaming progress (M3)
=========================================================================
Preference order (UPGRADE_PLAN §5):
  1. winget (search/install/upgrade/uninstall, silent flags) — always used first
  2. Direct download (MSI/EXE) — only when winget misses; T3-confirmed,
     policy-checked (no install scripts without explicit consent)
  3. Store/MSIX — surfaced for manual UAC, never bypassed silently

UAC rules:
  • Never attempt silent elevation.
  • The user is TOLD if admin approval is needed ("confirm the UAC dialog").
  • Failures suggest a retry via a different source.

Progress: async subprocess reading lines into a shared ring buffer, exposed via
installer_status() for the UI (`/api/install/status`); optional per-line emit.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

log = logging.getLogger("winbrow.installer")

_WIN_FLAGS = "--silent --accept-package-agreements --accept-source-agreements"


def winget_available() -> bool:
    return shutil.which("winget") is not None


# --------------------------------------------------------------------------- progress store

@dataclass
class InstallJob:
    job_id: str
    kind: str = "install"           # install | uninstall | upgrade | search | direct
    package: str = ""
    status: str = "running"         # running | done | failed
    started: float = field(default_factory=time.time)
    lines: List[str] = field(default_factory=list)      # ring buffer (latest last)
    exit_code: Optional[int] = None

    MAX_LINES = 60

    def push(self, text: str) -> None:
        line = text.rstrip()
        if line.strip():
            self.lines.append(line[:240])
            if len(self.lines) > self.MAX_LINES:
                self.lines[:] = self.lines[-self.MAX_LINES:]

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id, "kind": self.kind, "package": self.package,
            "status": self.status, "exit_code": self.exit_code,
            "elapsed_s": round(time.time() - self.started, 1),
            "tail": self.lines[-12:],
        }


_JOBS: Dict[str, InstallJob] = {}


def installer_status(job_id: Optional[str] = None) -> dict:
    if job_id:
        j = _JOBS.get(job_id)
        return (j.to_dict() if j else {"error": "unknown job"})
    return {k: v.to_dict() for k, v in _JOBS.items()}


def _meta_repair(line: str) -> str:
    # winget renders progress bars with \b and control chars; strip them
    return line.replace("\x08", "").rstrip()


# --------------------------------------------------------------------------- core run

async def _run_winget(args: List[str], job: InstallJob,
                      timeout: float = 600.0) -> dict:
    coro_cmd = ["winget", *args]
    job.push("$ " + " ".join(coro_cmd))
    try:
        proc = await asyncio.create_subprocess_exec(
            *coro_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (NotImplementedError, Exception) as e:
        job.status = "failed"
        job.push(f"spawn error: {e}")
        return {"success": False, "output": f"Could not start winget: {e}"}
    try:
        while True:
            chunk = await asyncio.wait_for(proc.stdout.read(256), timeout=timeout)
            if not chunk:
                break
            for line in _meta_repair(chunk.decode("utf-8", "replace")).splitlines():
                job.push(line)
        rc = await proc.wait()
    except asyncio.TimeoutError:
        proc.kill()
        job.status = "failed"
        job.push(f"Timed out after {int(timeout)}s")
        return {"success": False, "output": f"winget timed out after {int(timeout)}s", "elapsed": time.time() - job.started}
    job.exit_code = rc
    ok = rc == 0
    job.status = "done" if ok else "failed"
    return {"success": ok, "output": "\n".join(job.lines[-6:]) or f"winget exit {rc}"}


# --------------------------------------------------------------------------- ops

async def winget_search(query: str, timeout: float = 45.0) -> dict:
    """winget search <query> → id/name/version rows (read-only)."""
    if not winget_available():
        return {"success": False, "output": "winget not on PATH — install App Installer or use direct download"}
    job = InstallJob(job_id=uuid.uuid4().hex[:10], kind="search", package=query)
    _JOBS[job.job_id] = job
    res = await _run_winget(["search", query, "--source", "winget"], job, timeout=timeout)
    return {**res, "job_id": job.job_id}


async def winget_install(package: str, job_id: Optional[str] = None,
                         timeout: float = 600.0) -> dict:
    """winget install --id/-query <pkg> --silent (T3 — caller confirms first)."""
    if not package or not package.strip():
        return {"success": False, "output": "Missing package"}
    if not winget_available():
        return {"success": False, "output": "winget not on PATH — install App Installer from the Store"}
    job = InstallJob(job_id=job_id or uuid.uuid4().hex[:10],
                     kind="install", package=package)
    _JOBS[job.job_id] = job
    res = await _run_winget(["install", package, *_WIN_FLAGS.split()], job, timeout=timeout)
    additional = ""
    if not res["success"]:
        additional = " Try 'search <name>' to confirm the package id, or a different source."
    return {**res, "job_id": job.job_id, "suggestion": additional}


async def winget_uninstall(package: str, timeout: float = 300.0) -> dict:
    if not winget_available():
        return {"success": False, "output": "winget not on PATH"}
    job = InstallJob(job_id=uuid.uuid4().hex[:10], kind="uninstall", package=package)
    _JOBS[job.job_id] = job
    res = await _run_winget(["uninstall", package, "--silent"], job, timeout=timeout)
    return {**res, "job_id": job.job_id}


async def winget_upgrade_all(timeout: float = 900.0) -> dict:
    if not winget_available():
        return {"success": False, "output": "winget not on PATH"}
    job = InstallJob(job_id=uuid.uuid4().hex[:10], kind="upgrade")
    _JOBS[job.job_id] = job
    res = await _run_winget(["upgrade", "--all", *_WIN_FLAGS.split()], job, timeout=timeout)
    return {**res, "job_id": job.job_id}


# --------------------------------------------------------------------------- direct download (T3)

async def direct_download_install(url: str, silent_args: str = "",
                                  timeout: float = 600.0) -> dict:
    """Download an installer to temp and run it. Caller must have confirmed (T3)."""
    low = (url or "").lower()
    if not (low.startswith("https://") or low.startswith("http://")):
        return {"success": False, "output": "Installer URL must be http(s)"}
    job = InstallJob(job_id=uuid.uuid4().hex[:10], kind="direct", package=url)
    _JOBS[job.job_id] = job

    from .windows import run_powershell
    dest = str(Path(os.environ.get("TEMP", os.getcwd())) / f"winbrow_{uuid.uuid4().hex[:8]}")
    # policy-check: explicit download + run, always visible
    script = (
        "$ErrorActionPreference = 'Stop';"
        f" New-Item -ItemType Directory -Force -Path '{dest}' | Out-Null;"
        f" $file = Join-Path '{dest}' 'installer.bin';"
        f" Invoke-WebRequest -Uri '{url}' -OutFile $file -UseBasicParsing;"
        " Write-Output ('DOWNLOADED ' + $file);"
    )
    job.push("$ download " + url)
    res = await run_powershell(script, timeout=timeout)
    if not res.get("success") or "DOWNLOADED" not in res.get("stdout", ""):
        job.status = "failed"
        job.push("download failed: " + (res.get("stderr", "")[:100]))
        return {"success": False, "output": "Download failed — suggest trying winget or a different URL", "job_id": job.job_id}
    file_path = res["stdout"].strip().replace("DOWNLOADED ", "")
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".msi":
        args = f"/i \"{file_path}\" /qn {silent_args}"
        run_cmd = f"Start-Process msiexec.exe -ArgumentList '{args}' -Wait -PassThru"
    elif ext == ".exe":
        args = (silent_args or "/quiet /norestart").replace("'", "''")
        run_cmd = f"Start-Process -FilePath '{file_path}' -ArgumentList '{args}' -Wait -PassThru"
    else:
        job.status = "failed"
        return {"success": False, "output": f"Unsupported installer type {ext}", "job_id": job.job_id}
    job.push("$ run installer")
    res2 = await run_powershell(run_cmd + "; Write-Output ('EXIT ' + $p.ExitCode)", timeout=timeout)
    out = res2.get("stdout", "")
    try:
        rc = int(out.strip().split("EXIT ")[-1])
    except (ValueError, IndexError):
        rc = 1
    job.exit_code = rc
    job.status = "done" if rc == 0 else "failed"
    job.push(f"installer exit {rc}" + (" (UAC may still be pending)" if rc != 0 else ""))
    return {
        "success": rc == 0,
        "output": (f"Install complete (exit {rc})" if rc == 0
                   else f"Installer exited {rc} — needs UAC or manual confirmation"),
        "job_id": job.job_id,
    }


# --------------------------------------------------------------------------- registry ops (impl=installer)

INSTALLER_IMPL = "installer"


async def op_search(args: dict) -> dict:
    q = str(args.get("package", "")).strip()
    if not q:
        return {"success": False, "output": "Missing 'package' to search"}
    return await winget_search(q)


async def op_install(args: dict) -> dict:
    pkg = str(args.get("package", "")).strip()
    if not pkg:
        return {"success": False, "output": "Missing 'package'"}
    return await winget_install(pkg)


async def op_uninstall(args: dict) -> dict:
    pkg = str(args.get("package", "")).strip()
    if not pkg:
        return {"success": False, "output": "Missing 'package'"}
    return await winget_uninstall(pkg)


async def op_upgrade_all(args: dict) -> dict:
    return await winget_upgrade_all()


INSTALL_OPS = {
    "app_search": op_search,
    "app_install": op_install,
    "app_uninstall": op_uninstall,
    "app_upgrade_all": op_upgrade_all,
}


def get_installer_tools():
    """Registry Tool records (impl=installer) — tiered T0/T3 in permissions."""
    from .registry import ArgSpec, Tool
    pkg_arg = ArgSpec(name="package", kind="text",
                      instructions="Package name or winget id (e.g. 7zip.7zip)")
    return [
        Tool(
            name="app_search",
            description="Search winget packages by name (read-only).",
            script="", impl=INSTALLER_IMPL,
            args=[pkg_arg],
            examples=["search for 7zip", "find spotify package"],
        ),
        Tool(
            name="app_install",
            description="Install a package with winget (silent). Needs approval.",
            script="", impl=INSTALLER_IMPL,
            args=[pkg_arg],
            examples=["install 7zip", "install spotify silent"],
        ),
        Tool(
            name="app_uninstall",
            description="Uninstall a package with winget. Needs approval.",
            script="", impl=INSTALLER_IMPL,
            args=[pkg_arg],
            examples=["uninstall spotify", "remove 7zip"],
        ),
        Tool(
            name="app_upgrade_all",
            description="Upgrade all winget-managed packages. Needs approval.",
            script="", impl=INSTALLER_IMPL,
            examples=["update all my apps", "upgrade everything"],
        ),
    ]
