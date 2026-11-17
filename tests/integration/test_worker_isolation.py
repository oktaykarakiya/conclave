"""Worker isolation reaches every dispatch.

``execution.worker_isolation`` (built-in ``true``) is layered like every setting (built-in ->
global -> project delta) and becomes ``ResolvedProfile.isolated`` on each dispatch of a task run
and of a planning turn. Deterministic: a throwaway git repo and the FakeProvider, no LLM.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fake_provider import FakeProvider

from conclave.config import (
    create_project_with_config,
    deep_merge,
    resolve_project_config,
    save_global_config,
)
from conclave.db import Database, Project, TaskState
from conclave.db import repositories as repo
from conclave.engine import Orchestrator, run_git
from conclave.events import EventBus
from conclave.planning.session import PlanningOrchestrator

_OFF: dict[str, Any] = {"execution": {"worker_isolation": False}}
_ON: dict[str, Any] = {"execution": {"worker_isolation": True}}

# (global layer patch, project patch, effective execution.worker_isolation)
_BUILT_IN = pytest.param(None, None, True, id="built-in")
_PROJECT_OFF = pytest.param(None, _OFF, False, id="project-off")
_GLOBAL_OFF = pytest.param(_OFF, None, False, id="global-off")
_PROJECT_ON_OVER_GLOBAL_OFF = pytest.param(_OFF, _ON, True, id="project-on-over-global-off")


async def _project(
    db: Database,
    path: Path,
    global_patch: dict[str, Any] | None,
    project_patch: dict[str, Any] | None,
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


@pytest.mark.parametrize(
    ("global_patch", "project_patch", "expected"),
    [_BUILT_IN, _PROJECT_OFF, _GLOBAL_OFF, _PROJECT_ON_OVER_GLOBAL_OFF],
)
async def test_worker_isolation_is_layered(
    db: Database,
    tmp_path: Path,
    global_patch: dict[str, Any] | None,
    project_patch: dict[str, Any] | None,
    expected: bool,
) -> None:
    project = await _project(db, tmp_path, global_patch, project_patch)

    config = await resolve_project_config(db, project)

    assert config.execution.worker_isolation is expected
    # The project stores the key only when it overrides what it inherits.
    stored = project.config.get("execution", {}).get("worker_isolation")
    assert stored == (project_patch or {}).get("execution", {}).get("worker_isolation")


@pytest.mark.parametrize(
    ("global_patch", "project_patch", "expected"), [_BUILT_IN, _PROJECT_OFF, _GLOBAL_OFF]
)
async def test_task_run_dispatches_follow_worker_isolation(
    db: Database,
    tmp_path: Path,
    global_patch: dict[str, Any] | None,
    project_patch: dict[str, Any] | None,
    expected: bool,
) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await _project(db, repo_path, global_patch, project_patch)
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )
    provider = FakeProvider()
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None

    assert await orchestrator.process_task(claimed) is True

    done = await repo.get_task(db, task.id)
    assert done is not None and done.state is TaskState.done
    # The planner, the developer and the mandatory reviewers all dispatched.
    assert sum("Produce a structured plan" in p for p in provider.prompts) == 1
    assert sum("Review the changes made for this task" in p for p in provider.prompts) >= 3
    assert len(provider.profiles) >= 5
    assert {p.isolated for p in provider.profiles} == {expected}


@pytest.mark.parametrize(
    ("global_patch", "project_patch", "expected"), [_BUILT_IN, _PROJECT_OFF, _GLOBAL_OFF]
)
async def test_planning_turn_dispatch_follows_worker_isolation(
    db: Database,
    tmp_path: Path,
    global_patch: dict[str, Any] | None,
    project_patch: dict[str, Any] | None,
    expected: bool,
) -> None:
    project = await _project(db, tmp_path, global_patch, project_patch)
    session = await repo.create_planning_session(
        db, project_id=project.id, title="T", prompt="Plan X"
    )
    provider = FakeProvider()
    orchestrator = PlanningOrchestrator(db, EventBus(db), provider)

    text, _ = await orchestrator._agent_turn(
        session.id, "planner", "Planning Facilitator Agent", "Plan X"
    )

    assert text is not None and "task_changes" in text
    assert [p.isolated for p in provider.profiles] == [expected]
