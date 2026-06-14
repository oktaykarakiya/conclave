"""End-to-end orchestrator tests against a throwaway git repo + fake provider."""

from __future__ import annotations

from pathlib import Path

import pytest
from fake_provider import FakeProvider

from conclave.config import load_project_config
from conclave.db import Database, TaskState
from conclave.db import repositories as repo
from conclave.engine import Orchestrator, run_git
from conclave.engine.runner import AgentRunner
from conclave.events import EventBus
from conclave.providers import AgentResult, OnChunk, ResolvedProfile


async def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    await run_git(path, "config", "user.email", "test@example.com")
    await run_git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# test repo\n")
    await run_git(path, "add", "-A")
    await run_git(path, "commit", "-m", "initial commit")


class _EmptyReviewerProvider:
    """Provider whose every dispatch comes back empty (simulated backend outage).

    Used to drive ``_review`` directly so each reviewer falls back to a non-blocking
    'unknown' (ENG-4) while the all-'unknown' round still fails the attempt (ENG-3).
    """

    async def run_agent(
        self,
        *,
        profile: ResolvedProfile,
        prompt: str,
        timeout_seconds: int,
        cwd: Path | None = None,
        on_chunk: OnChunk | None = None,
    ) -> AgentResult:
        return AgentResult(ok=False, text="", error="backend unavailable")


async def test_happy_path_commits_and_merges(db: Database, tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )
    orchestrator = Orchestrator(db, EventBus(db), FakeProvider(), tmp_path / "home")

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True

    done = await repo.get_task(db, task.id)
    assert done is not None and done.state is TaskState.done

    code, out = await run_git(repo_path, "show", "main:FEATURE.txt")
    assert code == 0 and "done" in out

    verdicts = await repo.list_verdicts(db, task.id)
    assert {v.agent for v in verdicts} >= {"tester", "security", "reviewer"}
    assert all(v.verdict == "pass" for v in verdicts)

    types = {e.type for e in await repo.list_events(db, task_id=task.id)}
    assert {"task.started", "task.committed", "task.merged", "task.done"} <= types


async def test_green_gate_passes_after_developer_change(db: Database, tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={
            "execution": {"target_branch": "main", "baseline_test_command": "test -f FEATURE.txt"}
        },
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add feature", state=TaskState.approved
    )
    orchestrator = Orchestrator(
        db, EventBus(db), FakeProvider(developer_writes=True), tmp_path / "home"
    )
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True
    done = await repo.get_task(db, task.id)
    assert done is not None and done.state is TaskState.done


async def test_failure_when_gate_never_green(db: Database, tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={
            "execution": {"target_branch": "main", "baseline_test_command": "test -f FEATURE.txt"},
            "agent_overrides": {"developer": {"max_retries": 2}},
        },
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add feature", state=TaskState.approved
    )
    # developer never writes the file => gate stays red => fail after retries
    orchestrator = Orchestrator(
        db, EventBus(db), FakeProvider(developer_writes=False), tmp_path / "home"
    )
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is False

    failed = await repo.get_task(db, task.id)
    assert failed is not None and failed.state is TaskState.failed
    # the failed task branch was cleaned up
    _, branches = await run_git(repo_path, "branch", "--list", f"conclave/{task.id}")
    assert branches.strip() == ""


async def test_reviewer_edits_are_not_committed(db: Database, tmp_path: Path) -> None:
    # ENG-2: reviewers run with --dangerously-skip-permissions and *can* write to the
    # worktree, but only the reviewed tree may be gated/committed. With a tampering
    # reviewer, neither the stray file nor the clobbered content may survive into the
    # committed/merged tree, while the developer's change must.
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )
    orchestrator = Orchestrator(
        db, EventBus(db), FakeProvider(reviewer_tampers=True), tmp_path / "home"
    )

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True

    done = await repo.get_task(db, task.id)
    assert done is not None and done.state is TaskState.done

    # The developer's content survives; the reviewer's clobber does not.
    code, out = await run_git(repo_path, "show", "main:FEATURE.txt")
    assert code == 0 and "done" in out and "tampered" not in out

    # The reviewer's stray file never entered the committed/merged tree.
    code, _ = await run_git(repo_path, "show", "main:STRAY_REVIEWER.txt")
    assert code != 0


async def test_review_all_empty_reviewers_record_unknown_and_block(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ENG-4 + ENG-3: when every reviewer dispatch comes back empty (a backend outage),
    # _dispatch_reviewer exhausts its retries and each reviewer is recorded as a
    # NON-BLOCKING 'unknown' (source 'none'). The round as a whole still fails the attempt
    # (no usable PASS) so unvetted code is never merged — the all-'unknown' guard holds.
    monkeypatch.setattr("conclave.engine.orchestrator._REVIEWER_RETRY_BACKOFF_S", 0.0)
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )
    config = load_project_config(project.config)
    runner = AgentRunner(db, EventBus(db), _EmptyReviewerProvider(), project.id, config)
    orchestrator = Orchestrator(db, EventBus(db), _EmptyReviewerProvider(), tmp_path / "home")

    failed, feedback = await orchestrator._review(
        runner, task, project.id, repo_path, "diff --git a/x.py b/x.py\n", 1, config, "", ""
    )

    assert failed is True
    assert "REVIEW INCONCLUSIVE" in feedback

    verdicts = await repo.list_verdicts(db, task.id)
    assert {v.agent for v in verdicts} == {"tester", "security", "reviewer"}
    assert all(v.verdict == "unknown" and v.source == "none" for v in verdicts)


async def test_exception_mid_process_task_fails_task_and_cleans_worktree(
    db: Database, tmp_path: Path,
) -> None:
    """An unexpected exception after worktree creation must fail the task and
    remove the worktree — never strand a task in_progress or leak a worktree."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )
    orchestrator = Orchestrator(
        db, EventBus(db), FakeProvider(), tmp_path / "home"
    )

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None

    # Inject an exception mid-processing — simulates an unexpected runtime error
    # after the worktree has been created and the task is in_progress.
    async def _explode(*args: object, **kwargs: object) -> str:
        raise RuntimeError("injected failure")

    orchestrator._baseline = _explode  # type: ignore[method-assign]

    result = await orchestrator.process_task(claimed)
    assert result is False

    # Task must be failed, NOT stranded in_progress.
    final = await repo.get_task(db, task.id)
    assert final is not None
    assert final.state is TaskState.failed

    # Worktree must be cleaned up — the task's worktree path must not appear in
    # ``git worktree list`` output. The path is rooted at the orchestrator home.
    worktree_path = str(
        tmp_path / "home" / "projects" / project.id / "worktrees" / task.id
    )
    _, wt_list = await run_git(repo_path, "worktree", "list")
    assert worktree_path not in wt_list


async def test_crash_recovery_returns_in_progress_to_approved(db: Database, tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )
    await repo.create_task(db, project_id=project.id, request="x", state=TaskState.approved)
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None and claimed.state is TaskState.in_progress

    orchestrator = Orchestrator(db, EventBus(db), FakeProvider(), tmp_path / "home")
    assert await orchestrator.recover(project.id) == 1
    again = await repo.get_task(db, claimed.id)
    assert again is not None and again.state is TaskState.approved
