"""End-to-end orchestrator tests against a throwaway git repo + fake provider."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest
from fake_provider import FakeProvider
from httpx import ASGITransport

from conclave.bootstrap import seed_global_defaults
from conclave.config import load_project_config
from conclave.db import Database, TaskState
from conclave.db import repositories as repo
from conclave.engine import Orchestrator, run_git
from conclave.engine.orchestrator import _check_budget
from conclave.engine.runner import AgentRunner
from conclave.events import EventBus
from conclave.providers import AgentResult, OnChunk, ResolvedProfile
from conclave.runtime import Daemon
from conclave.web import create_app


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
        cancel_event: asyncio.Event | None = None,
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

    failed, feedback, review_timed_out = await orchestrator._review(
        runner, task, project.id, repo_path, "diff --git a/x.py b/x.py\n", 1, config, "", "",
        started=time.monotonic(), budget=0.0,
    )

    assert review_timed_out is False
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
    recovered, reblocked = await orchestrator.recover(project.id)
    assert recovered == 1
    assert reblocked == 0
    again = await repo.get_task(db, claimed.id)
    assert again is not None and again.state is TaskState.approved


async def test_crash_recovery_reblocks_descendants(db: Database, tmp_path: Path) -> None:
    """Orchestrator-level recovery must re-block children of failed/blocked parents.

    After a simulated crash where a parent is ``failed`` and its child is ``approved``,
    calling ``orchestrator.recover()`` leaves the child ``blocked``, while a child of a
    ``done`` parent remains ``approved`` and is claimable.
    """
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )

    # A failed parent with an approved child — the vulnerable state.
    parent = await repo.create_task(
        db, project_id=project.id, request="parent", state=TaskState.approved,
    )
    await repo.set_task_state(db, parent.id, TaskState.failed)
    child = await repo.create_task(
        db, project_id=project.id, request="child", state=TaskState.approved,
        parent_task_id=parent.id,
    )

    # A done parent with an approved child — should stay claimable.
    done_parent = await repo.create_task(
        db, project_id=project.id, request="done-parent", state=TaskState.approved,
    )
    await repo.set_task_state(db, done_parent.id, TaskState.done)
    healthy = await repo.create_task(
        db, project_id=project.id, request="healthy", state=TaskState.approved,
        parent_task_id=done_parent.id,
    )

    orchestrator = Orchestrator(db, EventBus(db), FakeProvider(), tmp_path / "home")
    recovered, reblocked = await orchestrator.recover(project.id)

    # No in_progress tasks to recover.
    assert recovered == 0
    # The failed parent's approved child was blocked.
    assert reblocked == 1

    c = await repo.get_task(db, child.id)
    assert c is not None and c.state is TaskState.blocked

    # The healthy task with a done parent is unaffected.
    h = await repo.get_task(db, healthy.id)
    assert h is not None and h.state is TaskState.approved

    # Only the healthy task is claimable — the blocked child is skipped.
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert claimed.id == healthy.id
    assert await repo.claim_next_approved(db, project.id) is None


# --- ENG-5: merge hardening ---------------------------------------------------


async def test_merge_conflict_detected_by_merge(db: Database, tmp_path: Path) -> None:
    """``_merge`` returns ``MergeResult.conflict`` for a real merge conflict.

    Two branches that both modified the same file differently produce a conflict
    that must be surfaced — never silently swallowed as success."""
    from conclave.engine.orchestrator import MergeResult

    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    _project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )

    # Create a feature branch with a commit modifying FEATURE.txt.
    await run_git(repo_path, "checkout", "-b", "feature")
    (repo_path / "FEATURE.txt").write_text("feature content\n")
    await run_git(repo_path, "add", "-A")
    await run_git(repo_path, "commit", "-m", "feature work")

    # Switch to main and make a conflicting change to the same file.
    await run_git(repo_path, "checkout", "main")
    (repo_path / "FEATURE.txt").write_text("conflicting main content\n")
    await run_git(repo_path, "add", "-A")
    await run_git(repo_path, "commit", "-m", "conflicting main change")

    orchestrator = Orchestrator(db, EventBus(db), FakeProvider(), tmp_path / "home")
    result = await orchestrator._merge(repo_path, "main", "feature", "task-1")
    assert result is MergeResult.conflict


async def test_merge_conflict_marks_task_failed_and_preserves_branch(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``_merge`` reports a conflict, the orchestrator must fail the task
    (not mark it done) and preserve the task branch so the operator can resolve it."""
    from conclave.engine.orchestrator import MergeResult

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

    # Inject a _merge that always returns conflict — simulates the merge failing
    # while keeping the rest of the pipeline (developer → review → gate) intact.
    # (No ``self`` param: monkeypatch.setattr stores the function in the instance
    # __dict__, so Python does NOT auto-bind it as a bound method.)
    async def _fake_merge(
        _repo_path: Path, _target_branch: str, _task_branch: str, _task_id: str,
    ) -> MergeResult:
        return MergeResult.conflict

    monkeypatch.setattr(orchestrator, "_merge", _fake_merge)

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    result = await orchestrator.process_task(claimed)

    # Must signal failure — no silent success on a conflicted merge.
    assert result is False

    final = await repo.get_task(db, task.id)
    assert final is not None
    assert final.state is TaskState.failed
    assert "merge conflict" in (final.result_summary or "")
    assert "main" in (final.result_summary or "")

    # The task branch must survive — work is preserved, not discarded.
    code, branches = await run_git(repo_path, "branch", "--list", f"conclave/{task.id}")
    assert code == 0
    assert f"conclave/{task.id}" in branches

    # Verify the failure event was emitted with a merge-conflict reason.
    events = await repo.list_events(db, task_id=task.id)
    fail_events = [e for e in events if e.type == "task.failed"]
    assert len(fail_events) == 1
    assert fail_events[0].payload.get("reason") == "merge_conflict"


