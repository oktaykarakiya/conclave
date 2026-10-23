"""Stdio MCP entrypoint (`python -m conclave.mcp` or `conclave-mcp`).

Forwards JSON-RPC 2.0 MCP messages over stdin/stdout to the running Conclave daemon
(`CONCLAVE_URL`, default `http://127.0.0.1:8700`), or falls back to an in-process
SQLite session if the HTTP daemon is unreachable.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

from ..bootstrap import seed_global_defaults
from ..db import Database
from ..main import conclave_home, make_provider
from ..runtime import Daemon
from .server import handle_mcp_message


def _forward_http(mcp_url: str, message: dict[str, Any]) -> dict[str, Any] | None:
    data = json.dumps(message).encode("utf-8")
    req = urllib.request.Request(
        mcp_url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30.0) as resp:
        if resp.status == 202 or resp.status == 204:
            return None
        raw = resp.read().decode("utf-8").strip()
        if not raw:
            return None
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None


async def _run_stdio() -> None:
    base_url = os.environ.get("CONCLAVE_URL", "http://127.0.0.1:8700").rstrip("/")
    mcp_url = f"{base_url}/mcp"
    fallback_daemon: Daemon | None = None

    async def _get_fallback_daemon() -> Daemon:
        nonlocal fallback_daemon
        if fallback_daemon is None:
            home = conclave_home()
            db = Database(home / "conclave.db")
            await db.connect()
            await seed_global_defaults(db)
            fallback_daemon = Daemon(db, home, make_provider(), workers_enabled=False)
        return fallback_daemon

    try:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue

            response: dict[str, Any] | None = None
            try:
                response = await asyncio.to_thread(_forward_http, mcp_url, msg)
            except (urllib.error.URLError, TimeoutError, OSError):
                daemon = await _get_fallback_daemon()
                response = await handle_mcp_message(daemon, msg)

            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
    finally:
        if fallback_daemon is not None:
            await fallback_daemon.shutdown()
            await fallback_daemon.db.close()


def main() -> None:
    asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
