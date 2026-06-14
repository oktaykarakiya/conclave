"""Conclave daemon entrypoint."""

from __future__ import annotations

import os
from pathlib import Path

import uvicorn

from .db import Database
from .providers import ClaudeCliProvider
from .runtime import Daemon
from .web import create_app


def conclave_home() -> Path:
    override = os.environ.get("CONCLAVE_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "conclave"


def cli() -> None:
    home = conclave_home()
    db = Database(home / "conclave.db")
    daemon = Daemon(db, home, ClaudeCliProvider())
    app = create_app(daemon)
    # Personal/single-user tool: bind all interfaces by default so the UI is reachable
    # from the LAN (e.g. your phone). Run only on a trusted network — the API is
    # intentionally unauthenticated. Set CONCLAVE_HOST=127.0.0.1 to restrict to loopback.
    host = os.environ.get("CONCLAVE_HOST", "0.0.0.0")
    port = int(os.environ.get("CONCLAVE_PORT", "8700"))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    cli()
