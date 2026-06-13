"""Async git and shell helpers used by the worktree manager and orchestrator."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

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
    """Run a shell command (e.g. the project's test command) in ``cwd``."""
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env={**os.environ, **env} if env else None,
    )
    if timeout_seconds is None:
        stdout, _ = await proc.communicate()
        return proc.returncode or 0, stdout.decode("utf-8", errors="replace")
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"(command timed out after {timeout_seconds}s)"
    return proc.returncode or 0, stdout.decode("utf-8", errors="replace")