def test_merge_worktree_path_unique_per_task() -> None:
    """``wm_merge_path`` must produce distinct paths for different task ids so
    concurrent merges into the same target never share a worktree directory."""
    from conclave.engine.orchestrator import wm_merge_path

    p1 = wm_merge_path(Path("/repo"), "main", "task-abc-123")
    p2 = wm_merge_path(Path("/repo"), "main", "task-xyz-456")
    assert p1 != p2
    assert "task-abc-123" in str(p1)
    assert "task-xyz-456" in str(p2)

    # Same task id, different targets → different paths.
    p3 = wm_merge_path(Path("/repo"), "develop", "task-1")
    p4 = wm_merge_path(Path("/repo"), "main", "task-1")
    assert p3 != p4


# --- ENG-6: venv guidance + setup timeout -----------------------------------


async def test_setup_command_creates_venv_guidance_in_developer_prompt(
    db: Database, tmp_path: Path,
) -> None:
    """When setup_command provisions .venv/, the developer agent sees venv guidance
    that reflects the configured test command — not hard-coded pytest/mypy/ruff."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={
            "execution": {
                "target_branch": "main",
                "setup_command": "mkdir -p .venv && echo ok",
                # Use a test command that actually passes so the green-gate
                # succeeds.  The command is crafted to look like a real test
                # runner (pytest) but just echoes success.
                "baseline_test_command": "echo '0 passed'",
            },
        },
    )
    await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )

    provider = FakeProvider()
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True

    # The developer agent's prompt should contain the derived venv guidance
    # with the ACTUAL test command — not hard-coded pytest/mypy/ruff.
    dev_prompts = [
        p for p in provider.prompts
        if "Review the changes made for this task" not in p
        and "Produce a structured plan" not in p
        and "Repository Analysis" not in p
    ]
    assert len(dev_prompts) >= 1
    full_prompt = "\n".join(dev_prompts)
    assert "Worktree environment (MANDATORY)" in full_prompt
    assert ".venv/bin/echo" in full_prompt
    # Must NOT contain the old hard-coded lines.
    assert "`.venv/bin/pytest -q`" not in full_prompt
    assert "`.venv/bin/mypy`" not in full_prompt
    assert "Do NOT use system-wide `pytest`/`mypy`/`ruff`" not in full_prompt


async def test_setup_timeout_seconds_from_config_is_passed_to_run_shell(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``setup_timeout_seconds`` config field is read and passed to ``run_shell``."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={
            "execution": {
                "target_branch": "main",
                "setup_command": "mkdir -p .venv && echo ok",
                "setup_timeout_seconds": 42,
            },
        },
    )
    await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )

    captured_timeouts: list[int] = []

    # run_shell is imported directly into orchestrator.py (from .gitio import
    # run_shell), so the reference lives on the orchestrator module — monkeypatch
    # there, not on gitio.
    from conclave.engine import orchestrator as orch_mod

    _original = orch_mod.run_shell

    async def _spy(cwd: Path, command: str, *, env=None, timeout_seconds=None):
        captured_timeouts.append(timeout_seconds)
        return await _original(cwd, command, env=env, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(orch_mod, "run_shell", _spy)

    provider = FakeProvider()
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True

    # At least one call to run_shell for the setup step.
    assert len(captured_timeouts) >= 1
    # The captured timeout should match the configured value (42), not the old 900.
    assert any(t == 42 for t in captured_timeouts), (
        f"Expected 42 in captured timeouts, got {captured_timeouts}"
    )


async def test_no_venv_guidance_when_setup_does_not_create_venv(
    db: Database, tmp_path: Path,
) -> None:
    """When setup_command runs but does NOT create .venv/, no venv guidance is injected."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={
            "execution": {
                "target_branch": "main",
                # This setup_command succeeds but does not create a .venv directory.
                "setup_command": "echo 'provisioning done'",
            },
        },
    )
    await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )

    provider = FakeProvider()
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True

    # No prompt should contain venv guidance.
    for p in provider.prompts:
        assert "Worktree environment (MANDATORY)" not in p


