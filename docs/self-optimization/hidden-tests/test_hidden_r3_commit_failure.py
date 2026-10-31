"""HIDDEN acceptance tests — bake-off round 3 (commit failure must not lose work)."""

from __future__ import annotations

from pathlib import Path

from fake_provider import FakeProvider

from conclave.db import Database, TaskState
from conclave.db import repositories as repo
from conclave.engine import Orchestrator, run_git
from conclave.events import EventBus


async def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    await run_git(path, "config", "user.email", "test@example.com")
    await run_git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# test repo\n")
    await run_git(path, "add", "-A")
    await run_git(path, "commit", "-m", "initial commit")


def _install_rejecting_hook(repo_path: Path) -> None:
    hook = repo_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'POLICY: commits are frozen' >&2\nexit 1\n")
    hook.chmod(0o755)


async def test_rejected_commit_fails_task_and_preserves_work(db: Database, tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    _install_rejecting_hook(repo_path)
    _, main_before = await run_git(repo_path, "rev-parse", "main")
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},  # auto_merge defaults to True
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved,
    )
    home = tmp_path / "home"
    orchestrator = Orchestrator(db, EventBus(db), FakeProvider(), home)
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None

    assert await orchestrator.process_task(claimed) is False

    final = await repo.get_task(db, task.id)
    assert final is not None and final.state is TaskState.failed
    summary = final.result_summary or ""
    assert summary.startswith("commit failed"), summary
    assert "POLICY: commits are frozen" in summary
    assert len(summary) <= 500 + 400  # bounded output tail plus a short prefix/path

    fails = [e for e in await repo.list_events(db, task_id=task.id) if e.type == "task.failed"]
    assert len(fails) == 1 and fails[0].payload.get("reason") == "commit_failed"

    _, main_after = await run_git(repo_path, "rev-parse", "main")
    assert main_after == main_before
    code, _ = await run_git(repo_path, "show", "main:FEATURE.txt")
    assert code != 0

    # The work survives in the preserved worktree, and the branch still exists.
    survivors = list((home / "projects" / project.id / "worktrees").rglob("FEATURE.txt"))
    assert survivors and survivors[0].read_text() == "done\n"
    assert str(survivors[0].parent) in summary
    _, branches = await run_git(repo_path, "branch", "--list", f"conclave/{task.id}")
    assert f"conclave/{task.id}" in branches


async def test_normal_commit_still_merges(db: Database, tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved,
    )
    orchestrator = Orchestrator(db, EventBus(db), FakeProvider(), tmp_path / "home")
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True
    done = await repo.get_task(db, task.id)
    assert done is not None and done.state is TaskState.done
    code, out = await run_git(repo_path, "show", "main:FEATURE.txt")
    assert code == 0 and "done" in out
