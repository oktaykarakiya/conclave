"""Gate integrity end to end: no silent pass without tests, and the test-integrity guard.

A project without a test command fails its tasks before any agent runs (``no_test_command``)
unless ``execution.allow_no_test_command`` opts out. A developer change that takes pre-existing
tests away is flagged to every reviewer (``TEST INTEGRITY``), reported as one
``test_integrity.flagged`` event per attempt and in the result summary, and under ``block`` is
never merged automatically. Deterministic: a throwaway git repo and the FakeProvider, no LLM.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fake_provider import FakeProvider

from conclave.config import (
    EdgeCondition,
    NodeKind,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    resolve_project_config,
)
from conclave.db import CheckpointStatus, Database, Project, Task, TaskState
from conclave.db import repositories as repo
from conclave.engine import Orchestrator, run_git
from conclave.engine.orchestrator import _NO_TEST_COMMAND_SUMMARY
from conclave.engine.task_merge import merge_task
from conclave.engine.workflow_evolver import compute_workflow_evolution
from conclave.events import EventBus
from conclave.providers import AgentResult, OnChunk, ResolvedProfile

_EXISTING_TEST = "tests/test_existing.py"
_REVIEW_PROMPT = "Review the changes made for this task"


async def _init_repo(path: Path) -> None:
    """A repo whose initial commit holds one pre-existing test."""
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    await run_git(path, "config", "user.email", "test@example.com")
    await run_git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# test repo\n", encoding="utf-8")
    (path / "tests").mkdir()
    (path / _EXISTING_TEST).write_text(
        "def test_existing() -> None:\n    assert True\n", encoding="utf-8"
    )
    await run_git(path, "add", "-A")
    await run_git(path, "commit", "-m", "initial commit")


async def _run(
    db: Database, tmp_path: Path, provider: FakeProvider, config: dict[str, Any]
) -> tuple[Orchestrator, Project, Task, bool]:
    """Attach ``tmp_path/repo`` with *config* and process one approved task."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main", config=config
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    ok = await orchestrator.process_task(claimed)
    final = await repo.get_task(db, task.id)
    assert final is not None
    return orchestrator, project, final, ok


async def _payloads(db: Database, task_id: str, event_type: str) -> list[dict[str, Any]]:
    return [e.payload for e in await repo.list_events(db, task_id=task_id) if e.type == event_type]


async def _gate_skipped(db: Database, task_id: str) -> list[dict[str, Any]]:
    logs = await _payloads(db, task_id, "log")
    return [p for p in logs if p.get("stage") == "gate_skipped_no_tests"]


# --- no test command ----------------------------------------------------------


async def test_no_test_command_fails_the_task_before_any_dispatch(
    db: Database, tmp_path: Path
) -> None:
    provider = FakeProvider()
    _, project, task, ok = await _run(db, tmp_path, provider, {"execution": {}})

    assert ok is False and task.state is TaskState.failed
    assert await _payloads(db, task.id, "task.failed") == [{"reason": "no_test_command"}]
    summary = task.result_summary or ""
    assert "execution.baseline_test_command" in summary
    assert "execution.allow_no_test_command: true" in summary
    assert provider.prompts == []  # no planner, developer or reviewer was dispatched
    types = [e.type for e in await repo.list_events(db, task_id=task.id)]
    assert types == ["task.started", "task.failed"]  # not even a baseline ran
    worktree = tmp_path / "home" / "projects" / project.id / "worktrees" / task.id
    assert not worktree.exists()
    code, _ = await run_git(tmp_path / "repo", "rev-parse", "--verify", f"conclave/{task.id}")
    assert code != 0


async def _count(db: Database, table: str, project_id: str) -> int:
    row = await db.fetchone(
        f"SELECT COUNT(*) AS n FROM {table} WHERE project_id = ?", (project_id,)
    )
    assert row is not None
    return int(row["n"])


