"""Async git and shell helpers used by the worktree manager and orchestrator."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

logger = logging.getLogger("conclave.engine.gitio")

GIT_ENV = {
    # Deterministic, non-interactive git for unattended operation.
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "Conclave",
    "GIT_AUTHOR_EMAIL": "conclave@localhost",
    "GIT_COMMITTER_NAME": "Conclave",
    "GIT_COMMITTER_EMAIL": "conclave@localhost",
}


async def run_git(cwd: Path, *args: str) -> tuple[int, str]:
    """Run a git command in ``cwd``; return (exit_code, combined_output)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **GIT_ENV},
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
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **env} if env else None,
        start_new_session=True,
    )
    if timeout_seconds is None:
        stdout, _ = await proc.communicate()
        return proc.returncode or 0, stdout.decode("utf-8", errors="replace")
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        await _kill_process_group(proc, timeout_seconds)
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

    _signal_group(pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except TimeoutError:
        _signal_group(pid, signal.SIGKILL)
        await proc.wait()


def _signal_group(pgid: int, sig: int) -> None:
    """Send *sig* to process group *pgid*, guarding for already-exited groups."""
    try:
        if hasattr(os, "killpg"):
            os.killpg(pgid, sig)
        else:
            os.kill(pgid, sig)
    except ProcessLookupError:
        # The group (or its leader) already exited between our check and the
        # signal — nothing left to kill.
        logger.debug("process group %d already exited", pgid)
