"""The worker sandbox setting reaches every dispatch, and a missing sandbox is reported.

``execution.worker_sandbox`` (built-in ``auto``) is layered like every setting and becomes
``ResolvedProfile.sandbox`` on each dispatch of a task run and of a planning turn. With
isolation on, ``auto`` and no working bubblewrap, each task and each planning session records one
``log`` event at its start naming the cause and the fix. MCP reads and patches the setting like
any other ``execution`` field. Deterministic: a throwaway git repo and the FakeProvider, and
``CONCLAVE_BWRAP_BIN`` pointing at a missing file or at a stand-in bubblewrap.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from fake_provider import FakeProvider

from conclave.config import (
    WorkerSandbox,
    create_project_with_config,
    deep_merge,
    load_project_config,
    resolve_project_config,
    save_global_config,
)
from conclave.config.schema import config_schema
from conclave.db import Database, EventRow, Project, TaskState
from conclave.db import repositories as repo
from conclave.engine import Orchestrator, run_git
from conclave.events import EventBus, EventType
from conclave.mcp.server import handle_mcp_message
from conclave.mcp.tools import call_mcp_tool
from conclave.planning.session import PlanningOrchestrator
from conclave.runtime import Daemon

_OFF: dict[str, Any] = {"execution": {"worker_sandbox": "off"}}
_REQUIRED: dict[str, Any] = {"execution": {"worker_sandbox": "required"}}
_AUTO: dict[str, Any] = {"execution": {"worker_sandbox": "auto"}}
_NO_ISOLATION: dict[str, Any] = {"execution": {"worker_isolation": False}}


@pytest.fixture
def no_bwrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    missing = str(tmp_path / "no-bwrap")
    monkeypatch.setenv("CONCLAVE_BWRAP_BIN", missing)
    return missing


async def _project(
    db: Database,
    path: Path,
    global_patch: dict[str, Any] | None = None,
    project_patch: dict[str, Any] | None = None,
) -> Project:
    """A project with a test command, created through the layered config writers."""
    if global_patch is not None:
        await save_global_config(db, global_patch)
    return await create_project_with_config(
        db,
        name="p",
        path=str(path),
        default_branch="main",
        patch=deep_merge({"execution": {"baseline_test_command": "echo ok"}}, project_patch or {}),
    )


async def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    await run_git(path, "config", "user.email", "test@example.com")
    await run_git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# test repo\n", encoding="utf-8")
    await run_git(path, "add", "-A")
    await run_git(path, "commit", "-m", "initial commit")


async def _run_task(
    db: Database, tmp_path: Path, project_patch: dict[str, Any] | None = None
) -> tuple[FakeProvider, list[EventRow]]:
    """Run one task to the end; return the provider and the task's events."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await _project(db, repo_path, project_patch=project_patch)
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )
    provider = FakeProvider()
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None

    assert await orchestrator.process_task(claimed) is True

    return provider, await repo.list_events(db, task_id=task.id)


def _sandbox_logs(events: list[EventRow]) -> list[EventRow]:
    return [
        event
        for event in events
        if event.type == EventType.log and event.payload.get("stage") == "worker_sandbox"
    ]


def test_worker_sandbox_is_auto_by_default_and_in_the_config_schema() -> None:
    assert load_project_config({}).execution.worker_sandbox == WorkerSandbox.auto
    assert load_project_config(_REQUIRED).execution.worker_sandbox == WorkerSandbox.required
    schema = config_schema()
    prop = schema["$defs"]["ExecutionSettings"]["properties"]["worker_sandbox"]
    assert prop["default"] == "auto"
    assert schema["$defs"]["WorkerSandbox"]["enum"] == ["auto", "required", "off"]
    assert "bubblewrap" in prop["description"]
    isolation = schema["$defs"]["ExecutionSettings"]["properties"]["worker_isolation"]
    assert "worker_sandbox" in isolation["description"]
    with pytest.raises(ValueError):
        load_project_config({"execution": {"worker_sandbox": "always"}})


@pytest.mark.parametrize(
    ("global_patch", "project_patch", "expected"),
    [
        pytest.param(None, None, WorkerSandbox.auto, id="built-in"),
        pytest.param(None, _OFF, WorkerSandbox.off, id="project-off"),
        pytest.param(_REQUIRED, None, WorkerSandbox.required, id="global-required"),
        pytest.param(_OFF, _AUTO, WorkerSandbox.auto, id="project-auto-over-global-off"),
    ],
)
async def test_worker_sandbox_is_layered(
    db: Database,
    tmp_path: Path,
    global_patch: dict[str, Any] | None,
    project_patch: dict[str, Any] | None,
    expected: WorkerSandbox,
) -> None:
    project = await _project(db, tmp_path, global_patch, project_patch)

    config = await resolve_project_config(db, project)

    assert config.execution.worker_sandbox == expected
    # The project stores the key only when it overrides what it inherits.
    stored = project.config.get("execution", {}).get("worker_sandbox")
    assert stored == (project_patch or {}).get("execution", {}).get("worker_sandbox")


@pytest.mark.parametrize(
    ("project_patch", "expected"),
    [(None, WorkerSandbox.auto), (_REQUIRED, WorkerSandbox.required), (_OFF, WorkerSandbox.off)],
)
async def test_every_dispatch_of_a_task_run_carries_worker_sandbox(
    db: Database,
    tmp_path: Path,
    no_bwrap: str,
    project_patch: dict[str, Any] | None,
    expected: WorkerSandbox,
) -> None:
    provider, _events = await _run_task(db, tmp_path, project_patch)

    assert len(provider.profiles) >= 5
    assert {p.sandbox for p in provider.profiles} == {expected}
    assert {p.isolated for p in provider.profiles} == {True}