# --- Mid-attempt timeout ------------------------------------------------------


async def test_mid_attempt_timeout(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wall-clock hard cap must abort the task mid-attempt — not just at the
    top of the attempt loop. We use a monkeypatched ``_check_budget`` that returns
    True after the developer dispatch to simulate a budget being exhausted during
    an agent call, avoiding timing-sensitive sleeps."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={
            "execution": {
                "target_branch": "main",
                # Any positive budget enables the check; the monkeypatch overrides it.
                "wall_clock_budget_minutes": 1,
            },
            "agent_overrides": {"developer": {"max_retries": 1}},
        },
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )

    # Let _check_budget return True only after the developer has run — but return
    # False for the top-of-loop and pre-developer checks so the developer actually
    # executes. We want to catch the mid-attempt check inside _review().
    original = _check_budget
    call_count = [0]

    def _timeout_after_developer(started: float, budget_minutes: float) -> bool:
        call_count[0] += 1
        # First few calls (top-of-loop, pre-developer): return False so the
        # developer gets dispatched. After that, return True to simulate the
        # budget being exhausted during the developer's long run.
        if call_count[0] <= 2:
            return False
        return True

    monkeypatch.setattr(
        "conclave.engine.orchestrator._check_budget", _timeout_after_developer
    )

    orchestrator = Orchestrator(db, EventBus(db), FakeProvider(), tmp_path / "home")

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    result = await orchestrator.process_task(claimed)

    # The task must be failed, not completed — the timeout prevented completion.
    assert result is False

    final = await repo.get_task(db, task.id)
    assert final is not None
    assert final.state is TaskState.failed
    assert "timeout" in (final.result_summary or "")

    # The task_failed event must carry reason='timeout'.
    events = await repo.list_events(db, task_id=task.id)
    fail_events = [e for e in events if e.type == "task.failed"]
    assert len(fail_events) >= 1
    assert any(e.payload.get("reason") == "timeout" for e in fail_events)

    # The reviewers should not have been dispatched because _review returned
    # timed_out=True before entering the reviewer loop.
    verdicts = await repo.list_verdicts(db, task.id)
    assert len(verdicts) == 0, (
        "No reviewer should have been dispatched after the timeout fired"
    )

    # Restore original so other tests aren't affected.
    monkeypatch.setattr(
        "conclave.engine.orchestrator._check_budget", original
    )


