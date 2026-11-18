"""Hidden acceptance tests for W18 (the workflow evolver respects a rejected proposal).

Written by the pilot from the spec alone, run against the task branch before merging; not part
of the repository's own suite. Measured live: the same "make risk optional" proposal came back
after every clean task although the owner rejected it each time.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest_asyncio
from fake_provider import FakeProvider
from httpx import ASGITransport

from conclave.db import Database
from conclave.db import repositories as repo
from conclave.mcp.server import handle_mcp_message
from conclave.runtime import Daemon
from conclave.web import create_app


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "conclave.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest_asyncio.fixture
async def daemon(db: Database, tmp_path: Path) -> AsyncIterator[Daemon]:
    instance = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    try:
        yield instance
    finally:
        await instance.shutdown()


@pytest_asyncio.fixture
async def client(daemon: Daemon) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(daemon, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _mcp(daemon: Daemon, name: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
    response = await handle_mcp_message(daemon, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert response is not None
    result = response["result"]
    text = result["content"][0]["text"]
    return (True, text) if result["isError"] else (False, json.loads(text))


async def _evolve(client: httpx.AsyncClient, pid: str) -> dict[str, Any]:
    resp = await client.post(f"/api/projects/{pid}/workflow/evolve", json={"task_failed": True})
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    assert body["mode"] == "suggest"
    return body


async def _proposals(client: httpx.AsyncClient, pid: str) -> list[dict[str, Any]]:
    resp = await client.get(f"/api/projects/{pid}/workflow/proposals")
    assert resp.status_code == 200, resp.text
    body: list[dict[str, Any]] = resp.json()
    return body


async def _proposed_events(db: Database, pid: str) -> int:
    events = await repo.list_events(db, project_id=pid, limit=5000)
    return sum(1 for e in events if e.type == "workflow.evolution_proposed")


async def _reject(client: httpx.AsyncClient, pid: str, proposal_id: str) -> None:
    resp = await client.post(
        f"/api/projects/{pid}/workflow/proposals/{proposal_id}/reject",
        json={"reason": "keep the workflow"},
    )
    assert resp.status_code == 200, resp.text


async def _project(db: Database, tmp_path: Path) -> str:
    project = await repo.create_project(
        db, name="p", path=str(tmp_path / "p"), default_branch="main"
    )
    return project.id


async def test_a_rejected_proposal_is_not_proposed_again(
    db: Database, tmp_path: Path, client: httpx.AsyncClient
) -> None:
    pid = await _project(db, tmp_path)
    first = await _evolve(client, pid)
    assert first["proposal"]["state"] == "pending"
    assert first.get("suppressed_by") is None
    await _reject(client, pid, first["proposal"]["id"])
    events_before = await _proposed_events(db, pid)

    again = await _evolve(client, pid)

    assert again["proposal"] is None
    assert again["suppressed_by"] == first["proposal"]["id"]
    assert [p["state"] for p in await _proposals(client, pid)] == ["rejected"]
    assert await _proposed_events(db, pid) == events_before


async def test_the_mcp_evolve_reports_the_suppression_too(
    db: Database, tmp_path: Path, client: httpx.AsyncClient, daemon: Daemon
) -> None:
    pid = await _project(db, tmp_path)
    first = await _evolve(client, pid)
    await _reject(client, pid, first["proposal"]["id"])

    is_error, body = await _mcp(
        daemon, "conclave_evolve_workflow", {"project_id": pid, "task_failed": True}
    )

    assert not is_error, body
    assert body["suppressed_by"] == first["proposal"]["id"]
    assert [p["state"] for p in await _proposals(client, pid)] == ["rejected"]


async def test_after_the_workflow_changes_the_evolver_proposes_again(
    db: Database, tmp_path: Path, client: httpx.AsyncClient, daemon: Daemon
) -> None:
    pid = await _project(db, tmp_path)
    first = await _evolve(client, pid)
    await _reject(client, pid, first["proposal"]["id"])
    is_error, body = await _mcp(daemon, "conclave_apply_workflow_template",
                                {"project_id": pid, "template_id": "deep_audit"})
    assert not is_error, body

    again = await _evolve(client, pid)

    assert again.get("suppressed_by") is None
    assert again["proposal"] is not None and again["proposal"]["state"] == "pending"
    assert again["proposal"]["id"] != first["proposal"]["id"]


async def test_a_pending_duplicate_is_still_deduplicated(
    db: Database, tmp_path: Path, client: httpx.AsyncClient
) -> None:
    pid = await _project(db, tmp_path)
    first = await _evolve(client, pid)
    second = await _evolve(client, pid)
    assert second["proposal"]["id"] == first["proposal"]["id"]
    assert len(await _proposals(client, pid)) == 1


async def test_concurrent_evolutions_after_a_rejection_store_nothing(
    db: Database, tmp_path: Path, client: httpx.AsyncClient
) -> None:
    pid = await _project(db, tmp_path)
    first = await _evolve(client, pid)
    await _reject(client, pid, first["proposal"]["id"])

    results = await asyncio.gather(*(_evolve(client, pid) for _ in range(4)))

    assert all(r["proposal"] is None for r in results)
    assert [p["state"] for p in await _proposals(client, pid)] == ["rejected"]
