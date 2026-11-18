"""Hidden acceptance tests for W16 (interaction-log retention and honest export).

Written by the pilot from the spec alone, run against the task branch before merging; not part
of the repository's own suite.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from pathlib import Path
from typing import Any

import pytest

from conclave.config import ConclaveConfig
from conclave.db import repositories as repo
from conclave.db.database import Database
from conclave.providers.base import AgentResult


@pytest.fixture
async def db(tmp_path: Path) -> Any:
    database = Database(tmp_path / "home" / "conclave.db")
    (tmp_path / "home").mkdir()
    await database.connect()
    yield database
    await database.close()


def test_retention_setting_defaults_and_bounds() -> None:
    from pydantic import ValidationError

    assert ConclaveConfig().system.interaction_log_retention_days == 90
    ConclaveConfig.model_validate({"system": {"interaction_log_retention_days": 0}})
    ConclaveConfig.model_validate({"system": {"interaction_log_retention_days": 3650}})
    for bad in (-1, 3651):
        with pytest.raises(ValidationError):
            ConclaveConfig.model_validate({"system": {"interaction_log_retention_days": bad}})


async def test_logging_a_dispatch_never_exports(db: Database, monkeypatch: Any) -> None:
    from conclave.observability import parquet_sync

    calls: list[object] = []

    async def spy(*a: object, **k: object) -> dict[str, object]:
        calls.append(a)
        return {}

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(parquet_sync, "export_interaction_logs_to_parquet", spy)
    await parquet_sync.log_agent_interaction(
        db, title="t", directory="/x", model="m", agent="developer", prompt="p",
        result=AgentResult(ok=True, text="done"), duration_seconds=1.0,
    )
    assert calls == []


async def test_export_is_complete_honestly_named_and_stays_out_of_cwd(
    db: Database, tmp_path: Path, monkeypatch: Any
) -> None:
    from conclave.observability import parquet_sync

    home = tmp_path / "home"
    work = tmp_path / "cwd"
    work.mkdir()
    (work / "pyproject.toml").write_text("[project]\nname='x'\n")
    monkeypatch.setenv("CONCLAVE_HOME", str(home))
    monkeypatch.chdir(work)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    n = 10_050
    await repo.record_interaction_log(db, session_id="s0", agent="a", model="m")
    now_ms = 1_794_900_000_000
    async with db.transaction() as conn:
        await conn.executemany(
            "INSERT INTO interaction_logs(session_id, agent, model, time_created, time_updated) "
            "VALUES (?, 'a', 'm', ?, ?)",
            [(f"s{i}", now_ms + i, now_ms + i) for i in range(1, n)],
        )

    out = await parquet_sync.export_interaction_logs_to_parquet(db)
    assert out["rows"] == n
    path = Path(out["path"])
    assert path.parent == home
    has_pyarrow = importlib.util.find_spec("pyarrow") is not None
    assert path.suffix == (".parquet" if has_pyarrow else ".jsonl")
    assert path.is_file()
    if not has_pyarrow:
        assert path.read_text().count("\n") >= n - 1
    assert not list(work.glob("INTERACTION_LOGS*")), "never write into the working directory"


# --- item 1: retention at daemon start ------------------------------------------------------

_DAY_MS = 86_400_000


async def _seed_logs(db: Database, retention_days: int, old: int) -> None:
    from conclave.bootstrap import seed_global_defaults
    from conclave.config.resolver import save_global_config

    await seed_global_defaults(db)
    await save_global_config(db, {"system": {"interaction_log_retention_days": retention_days}})
    now_ms = int(time.time() * 1000)
    rows = [(f"old{i}", now_ms - 40 * _DAY_MS) for i in range(old)]
    rows += [(f"new{i}", now_ms - _DAY_MS) for i in range(10)]
    async with db.transaction() as conn:
        await conn.executemany(
            "INSERT INTO interaction_logs(session_id, agent, model, time_created, time_updated) "
            "VALUES (?, 'a', 'm', ?, ?)",
            [(sid, ts, ts) for sid, ts in rows],
        )
        await conn.execute(
            "INSERT INTO usage(id, agent, cost_usd, ts) VALUES ('u-old', 'developer', 1.5, ?)",
            ("2020-01-01T00:00:00+00:00",),
        )


async def _count(db: Database, sql: str) -> int:
    rows = await db.fetchall(sql)
    return int(next(iter(dict(rows[0]).values())))


async def _start_daemon(db: Database, tmp_path: Path, monkeypatch: Any) -> Any:
    from fake_provider import FakeProvider

    from conclave.runtime import Daemon

    # A production start: background housekeeping must not depend on running under pytest.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    # Hermetic: nothing may land in the real home or the current working directory.
    monkeypatch.setenv("CONCLAVE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=True)
    await daemon.start()
    return daemon


async def test_old_rows_are_pruned_at_start_in_batches_and_usage_is_kept(
    db: Database, tmp_path: Path, monkeypatch: Any
) -> None:
    await _seed_logs(db, retention_days=30, old=2_500)  # more than two 1,000-row batches
    daemon = await _start_daemon(db, tmp_path, monkeypatch)
    try:
        old_left = -1
        for _ in range(100):  # up to 10 s for a background pass
            old_left = await _count(
                db, "SELECT COUNT(*) FROM interaction_logs WHERE session_id LIKE 'old%'"
            )
            if old_left == 0:
                break
            await asyncio.sleep(0.1)
        assert old_left == 0, "rows older than the retention survived the daemon start"
        assert await _count(
            db, "SELECT COUNT(*) FROM interaction_logs WHERE session_id LIKE 'new%'"
        ) == 10
        assert await _count(db, "SELECT COUNT(*) FROM usage WHERE id = 'u-old'") == 1
    finally:
        await daemon.shutdown()


async def test_zero_retention_keeps_everything(
    db: Database, tmp_path: Path, monkeypatch: Any
) -> None:
    await _seed_logs(db, retention_days=0, old=50)
    daemon = await _start_daemon(db, tmp_path, monkeypatch)
    try:
        await asyncio.sleep(1.5)
        assert await _count(db, "SELECT COUNT(*) FROM interaction_logs") == 60
    finally:
        await daemon.shutdown()


async def test_out_of_bounds_retention_is_a_4xx_over_rest_and_an_error_over_mcp(
    db: Database, tmp_path: Path
) -> None:
    import httpx
    from fake_provider import FakeProvider
    from httpx import ASGITransport

    from conclave.bootstrap import seed_global_defaults
    from conclave.runtime import Daemon
    from conclave.web import create_app

    await seed_global_defaults(db)
    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    try:
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            for bad in (-1, 3651):
                res = await c.patch(
                    "/api/config/global",
                    json={"config": {"system": {"interaction_log_retention_days": bad}}},
                )
                assert 400 <= res.status_code < 500, (bad, res.status_code, res.text)
                rpc = await c.post("/mcp", json={
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "conclave_patch_config", "arguments": {
                        "config": {"system": {"interaction_log_retention_days": bad}}}},
                })
                assert rpc.json()["result"]["isError"] is True, rpc.text
            schema = (await c.get("/api/config/schema")).json()
            assert "interaction_log_retention_days" in json.dumps(schema)
            ok = await c.patch(
                "/api/config/global",
                json={"config": {"system": {"interaction_log_retention_days": 30}}},
            )
            assert ok.status_code == 200, ok.text
    finally:
        await daemon.shutdown()
