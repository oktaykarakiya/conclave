"""Gate integrity end to end: no silent pass without tests, and the test-integrity guard.

A project without a test command fails its tasks before any agent runs (``no_test_command``)
unless ``execution.allow_no_test_command`` opts out. A developer change that takes pre-existing
tests away is flagged to every reviewer (``TEST INTEGRITY``), reported as one
``test_integrity.flagged`` event per attempt and in the result summary, and under ``block`` is
never merged automatically. Deterministic: a throwaway git repo and the FakeProvider, no LLM.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fake_provider import FakeProvider

from conclave.config import EdgeCondition, NodeKind, WorkflowEdge, WorkflowGraph, WorkflowNode
from conclave.db import Database, Project, Task, TaskState
from conclave.db import repositories as repo
from conclave.engine import Orchestrator, run_git
from conclave.engine.task_merge import merge_task
from conclave.events import EventBus

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
