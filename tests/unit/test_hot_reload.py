"""Unit tests for 100% quiescent in-process hot-reload (hot_reload.py)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
from httpx import ASGITransport

import conclave
from conclave.db import Database, TaskState
from conclave.db import repositories as repo
from conclave.events import EventType
from conclave.hot_reload import (
    HotReloader,
    discover_conclave_modules,
    is_quiescent,
    py_file_to_module_name,
)
from conclave.mcp.tools import call_mcp_tool
from conclave.providers import AgentResult, OnChunk, Provider, ResolvedProfile
from conclave.runtime import Daemon, ProjectWorker
from conclave.web import create_app


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
        return AgentResult(ok=True, text='```json\n{"verdict": "pass", "reason": "ok"}\n```')


def test_discover_conclave_modules_covers_100_percent_in_topological_order() -> None:
    """Every single .py file under src/conclave/ must be discovered in topological order."""
    pkg_dir = Path(conclave.__file__).resolve().parent
    all_py_files = [
        p for p in pkg_dir.rglob("*.py") if "__pycache__" not in p.parts
    ]
    expected_modules = {py_file_to_module_name(p, pkg_dir) for p in all_py_files}

    discovered = discover_conclave_modules(pkg_dir)
    assert len(discovered) == len(expected_modules)
    assert set(discovered) == expected_modules, (
        f"Missing modules from hot-reload discovery: {expected_modules - set(discovered)}"
    )

    # Verify strict bottom-up dependency ordering
    idx = {name: i for i, name in enumerate(discovered)}
    assert idx["conclave.util"] < idx["conclave.events.types"]
    assert idx["conclave.events.types"] < idx["conclave.events.bus"]
    assert idx["conclave.events"] < idx["conclave.config.graph"]
    assert idx["conclave.config.graph"] < idx["conclave.config.models"]
    assert idx["conclave.config"] < idx["conclave.agents.personas"]
    assert idx["conclave.agents"] < idx["conclave.db.migrations"]
    assert idx["conclave.db.migrations"] < idx["conclave.db.models"]
    assert idx["conclave.db.models"] < idx["conclave.db.planning_models"]
    assert idx["conclave.db.planning_models"] < idx["conclave.db.database"]
    assert idx["conclave.db.database"] < idx["conclave.db.repositories"]
    assert idx["conclave.db"] < idx["conclave.verification.quarantine"]
    assert idx["conclave.verification"] < idx["conclave.observability.parquet_sync"]
    assert idx["conclave.observability"] < idx["conclave.providers.base"]
    assert idx["conclave.providers"] < idx["conclave.bootstrap"]
    assert idx["conclave.bootstrap"] < idx["conclave.engine.orchestrator"]
    assert idx["conclave.engine.graph_executor"] < idx["conclave.engine.orchestrator"]
    assert idx["conclave.engine.workflow_evolver"] < idx["conclave.engine.orchestrator"]
    assert idx["conclave.engine.orchestrator"] < idx["conclave.engine.task_diff"]
    assert idx["conclave.engine.task_diff"] < idx["conclave.engine"]
    assert idx["conclave.engine"] < idx["conclave.planning.session"]
    assert idx["conclave.planning"] < idx["conclave.hot_reload"]
    assert idx["conclave.hot_reload"] < idx["conclave.runtime"]
    assert idx["conclave.runtime"] < idx["conclave.mcp.tools"]
    assert idx["conclave.mcp"] < idx["conclave.web.api"]
    assert idx["conclave.web.api"] < idx["conclave.web.app"]
    assert idx["conclave.web"] < idx["conclave.main"]


async def test_hot_reloader_swaps_runtime_preserves_subscribers_and_emits_event(
    db: Database, tmp_path: Path
) -> None:
    """reload_now() reloads modules, re-runs migrations/seeds, swaps orchestrator & workers,
    preserves live EventBus subscribers, and emits system.reloaded."""
    daemon = Daemon(db, tmp_path / "home", _StubProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)

    project = await repo.create_project(
        db, name="demo", path=str(tmp_path), default_branch="main"
    )
    old_worker = ProjectWorker(db, daemon.orchestrator, project.id)
    old_worker.paused = True
    daemon._workers[project.id] = old_worker

    old_orch = daemon.orchestrator
    old_planning = daemon.planning_orchestrator
    old_bus = daemon.bus

    # Subscribe to live EventBus before reload
    sub = daemon.bus.subscribe()
    with sub:
        res = await daemon.reload_now()
        assert res["reloaded"] is True
        assert res["deferred"] is False
        assert res["reload_count"] == 1
        assert res["modules_reloaded"] >= 30
        assert res["routes_refreshed"] > 0

        # DB and EventBus instances are preserved with active subscriber intact
        assert daemon.db is db
        assert daemon.bus is old_bus
        assert daemon.bus.subscriber_count() == 1

        # Orchestrator, PlanningOrchestrator, and ProjectWorker were hot-swapped
        assert daemon.orchestrator is not old_orch
        assert daemon.planning_orchestrator is not old_planning
        new_worker = daemon.worker(project.id)
        assert new_worker is not None
        assert new_worker is not old_worker
        assert new_worker.paused is True
        assert new_worker._orchestrator is daemon.orchestrator

        # Subscriber received system.reloaded event
        received: list[Any] = []
        while not sub.queue.empty():
            received.append(await sub.__anext__())
        reloaded_events = [
            e for e in received if e.type == EventType.system_reloaded
        ]
        assert len(reloaded_events) == 1
        assert reloaded_events[0].payload["reload_count"] == 1
        assert app.state.daemon is daemon


async def test_quiescent_deferral_and_auto_reload_on_task_completion(
    db: Database, tmp_path: Path
) -> None:
    """When a task is active in orchestrator._cancel_events, reload requests defer with
    reload_pending=True and execute automatically as soon as the task finishes."""
    daemon = Daemon(db, tmp_path / "home", _StubProvider(), workers_enabled=False)
    create_app(daemon, manage_lifecycle=False)

    project = await repo.create_project(
        db, name="self-host", path=str(tmp_path), default_branch="main"
    )
    task = await repo.create_task(
        db,
        project_id=project.id,
        title="Hot reload test",
        request="Test quiescent deferral",
        state=TaskState.in_progress,
    )

    # Simulate an in-flight task in orchestrator._cancel_events
    daemon.orchestrator._cancel_events[task.id] = asyncio.Event()
    assert is_quiescent(daemon) is False

    # Calling reload_now(force=False) while busy defers the reload
    deferred = await daemon.reload_now(force=False)
    assert deferred["reloaded"] is False
    assert deferred["deferred"] is True
    assert deferred["reload_pending"] is True
    assert daemon.reloader.reload_pending is True
    assert daemon.reloader.reload_count == 0

    # Simulate task completion inside ProjectWorker._run_claimed_task
    daemon.orchestrator._cancel_events.pop(task.id, None)

    async def _noop_process_task(*_a: Any, **_kw: Any) -> bool:
        # While task is running inside _run_claimed_task, it is not quiescent
        assert is_quiescent(daemon) is False
        await repo.finalize_task(
            db, task.id, state=TaskState.done, result_summary="completed"
        )
        return True

    daemon.orchestrator.process_task = _noop_process_task  # type: ignore[method-assign]
    worker = ProjectWorker(db, daemon.orchestrator, project.id)
    daemon._workers[project.id] = worker

    await worker._run_claimed_task(task)

    # Once _run_claimed_task finishes its finally block, _cancel_events is empty
    # and the deferred reload has executed automatically!
    assert is_quiescent(daemon) is True
    assert daemon.reloader.reload_pending is False
    assert daemon.reloader.reload_count == 1


async def test_api_and_mcp_reload_endpoints_and_spa_no_cache(
    db: Database, tmp_path: Path
) -> None:
    """Verify GET /api/system/reload-status, POST /api/system/reload, conclave_reload_system,
    and SPA index.html Cache-Control: no-cache."""
    daemon = Daemon(db, tmp_path / "home", _StubProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    transport = ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        status_res = await client.get("/api/system/reload-status")
        assert status_res.status_code == 200
        status_data = status_res.json()
        assert status_data["quiescent"] is True
        assert status_data["reload_pending"] is False
        assert status_data["discoverable_modules"] >= 35

        reload_res = await client.post("/api/system/reload", json={"force": False})
        assert reload_res.status_code == 200
        reload_data = reload_res.json()
        assert reload_data["reloaded"] is True
        assert reload_data["reload_count"] >= 1

        # Also test MCP tool conclave_reload_system
        mcp_res = await call_mcp_tool(daemon, "conclave_reload_system", {"force": False})
        assert mcp_res["reloaded"] is True
        assert mcp_res["reload_count"] == reload_data["reload_count"] + 1

        # Verify SPA root response sets Cache-Control: no-cache
        spa_res = await client.get("/")
        assert spa_res.status_code == 200
        assert "no-cache" in spa_res.headers.get("cache-control", "").lower()


def test_reloader_file_snapshot_detects_modifications(tmp_path: Path) -> None:
    """HotReloader.changed_files() detects modified and newly added files."""
    pkg = tmp_path / "src" / "conclave"
    pkg.mkdir(parents=True)
    mod_file = pkg / "sample.py"
    mod_file.write_text("X = 1\n", encoding="utf-8")

    reloader = HotReloader(repo_root=tmp_path)
    assert reloader.changed_files() == []

    mod_file.write_text("X = 222\n", encoding="utf-8")
    changed = reloader.changed_files()
    assert "src/conclave/sample.py" in changed