# --- Cooperative cancellation --------------------------------------------------


async def test_cancel_in_progress_task_sets_cancelled_and_cleans_worktree(
    db: Database, tmp_path: Path,
) -> None:
    """Cancelling an in_progress task before it dispatches any agent must transition
    it to cancelled, clean its worktree, and return False (worker stays alive)."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved,
    )
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None

    orchestrator = Orchestrator(db, EventBus(db), FakeProvider(), tmp_path / "home")
    # Pre-set the cancel event so the orchestrator detects it at the first check point
    # (after worktree setup, before baseline).
    cancel_event = asyncio.Event()
    cancel_event.set()

    result = await orchestrator.process_task(claimed, cancel_event=cancel_event)
    assert result is False

    final = await repo.get_task(db, task.id)
    assert final is not None
    assert final.state is TaskState.cancelled

    # Worktree must be cleaned — the path must not appear in ``git worktree list``.
    worktree_path = str(
        tmp_path / "home" / "projects" / project.id / "worktrees" / task.id
    )
    _, wt_list = await run_git(repo_path, "worktree", "list")
    assert worktree_path not in wt_list

    # Verify the cancellation event was emitted.
    events = await repo.list_events(db, task_id=task.id)
    cancel_events = [e for e in events if e.type == "task.cancelled"]
    assert len(cancel_events) == 1


async def test_cancel_event_checked_between_stages(
    db: Database, tmp_path: Path,
) -> None:
    """A blocking developer dispatch interrupted by cancellation must be detected
    at the post-developer check point (before review). The task must transition
    to cancelled and the worktree must be cleaned."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved,
    )
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None

    # A provider whose developer dispatch blocks until the cancel event is set,
    # while planner and reviewer dispatch pass through instantly.
    class _BlockingDeveloper:
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
            if "Produce a structured plan" in prompt:
                return AgentResult(ok=True, text="{}", model_reported="fake")
            if "Review the changes" in prompt:
                return AgentResult(
                    ok=True,
                    text='```json\n{"verdict": "pass", "reason": "ok", "evidence": []}\n```',
                    model_reported="fake",
                )
            # Developer: block until cancel_event is set, then return success.
            if cancel_event is not None:
                await cancel_event.wait()
            return AgentResult(ok=True, text="done", model_reported="fake")

    orchestrator = Orchestrator(db, EventBus(db), _BlockingDeveloper(), tmp_path / "home")
    cancel_event = asyncio.Event()

    # Run process_task in the background so we can signal cancellation mid-flight.
    async def _run() -> bool:
        return await orchestrator.process_task(claimed, cancel_event=cancel_event)

    proc_task = asyncio.create_task(_run())

    # Give the orchestrator time to dispatch the developer and enter the blocking wait.
    await asyncio.sleep(0.2)

    # Signal cancellation — the blocking developer will return, then the orchestrator
    # checks at the post-developer check point and calls _finish_cancelled.
    cancel_event.set()

    result = await asyncio.wait_for(proc_task, timeout=10.0)
    assert result is False

    final = await repo.get_task(db, task.id)
    assert final is not None
    assert final.state is TaskState.cancelled

    # Worktree cleaned.
    worktree_path = str(
        tmp_path / "home" / "projects" / project.id / "worktrees" / task.id
    )
    _, wt_list = await run_git(repo_path, "worktree", "list")
    assert worktree_path not in wt_list