async def test_no_test_command_failure_still_runs_the_task_housekeeping(
    db: Database, tmp_path: Path
) -> None:
    """The early return skips the worktree, not the pruning and per-task cleanup every task gets."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"retention_events_max": 100}},
    )
    for i in range(150):
        await repo.append_event(db, type="log", project_id=project.id, payload={"i": i})
    for i in range(25):
        await repo.save_baseline(db, project.id, f"sha-{i}", "old failures")
    await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )
    orchestrator = Orchestrator(db, EventBus(db), FakeProvider(), tmp_path / "home")
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    orchestrator._cancel_events[claimed.id] = asyncio.Event()  # as the worker registers it
    assert orchestrator.request_steer(claimed.id, "use the existing helper")

    assert await orchestrator.process_task(claimed) is False

    task = await repo.get_task(db, claimed.id)
    assert task is not None and task.state is TaskState.failed
    assert task.result_summary == _NO_TEST_COMMAND_SUMMARY
    assert await _count(db, "events", project.id) == 100
    assert await _count(db, "baselines", project.id) == 20
    types = [e.type for e in await repo.list_events(db, task_id=task.id)]
    assert types == ["task.started", "task.failed"]
    worktrees = tmp_path / "home" / "projects" / project.id / "worktrees"
    assert not (worktrees / task.id).exists()
    assert task.id not in orchestrator._cancel_events
    assert task.id not in orchestrator._pending_steers


class _DeveloperDropsTestCommand(FakeProvider):
    """A developer dispatch during which the operator removes the project's test command."""

    def __init__(self, db: Database) -> None:
        super().__init__()
        self.db = db

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
        if self.role_of(prompt) == "developer":
            (project,) = await repo.list_projects(self.db)
            execution = {**project.config["execution"], "baseline_test_command": ""}
            await repo.update_project_config(
                self.db, project.id, {**project.config, "execution": execution}
            )
        return await super().run_agent(
            profile=profile, prompt=prompt, timeout_seconds=timeout_seconds, cwd=cwd,
            on_chunk=on_chunk, cancel_event=cancel_event,
        )


async def test_test_command_removed_mid_task_fails_the_task_like_the_early_check(
    db: Database, tmp_path: Path
) -> None:
    provider = _DeveloperDropsTestCommand(db)
    config = {"execution": {"baseline_test_command": "echo ok", "auto_merge": True}}
    _, _, task, ok = await _run(db, tmp_path, provider, config)

    assert ok is False and task.state is TaskState.failed
    assert task.result_summary == _NO_TEST_COMMAND_SUMMARY
    assert await _payloads(db, task.id, "task.failed") == [{"reason": "no_test_command"}]
    assert await _payloads(db, task.id, "attempt.failed") == [
        {"n": 1, "stage": "gate", "reason": "no_test_command"}
    ]
    assert [FakeProvider.role_of(p) for p in provider.prompts].count("developer") == 1
    assert await _gate_skipped(db, task.id) == []
    assert await _payloads(db, task.id, "task.committed") == []
    assert await _payloads(db, task.id, "task.merged") == []
    code, _ = await run_git(tmp_path / "repo", "show", "main:FEATURE.txt")
    assert code != 0


