"""Conclave daemon entrypoint."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import IO, Any

import uvicorn

from .db import Database
from .providers import EngineRouter, Provider, parse_engine
from .runtime import Daemon
from .web import create_app

fcntl: Any = None
try:
    import fcntl as _fcntl

    fcntl = _fcntl
except ImportError:
    pass


def conclave_home() -> Path:
    override = os.environ.get("CONCLAVE_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "conclave"


def acquire_daemon_lock(home: Path) -> IO[str]:
    """Take an exclusive advisory lock on ``<home>/daemon.lock`` — or die loudly.

    Exactly ONE daemon may run per data dir. Two daemons sharing one SQLite DB
    means two ProjectWorkers claiming from the same queue: observed live as a
    half-shut-down daemon (SIGTERM received, socket released, worker still
    draining) racing a fresh daemon — crash recovery re-approved a task the old
    worker was still running, both processed it, and the late finalizer
    clobbered the winner's state. flock is released by the kernel on ANY
    process exit (including SIGKILL), so a stale lockfile can never brick a
    restart. The returned handle must stay referenced for the daemon's lifetime.
    """
    home.mkdir(parents=True, exist_ok=True)
    lock_path = home / "daemon.lock"
    handle = lock_path.open("w")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        elif sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise BlockingIOError from exc
    except BlockingIOError:
        handle.close()
        print(
            f"[conclave] another daemon already holds {lock_path} — refusing to start.\n"
            "[conclave] stop it first (or wait for its graceful shutdown to finish); "
            "a second daemon on the same data dir would double-claim tasks.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def make_provider() -> Provider:
    """Build the engine router. ``CONCLAVE_ENGINE`` picks the default engine (opencode unless
    set to ``claude``); an agent whose model is ``claude-code/<model>`` always runs on the
    Claude Code CLI, so engines can be mixed per agent."""
    try:
        default = parse_engine(os.environ.get("CONCLAVE_ENGINE"))
    except ValueError as exc:
        print(f"[conclave] error: CONCLAVE_ENGINE: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    return EngineRouter(default)


def cli() -> None:
    home = conclave_home()
    # Held (referenced) for the whole process lifetime; kernel releases it on exit.
    _lock = acquire_daemon_lock(home)
    db = Database(home / "conclave.db")
    daemon = Daemon(db, home, make_provider())
    app = create_app(daemon)
    # Daemon serves the bundled web UI and runs task workers in isolated worktrees.
    # Personal/single-user tool: bind all interfaces by default so the UI is reachable
    # from the LAN (e.g. your phone). Run only on a trusted network — the API is
    # intentionally unauthenticated. Set CONCLAVE_HOST=127.0.0.1 to restrict to loopback.
    host = os.environ.get("CONCLAVE_HOST", "0.0.0.0")
    port = int(os.environ.get("CONCLAVE_PORT", "8700"))
    # Bounded graceful shutdown: a long-lived stream (e.g. an SSE client) must not hold a
    # SIGTERM hostage — after the grace period uvicorn cancels whatever is still running.
    uvicorn.run(app, host=host, port=port, log_level="info", timeout_graceful_shutdown=10)


if __name__ == "__main__":
    cli()