async def test_cancel_on_final_attempt_lands_in_cancelled_not_failed(
    db: Database, tmp_path: Path,
) -> None:
    """Cancelling during the FINAL review attempt must land the task in ``cancelled``,
    never ``failed``.

    With ``max_retries=1`` the develop→review loop runs exactly once. A developer that
    sets the cancel event after writing its change forces ``_review``'s per-stage check to
    fire: it returns ``failed=True`` and the loop exits at the bottom. The orchestrator
    must recognise the pending cancellation there and finish as cancelled.
    """
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={
            "execution": {"target_branch": "main"},
            "agent_overrides": {"developer": {"max_retries": 1}},
        },
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved,
    )
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None

    cancel_event = asyncio.Event()

    class _CancelDuringReview:
        """Developer writes its change then trips the cancel event; review then sees it."""

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
            if "Produce a structured plan" in prompt:
                return AgentResult(ok=True, text="{}", model_reported="fake")
            if "Review the changes" in prompt:
                # Should never be reached: the cancel check inside _review fires first.
                return AgentResult(
                    ok=True,
                    text='```json\n{"verdict": "pass", "reason": "ok", "evidence": []}\n```',
                    model_reported="fake",
                )
            # Developer: write the change, then signal cancellation so the post-developer
            # and in-review checks observe it on this final attempt.
            if cwd is not None:
                (Path(cwd) / "FEATURE.txt").write_text("done\n", encoding="utf-8")
            cancel_event.set()
            return AgentResult(ok=True, text="done", model_reported="fake")

    orchestrator = Orchestrator(db, EventBus(db), _CancelDuringReview(), tmp_path / "home")

    result = await orchestrator.process_task(claimed, cancel_event=cancel_event)
    assert result is False

    final = await repo.get_task(db, task.id)
    assert final is not None
    assert final.state is TaskState.cancelled, (
        f"expected cancelled, got {final.state} (summary: {final.result_summary!r})"
    )

    # The terminal event must be a cancellation, not a failure.
    events = await repo.list_events(db, task_id=task.id)
    types = {e.type for e in events}
    assert "task.cancelled" in types
    assert "task.failed" not in types


