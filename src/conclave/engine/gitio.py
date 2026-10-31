"""Async git and shell helpers used by the worktree manager and orchestrator."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("conclave.engine.gitio")

def _build_win_subprocess_kw() -> dict[str, Any]:
    # sys.platform (not os.name) so mypy skips the Windows-only branch on POSIX.
    if sys.platform != "win32":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
    si.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        "startupinfo": si,
    }


_WIN_SUBPROCESS_KW: dict[str, Any] = _build_win_subprocess_kw()

GIT_ENV = {
    # Deterministic, non-interactive git for unattended operation.
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "Conclave",
    "GIT_AUTHOR_EMAIL": "conclave@localhost",
    "GIT_COMMITTER_NAME": "Conclave",
    "GIT_COMMITTER_EMAIL": "conclave@localhost",
}


def _find_windows_posix_sh() -> tuple[str | None, str | None]:
    """Locate Git for Windows' POSIX ``sh.exe`` and its ``usr/bin`` directory."""
    if os.name != "nt":
        return None, None
    git_bin = shutil.which("git")
    if git_bin:
        git_root = Path(git_bin).resolve().parent.parent
        usr_bin = git_root / "usr" / "bin"
        usr_sh = usr_bin / "sh.exe"
        if usr_sh.is_file():
            return str(usr_sh), str(usr_bin)
        sh_candidate = git_root / "bin" / "sh.exe"
        if sh_candidate.is_file():
            extra_path = str(usr_bin) if usr_bin.is_dir() else str(sh_candidate.parent)
            return str(sh_candidate), extra_path
    sh_bin = shutil.which("sh")
    return (sh_bin, None) if sh_bin else (None, None)


async def run_git(
    cwd: Path, *args: str, env: dict[str, str] | None = None
) -> tuple[int, str]:
    """Run a git command in ``cwd``; return (exit_code, combined_output)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **GIT_ENV, **(env or {})},
        **_WIN_SUBPROCESS_KW,
    )
    stdout, _ = await proc.communicate()
    return proc.returncode or 0, stdout.decode("utf-8", errors="replace")


async def run_shell(
    cwd: Path,
    command: str,
    *,
    env: dict[str, str] | None = None,
    timeout_seconds: int | None = None,
) -> tuple[int, str]:
    """Run a shell command (e.g. the project's test command) in ``cwd``.

    On timeout the whole process group is killed so that child processes spawned
    by the shell (pipelines, subshells, background jobs) are not orphaned.
    """
    # start_new_session=True gives the shell its own process group (pgid == pid).
    # When the timeout fires we can killpg the entire group rather than just the
    # shell leader, which prevents orphaned grandchildren from outliving the parent.
    merged_env = {**os.environ, **env} if env else dict(os.environ)
    win_sh, extra_path = _find_windows_posix_sh()
    if win_sh is not None and extra_path:
        merged_env["PATH"] = f"{extra_path}{os.pathsep}{merged_env.get('PATH', '')}"

    venv_dirs: list[str] = []
    for candidate in (
        cwd / ".venv" / "Scripts",
        cwd / ".venv" / "bin",
        Path(sys.executable).parent if sys.executable else None,
    ):
        if candidate is not None and candidate.is_dir():
            cand_str = str(candidate)
            if cand_str not in venv_dirs:
                venv_dirs.append(cand_str)
    if venv_dirs:
        existing_path = merged_env.get("PATH") or merged_env.get("Path") or ""
        if "Path" in merged_env:
            merged_env.pop("Path", None)
        prefix = os.pathsep.join(venv_dirs)
        merged_env["PATH"] = f"{prefix}{os.pathsep}{existing_path}" if existing_path else prefix

    if win_sh is not None:
        proc = await asyncio.create_subprocess_exec(
            win_sh,
            "-c",
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=merged_env,
            start_new_session=True,
            **_WIN_SUBPROCESS_KW,
        )
    else:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=merged_env,
            start_new_session=True,
            **_WIN_SUBPROCESS_KW,
        )
    if timeout_seconds is None:
        stdout, _ = await proc.communicate()
        return proc.returncode or 0, stdout.decode("utf-8", errors="replace")
    # Shield communicate() so a timeout does not *cancel* it — cancelling mid-read
    # leaves the subprocess pipe transport dangling, which asyncio later GC-warns
    # about ("Exception ignored in BaseSubprocessTransport.__del__"). Instead we let
    # it keep running, kill the process, then await it so the transport closes cleanly.
    comm = asyncio.ensure_future(proc.communicate())
    try:
        stdout, _ = await asyncio.wait_for(asyncio.shield(comm), timeout=timeout_seconds)
    except TimeoutError:
        await _kill_process_group(proc, timeout_seconds)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(comm, timeout=1.0)
        if not comm.done():
            comm.cancel()
        return 124, f"(command timed out after {timeout_seconds}s)"
    return proc.returncode or 0, stdout.decode("utf-8", errors="replace")


# --- Internal helpers ---------------------------------------------------------


async def _kill_process_group(
    proc: asyncio.subprocess.Process, timeout_seconds: int
) -> None:
    """Kill the whole process group of *proc*, falling back to per-process kill.

    When ``start_new_session=True`` was used the process pgid equals its pid.
    We try SIGTERM first for a graceful shutdown, then SIGKILL after a short
    grace period so stubborn children don't linger.
    """
    pid = proc.pid
    if pid is None:
        return  # already reaped — nothing to do

    if os.name == "nt" and isinstance(proc, asyncio.subprocess.Process):
        with contextlib.suppress(Exception):
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                **_WIN_SUBPROCESS_KW,
            )
            await killer.wait()

    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
    _signal_group(pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except TimeoutError:
        _signal_group(pid, sigkill)
        await proc.wait()


def _signal_group(pgid: int, sig: int) -> None:
    """Send *sig* to process group *pgid*, guarding for already-exited groups."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(pgid, sig)
        else:
            os.kill(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError, SystemError):
        # The group (or its leader) already exited between our check and the
        # signal — nothing left to kill.
        logger.debug("process group %d already exited", pgid)