async def test_test_command_removed_mid_task_teaches_the_workflow_evolver_nothing(
    db: Database, tmp_path: Path
) -> None:
    """The review passed, then the gate failed for want of a test command: a configuration
    error, not a red gate after review. No proposal, and the clean streak is left as it was."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db,
        name="t",
        path=str(repo_path),
        default_branch="main",
        config={"execution": {"baseline_test_command": "echo ok"}},  # suggest mode (default)
    )
    for n in range(2):
        await repo.create_task(
            db, project_id=project.id, request=f"done {n}", state=TaskState.done
        )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )
    orchestrator = Orchestrator(
        db, EventBus(db), _DeveloperDropsTestCommand(db), tmp_path / "home"
    )
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None and claimed.id == task.id

    assert await orchestrator.process_task(claimed) is False

    checkpoints = {cp.node_id: cp for cp in await repo.list_task_checkpoints(db, task.id)}
    assert checkpoints["review"].status == CheckpointStatus.completed
    assert (checkpoints["gate"].status, checkpoints["gate"].artifact) == (
        CheckpointStatus.failed,
        {"cause": "no_test_command"},
    )
    assert await repo.list_workflow_proposals(db, project.id) == []
    events = await repo.list_events(db, project_id=project.id, limit=10_000)
    assert [e.type for e in events if e.type.startswith("workflow.")] == []
    stored = await repo.get_project(db, project.id)
    assert stored is not None
    evolution = await compute_workflow_evolution(
        db, project.id, await resolve_project_config(db, stored), task_id=task.id
    )
    assert (
        evolution.action,
        evolution.task_failed,
        evolution.gate_failed_after_review,
        evolution.clean_streak,
    ) == ("none", False, False, 2)


async def test_test_command_removed_mid_task_passes_logged_when_allowed(
    db: Database, tmp_path: Path
) -> None:
    provider = _DeveloperDropsTestCommand(db)
    config = {
        "execution": {
            "baseline_test_command": "echo ok",
            "allow_no_test_command": True,
            "auto_merge": True,
        }
    }
    _, _, task, ok = await _run(db, tmp_path, provider, config)

    assert ok is True and task.state is TaskState.done
    assert len(await _gate_skipped(db, task.id)) == 1
    assert await _payloads(db, task.id, "attempt.failed") == []


async def test_blank_test_command_counts_as_none(db: Database, tmp_path: Path) -> None:
    provider = FakeProvider()
    config = {"execution": {"baseline_test_command": "   "}}
    _, _, task, ok = await _run(db, tmp_path, provider, config)

    assert ok is False and await _payloads(db, task.id, "task.failed") == [
        {"reason": "no_test_command"}
    ]
    assert provider.prompts == []


async def test_allow_no_test_command_runs_ungated_and_says_so(
    db: Database, tmp_path: Path
) -> None:
    config = {"execution": {"allow_no_test_command": True, "auto_merge": True}}
    _, _, task, ok = await _run(db, tmp_path, FakeProvider(), config)

    assert ok is True and task.state is TaskState.done
    code, out = await run_git(tmp_path / "repo", "show", "main:FEATURE.txt")
    assert code == 0 and "done" in out
    skipped = await _gate_skipped(db, task.id)
    assert len(skipped) == 1
    assert skipped[0]["attempt"] == 1 and "ran no tests" in skipped[0]["message"]


async def test_require_full_green_false_keeps_running_without_tests(
    db: Database, tmp_path: Path
) -> None:
    """An explicit ``require_full_green: false`` is a choice: no failure and no gate at all."""
    config = {"execution": {"require_full_green": False}}
    _, _, task, ok = await _run(db, tmp_path, FakeProvider(), config)

    assert ok is True and task.state is TaskState.done
    assert await _gate_skipped(db, task.id) == []


async def test_review_only_workflow_without_tests_is_not_failed(
    db: Database, tmp_path: Path
) -> None:
    """A flow without a developer changes nothing, so it needs no test command."""
    workflow = WorkflowGraph(
        nodes=[
            WorkflowNode(id="reviewer", kind=NodeKind.reviewer, agent="reviewer"),
            WorkflowNode(id="gate", kind=NodeKind.gate),
            WorkflowNode(id="submitter", kind=NodeKind.custom, agent="submitter"),
        ],
        edges=[
            WorkflowEdge(id="e1", source="reviewer", target="gate"),
            WorkflowEdge(
                id="e2", source="gate", target="submitter", condition=EdgeCondition.always
            ),
        ],
    )
    provider = FakeProvider()
    config = {"execution": {}, "workflow": workflow.model_dump(mode="json")}
    _, _, task, ok = await _run(db, tmp_path, provider, config)

    assert ok is True and task.state is TaskState.done
    assert any(_REVIEW_PROMPT in p for p in provider.prompts)
    assert await _gate_skipped(db, task.id) == []
    assert await _payloads(db, task.id, "test_integrity.flagged") == []


# --- the test-integrity guard ---------------------------------------------------


def _guarded(mode: str, *, parallel: bool = False) -> dict[str, Any]:
    return {
        "execution": {
            "baseline_test_command": "echo ok",
            "auto_merge": True,
            "test_integrity": mode,
            "parallel_reviewers": parallel,
        }
    }


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel"])
async def test_block_flags_a_deleted_test_and_holds_the_merge(
    db: Database, tmp_path: Path, parallel: bool
) -> None:
    provider = FakeProvider(developer_deletes=_EXISTING_TEST)
    orchestrator, project, task, ok = await _run(
        db, tmp_path, provider, _guarded("block", parallel=parallel)
    )

    review_prompts = [p for p in provider.prompts if _REVIEW_PROMPT in p]
    assert len(review_prompts) >= 3  # tester, security, reviewer at least
    for prompt in review_prompts:
        section = prompt.split("TEST INTEGRITY", 1)[1]
        assert f"- {_EXISTING_TEST} · (whole file) · deleted_file" in section
        assert "Return fail unless the task request explicitly asks" in section
    assert await _payloads(db, task.id, "test_integrity.flagged") == [
        {
            "findings": [{"file": _EXISTING_TEST, "test": "", "kind": "deleted_file"}],
            "attempt": 1,
            "mode": "block",
        }
    ]

    # Done on its committed branch, never merged, even though auto_merge is on.
    assert ok is True and task.state is TaskState.done
    assert await _payloads(db, task.id, "task.merged") == []
    assert await _payloads(db, task.id, "task.done") == [{"attempts": 1, "merged": False}]
    repo_path = tmp_path / "repo"
    code, _ = await run_git(repo_path, "show", f"main:{_EXISTING_TEST}")
    assert code == 0  # the deletion did not reach the target branch
    code, _ = await run_git(repo_path, "rev-parse", "--verify", f"conclave/{task.id}")
    assert code == 0
    summary = task.result_summary or ""
    assert "merged=False" in summary
    assert f"test integrity (block): 1 finding(s): {_EXISTING_TEST}" in summary
    assert "conclave_merge_task" in summary

    # A human who reviewed it can still merge it.
    merged = await merge_task(orchestrator, project, task)
    assert merged["merged"] is True
    code, _ = await run_git(repo_path, "show", f"main:{_EXISTING_TEST}")
    assert code != 0


async def test_warn_flags_but_merges(db: Database, tmp_path: Path) -> None:
    provider = FakeProvider(developer_deletes=_EXISTING_TEST)
    _, _, task, ok = await _run(db, tmp_path, provider, _guarded("warn"))

    assert ok is True and task.state is TaskState.done
    flagged = await _payloads(db, task.id, "test_integrity.flagged")
    assert [p["mode"] for p in flagged] == ["warn"]
    assert all("TEST INTEGRITY" in p for p in provider.prompts if _REVIEW_PROMPT in p)
    assert len(await _payloads(db, task.id, "task.merged")) == 1
    code, _ = await run_git(tmp_path / "repo", "show", f"main:{_EXISTING_TEST}")
    assert code != 0
    summary = task.result_summary or ""
    assert "merged=True" in summary and "test integrity (warn)" in summary
    assert "conclave_merge_task" not in summary


async def test_off_neither_flags_nor_holds(db: Database, tmp_path: Path) -> None:
    provider = FakeProvider(developer_deletes=_EXISTING_TEST)
    _, _, task, ok = await _run(db, tmp_path, provider, _guarded("off"))

    assert ok is True and task.state is TaskState.done
    assert not any("TEST INTEGRITY" in p for p in provider.prompts)
    assert await _payloads(db, task.id, "test_integrity.flagged") == []
    assert len(await _payloads(db, task.id, "task.merged")) == 1


async def test_a_change_that_keeps_the_tests_is_not_flagged(
    db: Database, tmp_path: Path
) -> None:
    provider = FakeProvider()
    _, _, task, ok = await _run(db, tmp_path, provider, _guarded("block"))

    assert ok is True and task.state is TaskState.done
    assert not any("TEST INTEGRITY" in p for p in provider.prompts)
    assert await _payloads(db, task.id, "test_integrity.flagged") == []
    assert len(await _payloads(db, task.id, "task.merged")) == 1
