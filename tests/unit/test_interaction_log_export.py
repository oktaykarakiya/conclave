"""Interaction log export: only on request, every row, a file named after its format, written
into the daemon's home only, and converted off the event loop."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from conclave.db import Database
from conclave.db import repositories as repo
from conclave.mcp.tools import call_mcp_tool
from conclave.observability import parquet_sync
from conclave.observability.parquet_sync import (
    export_interaction_logs_to_parquet,
    log_agent_interaction,
)
from conclave.providers import AgentResult, OnChunk, Provider, ResolvedProfile
from conclave.runtime import Daemon
from conclave.web import create_app

# The 23 archive columns, in order.
_COLUMNS = [
    "session_id",
    "title",
    "directory",
    "path",
    "slug",
    "model",
    "agent",
    "cost",
    "tokens_input",
    "tokens_output",
    "tokens_reasoning",
    "tokens_cache_read",
    "tokens_cache_write",
    "time_created",
    "time_updated",
    "created_at_str",
    "updated_at_str",
    "duration",
    "tool_calls_count",
    "message_count",
    "user_prompt",
    "tool_breakdown",
    "messages",
]
_MANY = 10_500  # more than the 10,000 rows the export used to stop at


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


@pytest.fixture
def no_pyarrow(monkeypatch: pytest.MonkeyPatch) -> None:
    """``import pyarrow`` fails, as on a host without the observability extra."""
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    monkeypatch.setitem(sys.modules, "pyarrow.parquet", None)


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The working directory is a repository checkout, as for the systemd unit;
    ``$CONCLAVE_HOME`` is temporary."""
    path = tmp_path / "checkout"
    path.mkdir()
    (path / "pyproject.toml").write_text("[project]\nname = 'repo'\n", encoding="utf-8")
    monkeypatch.chdir(path)
    monkeypatch.setenv("CONCLAVE_HOME", str(tmp_path / "env-home"))
    return path


def _outside_pytest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing tells the code a test runs. Called in the test body: pytest sets
    ``PYTEST_CURRENT_TEST`` again before each phase, so a fixture could not remove it."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)


async def _insert_rows(db: Database, count: int) -> None:
    now = int(time.time() * 1000)
    async with db.transaction() as conn:
        await conn.executemany(
            "INSERT INTO interaction_logs(session_id, agent, time_created, time_updated) "
            "VALUES (?, ?, ?, ?)",
            [(f"s{i:05d}", "developer", now, now) for i in range(count)],
        )


def _archive_files(directory: Path) -> list[str]:
    """The archive files in *directory*, temporary ones included."""
    return sorted(p.name for p in directory.iterdir() if "INTERACTION_LOGS" in p.name)


