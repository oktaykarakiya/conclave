"""Unit tests for git/shell helpers (engine/gitio.py)."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from unittest.mock import patch

from conclave.engine.gitio import _kill_process_group, _signal_group, run_shell

# ---------------------------------------------------------------------------
# run_shell — process-group kill on timeout
# ---------------------------------------------------------------------------


async def test_run_shell_kills_process_group_on_timeout(
    tmp_path: Path,
) -> None:
    """A shell that spawns children must leave no orphans after timeout.

    The shell starts a background ``sleep`` child, records its pid to a file
    so we can verify it post-timeout, then waits forever.  With
    ``start_new_session=True`` + ``os.killpg`` the entire group is reaped.
    """
    pid_file = tmp_path / "child.pid"
    # Write the background child's pid to a file so we can check it after
    # the timeout (stdout is lost when communicate() is interrupted).
    script = f"sleep 300 & echo $! > {pid_file}; wait"
    exit_code, output = await run_shell(tmp_path, script, timeout_seconds=1)
    assert exit_code == 124, f"expected exit 124, got {exit_code}; output={output!r}"
    assert "timed out" in output

    # Read the child pid from the file.
    child_pid_str = pid_file.read_text().strip()
    assert child_pid_str, f"child pid file is empty; output={output!r}"
    child_pid = int(child_pid_str)
    await _wait_for_process_gone(child_pid)


async def test_run_shell_no_timeout_does_not_use_session(tmp_path: Path) -> None:
    """Normal path (no timeout) must still work and return clean output."""
    exit_code, output = await run_shell(tmp_path, "echo ok && exit 0")
    assert exit_code == 0
    assert "ok" in output


async def test_run_shell_timeout_graceful_exit(tmp_path: Path) -> None:
    """Command that finishes before timeout — no kill needed."""
    exit_code, output = await run_shell(tmp_path, "echo done", timeout_seconds=5)
    assert exit_code == 0
    assert "done" in output


# ---------------------------------------------------------------------------
# _signal_group — platform guards and error handling
# ---------------------------------------------------------------------------


def test_signal_group_process_lookup_error_does_not_raise() -> None:
    """When the process group has already exited, ProcessLookupError is caught."""
    nonexistent_pid = 99999999
    # Must not raise.
    _signal_group(nonexistent_pid, signal.SIGTERM)


async def test_kill_process_group_handles_none_pid() -> None:
    """When proc.pid is None (already reaped), _kill_process_group is a no-op."""

    class _FakeProc:
        pid = None

        async def wait(self) -> None:
            pass  # pragma: no cover — not reached

    await _kill_process_group(_FakeProc(), 10)  # type: ignore[arg-type]


async def test_kill_process_group_sigterm_then_sigkill() -> None:
    """Verify SIGTERM is tried first, then SIGKILL if the process lingers."""
    hang = asyncio.Event()
    signals_sent: list[int] = []

    # Build a fake process whose wait() hangs the first time (forcing the
    # 2 s grace period to expire and trigger SIGKILL escalation), then
    # returns immediately the second time.
    class _StubbornProc:
        pid = 12345
        _wait_count = 0

        async def wait(self) -> None:
            self._wait_count += 1
            if self._wait_count == 1:
                await hang.wait()

    # Patch os.killpg and os.kill so we can record which signals are sent
    # without actually killing anything.
    def _fake_killpg(pgid: int, sig: int) -> None:
        signals_sent.append(sig)
        if sig == signal.SIGKILL:
            hang.set()

    def _fake_kill(pid: int, sig: int) -> None:
        signals_sent.append(sig)
        if sig == signal.SIGKILL:
            hang.set()

    with patch.object(os, "killpg", _fake_killpg), patch.object(os, "kill", _fake_kill):
        await _kill_process_group(_StubbornProc(), 10)  # type: ignore[arg-type]

    assert signal.SIGTERM in signals_sent, "SIGTERM must be sent first"
    assert signal.SIGKILL in signals_sent, "SIGKILL must follow if process survives SIGTERM"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _wait_for_process_gone(pid: int, deadline_seconds: float = 3.0) -> None:
    """Poll until *pid* no longer exists or *deadline_seconds* elapses."""
    deadline = asyncio.get_event_loop().time() + deadline_seconds
    while asyncio.get_event_loop().time() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"pid {pid} still alive after {deadline_seconds}s")
