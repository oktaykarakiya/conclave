"""Unit tests for the daemon entrypoint (main.py) — the single-daemon lock."""

from __future__ import annotations

from pathlib import Path

import pytest

from conclave.main import acquire_daemon_lock


def test_daemon_lock_is_exclusive(tmp_path: Path) -> None:
    """A second acquire on the same home must refuse loudly (SystemExit, code 1).

    Two daemons on one data dir double-claim tasks from the shared queue — the
    lock makes the second start fail fast instead.
    """
    first = acquire_daemon_lock(tmp_path)
    try:
        with pytest.raises(SystemExit) as exc:
            acquire_daemon_lock(tmp_path)
        assert exc.value.code == 1
    finally:
        first.close()


def test_daemon_lock_released_on_close(tmp_path: Path) -> None:
    """Once the holder exits (handle closed), a new daemon can acquire the lock.

    flock is kernel-released on any process exit — including SIGKILL — so a
    stale lockfile can never brick a restart; closing the handle models that.
    """
    first = acquire_daemon_lock(tmp_path)
    first.close()
    second = acquire_daemon_lock(tmp_path)
    try:
        assert (tmp_path / "daemon.lock").exists()
    finally:
        second.close()


def test_daemon_lock_creates_home_dir(tmp_path: Path) -> None:
    """The lock helper bootstraps a missing data dir instead of crashing."""
    home = tmp_path / "nested" / "conclave-home"
    handle = acquire_daemon_lock(home)
    try:
        assert home.is_dir()
        assert (home / "daemon.lock").exists()
    finally:
        handle.close()