async def test_a_dispatch_only_records_its_row(
    db: Database, tmp_path: Path, checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exports: list[tuple[Any, ...]] = []

    async def _export(*args: Any, **kwargs: Any) -> dict[str, Any]:
        exports.append((args, kwargs))
        return {}

    monkeypatch.setattr(parquet_sync, "export_interaction_logs_to_parquet", _export)
    _outside_pytest(monkeypatch)

    entry = await log_agent_interaction(
        db,
        title="t",
        directory=str(checkout),
        model="m",
        agent="developer",
        prompt="do it",
        result=AgentResult(ok=True, text="done"),
        duration_seconds=1.5,
    )

    assert exports == []
    assert await repo.get_interaction_log(db, entry.session_id) is not None
    assert list(tmp_path.rglob("*INTERACTION_LOGS*")) == []
    assert [p.name for p in checkout.iterdir()] == ["pyproject.toml"]


async def test_without_pyarrow_every_row_goes_to_a_json_lines_file(
    db: Database, tmp_path: Path, no_pyarrow: None
) -> None:
    home = tmp_path / "home"
    archive = home / "INTERACTION_LOGS.jsonl"

    empty = await export_interaction_logs_to_parquet(db, home=home)
    assert (empty["path"], empty["rows"], empty["format"]) == (str(archive), 0, "jsonl")
    assert archive.read_bytes() == b"" and empty["size_bytes"] == 0

    await _insert_rows(db, _MANY)
    info = await export_interaction_logs_to_parquet(db, home=home)

    assert info == {
        "ok": True,
        "path": str(archive),
        "rows": _MANY,
        "format": "jsonl",
        "size_bytes": archive.stat().st_size,
    }
    lines = archive.read_text(encoding="utf-8").split("\n")
    assert lines.pop() == ""  # every line ends with a newline, and no line is blank
    assert len(lines) == _MANY == await db.fetchval("SELECT COUNT(*) FROM interaction_logs")
    records = [json.loads(line) for line in lines]
    assert all(list(rec) == _COLUMNS for rec in records)
    assert {rec["session_id"] for rec in records} == {f"s{i:05d}" for i in range(_MANY)}
    # Replaced atomically, and no .parquet name on JSON Lines.
    assert _archive_files(home) == ["INTERACTION_LOGS.jsonl"]


async def test_a_parquet_target_path_without_pyarrow_is_written_as_jsonl(
    db: Database, tmp_path: Path, no_pyarrow: None
) -> None:
    await _insert_rows(db, 3)
    out = tmp_path / "out"

    info = await export_interaction_logs_to_parquet(db, target_path=out / "logs.parquet")

    assert (info["path"], info["rows"], info["format"]) == (str(out / "logs.jsonl"), 3, "jsonl")
    assert [p.name for p in out.iterdir()] == ["logs.jsonl"]


async def test_a_failed_export_leaves_the_previous_archive_in_place(
    db: Database, tmp_path: Path, no_pyarrow: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    await _insert_rows(db, 3)
    await export_interaction_logs_to_parquet(db, home=home)
    before = (home / "INTERACTION_LOGS.jsonl").read_bytes()

    def _broken(row: Any) -> dict[str, Any]:
        raise ValueError("cannot convert")

    monkeypatch.setattr(parquet_sync, "_archive_record", _broken)
    with pytest.raises(ValueError, match="cannot convert"):
        await export_interaction_logs_to_parquet(db, home=home)

    assert (home / "INTERACTION_LOGS.jsonl").read_bytes() == before
    assert _archive_files(home) == ["INTERACTION_LOGS.jsonl"]  # its temporary file is gone


async def test_with_pyarrow_the_archive_is_parquet(db: Database, tmp_path: Path) -> None:
    pq = pytest.importorskip("pyarrow.parquet")
    home = tmp_path / "home"
    archive = home / "INTERACTION_LOGS.parquet"

    empty = await export_interaction_logs_to_parquet(db, home=home)
    assert (empty["path"], empty["rows"], empty["format"]) == (str(archive), 0, "parquet")
    empty_table = pq.read_table(archive)
    assert (empty_table.num_rows, empty_table.column_names) == (0, _COLUMNS)

    await _insert_rows(db, 1_234)  # several pages
    info = await export_interaction_logs_to_parquet(db, home=home)

    table = pq.read_table(archive)
    assert (info["path"], info["rows"], info["format"]) == (str(archive), 1_234, "parquet")
    assert (table.num_rows, table.column_names) == (1_234, _COLUMNS)
    assert info["size_bytes"] == archive.stat().st_size
    assert _archive_files(home) == ["INTERACTION_LOGS.parquet"]


async def test_rest_and_mcp_exports_write_only_into_the_daemon_home(
    db: Database,
    tmp_path: Path,
    checkout: Path,
    no_pyarrow: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _outside_pytest(monkeypatch)
    await _insert_rows(db, 3)
    home = tmp_path / "home"
    daemon = Daemon(db, home, _StubProvider(), workers_enabled=False)

    app = create_app(daemon, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        res = await c.post("/api/observability/export-parquet")
    assert res.status_code == 200
    stats = await call_mcp_tool(
        daemon, "conclave_get_observability_stats", {"export_parquet": True}
    )

    archive = home / "INTERACTION_LOGS.jsonl"
    expected = {
        "ok": True,
        "path": str(archive),
        "rows": 3,
        "format": "jsonl",
        "size_bytes": archive.stat().st_size,
    }
    assert res.json() == expected
    assert stats["parquet"] == expected
    assert _archive_files(home) == ["INTERACTION_LOGS.jsonl"]
    assert [p.name for p in checkout.iterdir()] == ["pyproject.toml"]
    assert not (tmp_path / "env-home").exists()


async def test_the_export_converts_rows_off_the_event_loop(
    db: Database, tmp_path: Path, no_pyarrow: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _insert_rows(db, _MANY)
    loop_thread = threading.get_ident()
    converting_threads: set[int] = set()
    archive_record = parquet_sync._archive_record

    def _spy(row: Any) -> dict[str, Any]:
        converting_threads.add(threading.get_ident())
        return archive_record(row)

    monkeypatch.setattr(parquet_sync, "_archive_record", _spy)
    ticks = 0
    exported = asyncio.Event()

    async def _ticker() -> None:
        nonlocal ticks
        while not exported.is_set():
            ticks += 1
            await asyncio.sleep(0)

    ticker = asyncio.create_task(_ticker())
    try:
        info = await export_interaction_logs_to_parquet(db, home=tmp_path / "home")
    finally:
        exported.set()
        await ticker

    assert info["rows"] == _MANY
    assert converting_threads and loop_thread not in converting_threads
    assert ticks >= _MANY // 500  # the loop kept running, at least once per page
