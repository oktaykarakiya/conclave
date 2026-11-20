"""Interaction log retention: the daemon-wide ``system.interaction_log_retention_days``.

Logs last updated before the cutoff are deleted at daemon start and on every pass of the hourly
loop, in bounded batches; 0 keeps them all, a project's value is ignored, and the ``usage`` table
(cost accounting) is never touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from conclave import runtime
from conclave.config import save_global_config
from conclave.db import Database
from conclave.db import repositories as repo
from conclave.mcp.tools import call_mcp_tool
from conclave.providers import AgentResult, OnChunk, Provider, ResolvedProfile
from conclave.runtime import Daemon, prune_interaction_logs
from conclave.web import create_app
from waits import wait_until

_DAY_MS = 86_400_000


class _StubProvider(Provider):
    name = "stub"

    async def run_agent(
        self,
        *,
        profile: ResolvedProfile,
        prompt: str,
        timeout_seconds: int,
        cwd: Path | None = None,
        on_chunk: OnChunk | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AgentResult:
        return AgentResult(ok=True, text="ok")


def _days_ago(days: float) -> int:
    return int(time.time() * 1000) - int(days * _DAY_MS)


async def _log(db: Database, session_id: str, *, days_old: float) -> None:
    """One interaction log last updated *days_old* days ago (a plain INSERT, so a concurrent
    retention pass cannot race a read-back)."""
    ts = _days_ago(days_old)
    await db.execute(
        "INSERT INTO interaction_logs(session_id, time_created, time_updated) VALUES (?, ?, ?)",
        (session_id, ts, ts),
    )


async def _session_ids(db: Database) -> set[str]:
    rows = await db.fetchall("SELECT session_id FROM interaction_logs")
    return {str(r["session_id"]) for r in rows}


async def test_daemon_start_deletes_the_logs_older_than_the_default_90_days(
    db: Database, tmp_path: Path
) -> None:
    await _log(db, "100-days", days_old=100)
    await _log(db, "89-days", days_old=89)
    await _log(db, "1-day", days_old=1)
    for agent in ("developer", "reviewer"):
        await repo.add_usage(db, agent=agent, cost_usd=0.5, input_tokens=10)
    # As old as the deleted log, and still kept: cost accounting is not pruned.
    await db.execute("UPDATE usage SET ts = ?", ("2000-01-01T00:00:00+00:00",))
    usage_before = [tuple(r) for r in await db.fetchall("SELECT * FROM usage ORDER BY id")]

    daemon = Daemon(db, tmp_path / "home", _StubProvider())
    try:
        await daemon.start()
    finally:
        await daemon.shutdown()

    assert await _session_ids(db) == {"89-days", "1-day"}
    assert len(usage_before) == 2
    assert [tuple(r) for r in await db.fetchall("SELECT * FROM usage ORDER BY id")] == usage_before


async def test_every_pass_of_the_retention_loop_deletes_the_expired_logs(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "INTERACTION_LOG_RETENTION_INTERVAL_SECONDS", 0.01)
    daemon = Daemon(db, tmp_path / "home", _StubProvider(), workers_enabled=False)
    await _log(db, "recent", days_old=1)

    loop_task = asyncio.create_task(daemon._interaction_log_retention_loop())
    try:
        for expired in ("expired-1", "expired-2"):  # two passes, not just one

            async def deleted(expired: str = expired) -> bool:
                return expired not in await _session_ids(db)

            await _log(db, expired, days_old=91)
            await wait_until(deleted, f"the retention loop never deleted {expired}")
            assert await _session_ids(db) == {"recent"}
    finally:
        loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await loop_task


async def test_zero_keeps_every_log_and_a_project_value_is_ignored(
    db: Database, tmp_path: Path
) -> None:
    await _log(db, "10-years", days_old=3650)
    await _log(db, "10-days", days_old=10)

    # Daemon-wide: a project's value is stored but ignored, so the global 90 days apply.
    await repo.create_project(
        db,
        name="p",
        path=str(tmp_path),
        default_branch="main",
        config={"system": {"interaction_log_retention_days": 1}},
    )
    assert await prune_interaction_logs(db) == 1
    assert await _session_ids(db) == {"10-days"}

    await _log(db, "10-years", days_old=3650)
    await save_global_config(db, {"system": {"interaction_log_retention_days": 0}})
    assert await prune_interaction_logs(db) == 0
    assert await _session_ids(db) == {"10-years", "10-days"}

    # Read on every pass: a lower global value applies without a restart.
    await save_global_config(db, {"system": {"interaction_log_retention_days": 5}})
    assert await prune_interaction_logs(db) == 2
    assert await _session_ids(db) == set()


async def test_the_delete_runs_in_bounded_batches_until_none_is_left(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    expired, recent = _days_ago(200), _days_ago(1)
    async with db.transaction() as conn:
        await conn.executemany(
            "INSERT INTO interaction_logs(session_id, time_created, time_updated) "
            "VALUES (?, ?, ?)",
            [(f"old-{i}", expired, expired) for i in range(2_500)]
            + [(f"new-{i}", recent, recent) for i in range(10)],
        )
    statements = 0
    write = db._write

    @contextlib.asynccontextmanager
    async def _counting_write() -> AsyncIterator[Any]:
        nonlocal statements
        statements += 1
        async with write() as conn:
            yield conn

    monkeypatch.setattr(db, "_write", _counting_write)

    deleted = await repo.delete_interaction_logs_before(db, _days_ago(90), batch_size=1_000)

    assert deleted == 2_500
    assert statements == 3  # 1,000 + 1,000 + 500: the write lock is released between them
    remaining = await db.fetchall("SELECT session_id FROM interaction_logs")
    assert sorted(str(r["session_id"]) for r in remaining) == sorted(f"new-{i}" for i in range(10))


async def test_a_failing_retention_pass_does_not_stop_the_daemon_start(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _fail(_db: Database) -> int:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(runtime, "prune_interaction_logs", _fail)
    project = await repo.create_project(db, name="p", path=str(tmp_path), default_branch="main")
    daemon = Daemon(db, tmp_path / "home", _StubProvider())
    try:
        with caplog.at_level(logging.WARNING, logger="conclave.runtime"):
            await daemon.start()
        assert daemon.worker(project.id) is not None  # the workers still started
    finally:
        await daemon.shutdown()
    assert "interaction log retention pass failed" in caplog.text


async def test_the_retention_is_a_global_setting_over_mcp_and_rest(
    db: Database, tmp_path: Path
) -> None:
    daemon = Daemon(db, tmp_path / "home", _StubProvider(), workers_enabled=False)

    await call_mcp_tool(
        daemon, "conclave_patch_config", {"system": {"interaction_log_retention_days": 30}}
    )
    glob = await call_mcp_tool(daemon, "conclave_get_config", {})
    assert glob["system"]["interaction_log_retention_days"] == 30

    app = create_app(daemon, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        res = await c.patch(
            "/api/config/global",
            json={"config": {"system": {"interaction_log_retention_days": 0}}},
        )
        assert res.status_code == 200
        assert res.json()["global_config"]["system"]["interaction_log_retention_days"] == 0
        got = await c.get("/api/config/global")
    assert got.json()["system"]["interaction_log_retention_days"] == 0