async def test_cancel_non_in_progress_task_via_api(
    db: Database, tmp_path: Path,
) -> None:
    """Cancelling inbox/approved transitions to cancelled; cancelling terminal
    states returns 409."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    await seed_global_defaults(db)

    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    transport = ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        try:
            created = await client.post(
                "/api/projects",
                json={"name": "t", "path": str(repo_path), "default_branch": "main"},
            )
            assert created.status_code == 200
            project_id = created.json()["id"]

            # Cancel an inbox task → 200, state→cancelled.
            t1 = await client.post(
                f"/api/projects/{project_id}/tasks",
                json={"request": "x", "auto_approve": False},
            )
            t1_id = t1.json()["id"]
            r1 = await client.post(f"/api/tasks/{t1_id}/cancel")
            assert r1.status_code == 200
            assert r1.json()["cancelled"] is True
            state1 = (await client.get(f"/api/tasks/{t1_id}")).json()["state"]
            assert state1 == "cancelled"

            # Cancel an approved task → 200, state→cancelled.
            t2 = await client.post(
                f"/api/projects/{project_id}/tasks",
                json={"request": "y", "auto_approve": True},
            )
            t2_id = t2.json()["id"]
            r2 = await client.post(f"/api/tasks/{t2_id}/cancel")
            assert r2.status_code == 200
            assert r2.json()["cancelled"] is True
            state2 = (await client.get(f"/api/tasks/{t2_id}")).json()["state"]
            assert state2 == "cancelled"

            # Manually set a task to done, then try to cancel → 409.
            t3 = await client.post(
                f"/api/projects/{project_id}/tasks",
                json={"request": "z", "auto_approve": True},
            )
            t3_id = t3.json()["id"]
            await repo.set_task_state(db, t3_id, TaskState.done)
            r3 = await client.post(f"/api/tasks/{t3_id}/cancel")
            assert r3.status_code == 409

            # Already cancelled → 409.
            r4 = await client.post(f"/api/tasks/{t1_id}/cancel")
            assert r4.status_code == 409
        finally:
            await daemon.shutdown()


async def test_cancel_via_api_endpoint(
    db: Database, tmp_path: Path,
) -> None:
    """POST /api/tasks/{id}/cancel on an in_progress task triggers daemon.request_cancel
    and returns cancelled=true."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    await seed_global_defaults(db)

    # A provider whose developer dispatch blocks until explicitly released.
    # Planner and reviewer prompts pass through instantly; AI enrichment is
    # handled so onboarding doesn't block.
    _PASS = (
        '```json\n{"verdict": "pass", "reason": "looks correct", "evidence": []}\n```'
    )
    _AI = (
        '```json\n'
        '{"languages": [], "frameworks": [], '
        '"commands": {}, '
        '"architecture_summary": "A minimal git repository with a README.", '
        '"conventions": [], "protected_globs": [], '
        '"layout": {"dirs": []}}\n'
        '```'
    )

    class _BlockingProvider:
        def __init__(self) -> None:
            self._release = asyncio.Event()

        def release(self) -> None:
            self._release.set()

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
            if "Produce a structured plan" in prompt:
                return AgentResult(ok=True, text="{}", model_reported="fake")
            if "Review the changes" in prompt:
                return AgentResult(ok=True, text=_PASS, model_reported="fake")
            if "Repository Analysis" in prompt and "AI Enrichment" in prompt:
                return AgentResult(ok=True, text=_AI, model_reported="fake")
            # Developer dispatch — block until released so the cancel request
            # can race against the running task.
            await self._release.wait()
            if cwd is not None:
                (Path(cwd) / "FEATURE.txt").write_text("done\n", encoding="utf-8")
            return AgentResult(
                ok=True, text="Implemented the change.", model_reported="fake",
            )

    blocking = _BlockingProvider()
    daemon = Daemon(db, tmp_path / "home", blocking, workers_enabled=True)
    app = create_app(daemon, manage_lifecycle=False)
    transport = ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        try:
            created = await client.post(
                "/api/projects",
                json={"name": "t", "path": str(repo_path), "default_branch": "main"},
            )
            assert created.status_code == 200
            project_id = created.json()["id"]

            task = await client.post(
                f"/api/projects/{project_id}/tasks",
                json={"request": "add a feature file", "auto_approve": True},
            )
            task_id = task.json()["id"]

            # Wait for the worker to claim the task (in_progress) and dispatch
            # the developer (which will block on _release).
            state = ""
            for _ in range(150):
                state = (await client.get(f"/api/tasks/{task_id}")).json()["state"]
                if state == "in_progress":
                    break
                await asyncio.sleep(0.1)
            assert state == "in_progress", f"expected in_progress, got {state!r}"

            # Give the orchestrator a moment to reach the developer dispatch
            # and enter the blocking wait.
            await asyncio.sleep(0.2)

            # Cancel via the endpoint — this sets the cancel_event on the
            # orchestrator. The developer is still blocked, so the cancel
            # signal will be detected at CHECKPOINT 4 after the developer
            # returns.
            cancel_resp = await client.post(f"/api/tasks/{task_id}/cancel")
            assert cancel_resp.status_code == 200
            assert cancel_resp.json()["cancelled"] is True

            # Release the developer so the orchestrator can proceed to the
            # post-developer check point and detect the cancellation.
            blocking.release()

            # Wait for the worker to process the cancellation.
            for _ in range(150):
                state = (await client.get(f"/api/tasks/{task_id}")).json()["state"]
                if state in ("cancelled", "done", "failed"):
                    break
                await asyncio.sleep(0.1)
            assert state == "cancelled", f"expected cancelled, got {state!r}"

            # Verify the task_cancelled event was emitted.
            events_resp = await client.get(f"/api/tasks/{task_id}/events")
            events = events_resp.json()
            assert any(e["type"] == "task.cancelled" for e in events)
        finally:
            await daemon.shutdown()
