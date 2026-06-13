"""FastAPI dependencies."""

from __future__ import annotations

from fastapi import Request

from ..runtime import Daemon


def get_daemon(request: Request) -> Daemon:
    daemon: Daemon = request.app.state.daemon
    return daemon