@pytest.mark.parametrize(
    ("project_patch", "expected"), [(None, WorkerSandbox.auto), (_OFF, WorkerSandbox.off)]
)
async def test_a_planning_turn_dispatch_carries_worker_sandbox(
    db: Database, tmp_path: Path, project_patch: dict[str, Any] | None, expected: WorkerSandbox
) -> None:
    project = await _project(db, tmp_path, project_patch=project_patch)
    session = await repo.create_planning_session(
        db, project_id=project.id, title="T", prompt="Plan X"
    )
    provider = FakeProvider()

    text, _ = await PlanningOrchestrator(db, EventBus(db), provider)._agent_turn(
        session.id, "planner", "Planning Facilitator Agent", "Plan X"
    )

    assert text is not None
    assert [p.sandbox for p in provider.profiles] == [expected]


async def test_a_task_records_one_log_event_when_its_workers_run_without_the_sandbox(
    db: Database, tmp_path: Path, no_bwrap: str
) -> None:
    _provider, events = await _run_task(db, tmp_path)

    [notice] = _sandbox_logs(events)
    message = str(notice.payload["message"])
    assert no_bwrap in message  # the cause
    assert "install bubblewrap" in message and "unprivileged user namespaces" in message
    assert "worker_sandbox to off" in message
    assert no_bwrap in str(notice.payload["reason"])
    types = [event.type for event in events]
    started = types.index(EventType.task_started)
    assert events[started + 1] == notice  # at the task's start, before any dispatch
    assert started + 1 < types.index(EventType.agent_dispatched)


@pytest.mark.parametrize("project_patch", [_OFF, _REQUIRED, _NO_ISOLATION], ids=str)
async def test_no_log_event_unless_isolation_is_on_and_the_sandbox_auto(
    db: Database, tmp_path: Path, no_bwrap: str, project_patch: dict[str, Any]
) -> None:
    _provider, events = await _run_task(db, tmp_path, project_patch)

    assert _sandbox_logs(events) == []


async def test_no_log_event_for_a_task_its_config_stops_before_any_worker(
    db: Database, tmp_path: Path, no_bwrap: str
) -> None:
    project = await create_project_with_config(
        db, name="p", path=str(tmp_path), default_branch="main", patch={}
    )  # no test command: the task fails before any worker runs
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    orchestrator = Orchestrator(db, EventBus(db), FakeProvider(), tmp_path / "home")

    assert await orchestrator.process_task(claimed) is False

    events = await repo.list_events(db, task_id=task.id)
    assert [event.type for event in events] == [EventType.task_started, EventType.task_failed]


@pytest.mark.skipif(os.name == "nt", reason="a POSIX shell script stands in for bubblewrap")
async def test_no_log_event_when_the_sandbox_works(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bwrap = tmp_path / "bwrap"
    bwrap.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")  # passes the probe
    bwrap.chmod(0o755)
    monkeypatch.setenv("CONCLAVE_BWRAP_BIN", str(bwrap))

    _provider, events = await _run_task(db, tmp_path)

    assert _sandbox_logs(events) == []


async def test_a_planning_session_records_one_log_event_at_its_start(
    db: Database, tmp_path: Path, no_bwrap: str
) -> None:
    project = await _project(db, tmp_path)
    orchestrator = PlanningOrchestrator(db, EventBus(db), FakeProvider())

    # No round: the discussion starts, reports, and ends stable at once.
    session = await orchestrator.create_and_start(project.id, "T", "Plan X", max_rounds=0)
    await orchestrator._active_sessions[session.id]

    events = [
        event
        for event in await repo.list_events(db, project_id=project.id)
        if event.planning_session_id == session.id
    ]
    [notice] = _sandbox_logs(events)
    assert no_bwrap in str(notice.payload["message"])
    types = [event.type for event in events]
    assert types.index(EventType.log) == types.index(EventType.planning_session_started) + 1


async def test_mcp_reads_and_patches_worker_sandbox(db: Database, tmp_path: Path) -> None:
    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    git_repo = tmp_path / "repo"
    (git_repo / ".git").mkdir(parents=True)
    pid = (
        await call_mcp_tool(daemon, "conclave_attach_project", {"name": "p", "path": str(git_repo)})
    )["id"]

    async def sandbox() -> str:
        config = await call_mcp_tool(daemon, "conclave_get_config", {"project_id": pid})
        return str(config["execution"]["worker_sandbox"])

    assert await sandbox() == "auto"
    await call_mcp_tool(
        daemon,
        "conclave_patch_config",
        {"project_id": pid, "config": {"execution": {"worker_sandbox": "required"}}},
    )
    assert await sandbox() == "required"
    await call_mcp_tool(
        daemon, "conclave_patch_config", {"project_id": pid, "worker_sandbox": "off"}
    )
    assert await sandbox() == "off"

    response = await handle_mcp_message(
        daemon,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "conclave_patch_config",
                "arguments": {"project_id": pid, "worker_sandbox": "always"},
            },
        },
    )
    assert response is not None and response["result"]["isError"] is True
    assert await sandbox() == "off"  # the bad patch was not stored
