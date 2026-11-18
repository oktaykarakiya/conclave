"""Hidden acceptance tests for W14 (MCP stdio bridge with the daemon unreachable, GET /mcp).

Written by the pilot from the spec alone, run against the task branch before merging; not part
of the repository's own suite. Black-box: runs ``python -m conclave.mcp`` as a subprocess.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest_asyncio
from fake_provider import FakeProvider
from httpx import ASGITransport

import conclave
from conclave.db import Database
from conclave.runtime import Daemon
from conclave.web import create_app


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def _bridge(tmp_path: Path, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    home = tmp_path / "home"
    env = {
        **os.environ,
        "CONCLAVE_URL": f"http://127.0.0.1:{_closed_port()}",
        "CONCLAVE_HOME": str(home),
        # The conclave package under test (a snapshot src when run against a task branch).
        "PYTHONPATH": str(Path(conclave.__file__).resolve().parents[1]),
    }
    env.pop("PYTEST_CURRENT_TEST", None)
    proc = subprocess.run(
        [sys.executable, "-m", "conclave.mcp"],
        input="\n".join(json.dumps(m) for m in messages) + "\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def test_unreachable_daemon_lists_tools_but_refuses_calls_without_touching_the_db(
    tmp_path: Path,
) -> None:
    out = _bridge(
        tmp_path,
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "t", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "conclave_list_projects", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
             "params": {"name": "conclave_patch_config",
                        "arguments": {"config": {"execution": {"auto_merge": True}}}}},
        ],
    )
    by_id = {m.get("id"): m for m in out}
    assert None not in by_id, "a notification must not get a response"
    assert "result" in by_id[1], by_id[1]
    tools = by_id[2]["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"conclave_list_projects", "conclave_merge_task", "conclave_get_pilot_guide"} <= names
    assert len(tools) >= 60
    for call_id in (3, 4):
        result = by_id[call_id].get("result")
        assert result is not None and result.get("isError") is True, by_id[call_id]
        text = " ".join(c.get("text", "") for c in result.get("content", []))
        assert "unreachable" in text.lower()
    home = tmp_path / "home"
    assert not (home / "conclave.db").exists(), "the bridge must never open the database"
    if home.exists():
        assert not list(home.glob("*.db*")), list(home.iterdir())


# --- item 4: GET /mcp is 405, not a 200 the client re-polls every second ---------------------


@pytest_asyncio.fixture
async def client(tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    db = Database(tmp_path / "conclave.db")
    await db.connect()
    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c
    finally:
        await daemon.shutdown()
        await db.close()


async def test_get_mcp_is_method_not_allowed_for_a_streaming_client(
    client: httpx.AsyncClient,
) -> None:
    # Measured live: Claude Code's MCP client opened GET /mcp once per second for two days
    # (392,577 requests), because a 200 JSON body reads as an SSE stream that ended at once.
    res = await client.get("/mcp", headers={"Accept": "text/event-stream"})
    assert res.status_code == 405
    assert "POST" in res.headers.get("allow", "")


async def test_get_mcp_is_method_not_allowed_without_an_accept_header_too(
    client: httpx.AsyncClient,
) -> None:
    res = await client.get("/mcp")
    assert res.status_code == 405
    assert "POST" in res.headers.get("allow", "")


async def test_the_metadata_stays_at_api_mcp_info_and_post_still_works(
    client: httpx.AsyncClient,
) -> None:
    info = await client.get("/api/mcp/info")
    assert info.status_code == 200 and info.json()["tool_count"] > 0
    res = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert res.status_code == 200 and res.json()["id"] == 1
