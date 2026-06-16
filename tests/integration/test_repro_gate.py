"""End-to-end tests for the Bug-Fixer reproduction gate (``reproduce_bug``).

The gate proves a discovered candidate by running its synthesized test against a clean throwaway
worktree at the unfixed checkpoint and DEMANDING a real assertion failure. These tests exercise
the gate through its real plumbing — a throwaway git repo, the shared :class:`WorktreeManager`,
and :func:`run_tests` driving REAL ``pytest`` over a tiny written test file (the "write a real
failing file" option) — so every classification path is proven against actual behaviour, not a
stubbed gate:

* (a) a test that fails on the unfixed code  → ``reproduced`` (+ pinned path/body/hash).
* (b) a test that passes on the unfixed code → ``dismissed_false_positive``.
* (c) an infra outcome (missing command)     → NEITHER reproduced nor dismissed.
* (d) a path the bf-repro-pathguard rejects   → short-circuits before any worktree or write.

Plus the heightened-scrutiny route (a covered-behaviour change is held for a human) and the
eligibility guard (only a ``discovered`` candidate is acted on). All deterministic and LLM-free.
"""

from __future__ import annotations

import hashlib
import shlex
import sys
from pathlib import Path

from conclave.config import ConclaveConfig
from conclave.db import BugCandidate, BugStatus, Database, EventRow, Project
from conclave.db import repositories as repo
from conclave.engine import ReproOutcome, ReproTest, WorktreeManager, reproduce_bug
from conclave.engine.gitio import run_git
from conclave.events import EventBus, EventType

_FAILING = "def test_repro_target() -> None:\n    assert 1 == 2\n"
_PASSING = "def test_repro_target() -> None:\n    assert 1 == 1\n"
_REPRO_PATH = "tests/repro/test_target.py"


async def _init_repo(path: Path) -> None:
    """A throwaway git repo on ``main`` with a single commit — the unfixed checkpoint."""
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    await run_git(path, "config", "user.email", "test@example.com")
    await run_git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# test repo\n")
    await run_git(path, "add", "-A")
    await run_git(path, "commit", "-m", "initial commit")


async def _make(
    db: Database, tmp_path: Path
) -> tuple[Project, ConclaveConfig, WorktreeManager, BugCandidate]:
    """Stand up a repo + project + a fresh ``discovered`` candidate, and a WorktreeManager."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main"
    )
    config = ConclaveConfig()  # execution.target_branch defaults to "main"
    wm = WorktreeManager(repo_path, tmp_path / "home" / "projects" / project.id / "worktrees")
    candidate = await repo.create_bug_candidate(
        db,
        project_id=project.id,
        fingerprint="fp-1",
        claim="f returns the wrong value for empty input",
        file="src/app.py",
        symbol="f",
        region="src",
    )
    return project, config, wm, candidate


def _pytest_command(repro_path: str) -> str:
    """Run JUST the repro test with the same interpreter running this suite.

    ``sys.executable`` guarantees the subprocess interpreter has pytest installed; the throwaway
    worktree carries no pytest config, so the run is independent of this project's settings.
    """
    return (
        f"{shlex.quote(sys.executable)} -m pytest "
        f"{shlex.quote(repro_path)} -p no:cacheprovider -q"
    )


def _events(rows: list[EventRow], event_type: EventType) -> list[EventRow]:
    return [e for e in rows if e.type == event_type]


# --- (a) fail-on-unfixed → reproduced ---------------------------------------


async def test_failing_test_reproduces_and_pins(db: Database, tmp_path: Path) -> None:
    """A test that genuinely fails on the unfixed code advances the candidate to ``reproduced``.

    REAL pytest over a written ``assert 1 == 2`` file yields a real assertion failure; the gate
    pins the proven path/body and a SHA-256 of the body (bf-integrity-repro-pin) and announces
    exactly one ``bug.reproduced``.
    """
    project, config, wm, candidate = await _make(db, tmp_path)
    bus = EventBus(db)
    repro = ReproTest(path=_REPRO_PATH, body=_FAILING)

    result = await reproduce_bug(
        db, bus, project, config, candidate, repro,
        wm=wm, test_command=_pytest_command(_REPRO_PATH),
    )

    assert result.outcome is ReproOutcome.reproduced
    assert result.candidate.status is BugStatus.reproduced
    # The proven artifact is pinned: vetted path, verbatim body, and a body hash for re-verify.
    assert result.candidate.repro_test_path == _REPRO_PATH
    assert result.candidate.repro_test_body == _FAILING
    assert result.candidate.repro_test_hash == hashlib.sha256(_FAILING.encode("utf-8")).hexdigest()

    # The persisted row matches what the gate returned.
    stored = await repo.get_bug_candidate(db, candidate.id)
    assert stored is not None and stored.status is BugStatus.reproduced
    assert stored.repro_test_hash == result.candidate.repro_test_hash

    # Exactly one bug.reproduced, carrying the path + hash but never the body.
    events = await repo.list_events(db, project_id=project.id)
    reproduced = _events(events, EventType.bug_reproduced)
    assert len(reproduced) == 1
    assert reproduced[0].payload["candidate_id"] == candidate.id
    assert reproduced[0].payload["repro_test_path"] == _REPRO_PATH
    assert reproduced[0].payload["repro_test_hash"] == result.candidate.repro_test_hash
    assert "body" not in reproduced[0].payload

    # The throwaway worktree is cleaned up and the operator's checkout was never touched.
    assert not wm.path_for(f"repro-{candidate.id}").exists()
    assert not (Path(project.path) / _REPRO_PATH).exists()


# --- (b) pass-on-unfixed → dismissed ----------------------------------------


async def test_passing_test_dismisses_as_false_positive(db: Database, tmp_path: Path) -> None:
    """A test that already PASSES on the unfixed code dismisses the claim as a false positive."""
    project, config, wm, candidate = await _make(db, tmp_path)
    bus = EventBus(db)
    repro = ReproTest(path=_REPRO_PATH, body=_PASSING)

    result = await reproduce_bug(
        db, bus, project, config, candidate, repro,
        wm=wm, test_command=_pytest_command(_REPRO_PATH),
    )

    assert result.outcome is ReproOutcome.dismissed
    assert result.candidate.status is BugStatus.dismissed_false_positive
    # No reproduction was proven, so nothing is pinned.
    assert result.candidate.repro_test_path is None
    assert result.candidate.repro_test_hash is None

    events = await repo.list_events(db, project_id=project.id)
    assert len(_events(events, EventType.bug_dismissed)) == 1
    assert _events(events, EventType.bug_reproduced) == []
    assert not wm.path_for(f"repro-{candidate.id}").exists()


# --- (c) infra outcome → neither --------------------------------------------


async def test_infra_outcome_is_neither(db: Database, tmp_path: Path) -> None:
    """A missing test command is infra noise (127), not proof — the candidate is untouched.

    The body WOULD fail (``assert 1 == 2``), proving the gate never mistakes a toolchain failure
    for a reproduction even when the test itself would have failed.
    """
    project, config, wm, candidate = await _make(db, tmp_path)
    bus = EventBus(db)
    repro = ReproTest(path=_REPRO_PATH, body=_FAILING)

    result = await reproduce_bug(
        db, bus, project, config, candidate, repro,
        wm=wm, test_command="conclave-nonexistent-cmd-xyzzy",
    )

    assert result.outcome is ReproOutcome.infra
    # No transition: still discovered, nothing pinned.
    assert result.candidate.status is BugStatus.discovered
    stored = await repo.get_bug_candidate(db, candidate.id)
    assert stored is not None and stored.status is BugStatus.discovered
    assert stored.repro_test_path is None

    events = await repo.list_events(db, project_id=project.id)
    assert _events(events, EventType.bug_reproduced) == []
    assert _events(events, EventType.bug_dismissed) == []
    assert not wm.path_for(f"repro-{candidate.id}").exists()


# --- (d) path-guard rejection short-circuits before any write ---------------


async def test_pathguard_rejection_short_circuits_before_any_write(
    db: Database, tmp_path: Path
) -> None:
    """A traversal path is rejected BEFORE any worktree is created or any file is written."""
    project, config, wm, candidate = await _make(db, tmp_path)
    bus = EventBus(db)
    # Constructed directly (bypassing parse_repro_test) to exercise the gate's defense-in-depth
    # re-validation: a path that escapes the worktree must sink the run before it touches disk.
    repro = ReproTest(path="../../../etc/test_evil.py", body=_FAILING)

    result = await reproduce_bug(
        db, bus, project, config, candidate, repro,
        wm=wm, test_command=_pytest_command(_REPRO_PATH),
    )

    assert result.outcome is ReproOutcome.rejected
    assert result.candidate.status is BugStatus.discovered  # untouched
    # Short-circuited before WorktreeManager.create ran: no worktree, no worktrees root at all.
    assert not wm.path_for(f"repro-{candidate.id}").exists()
    assert not (tmp_path / "home").exists()

    events = await repo.list_events(db, project_id=project.id)
    assert _events(events, EventType.bug_reproduced) == []
    assert _events(events, EventType.bug_dismissed) == []


# --- heightened scrutiny: covered-behaviour change → needs a human ----------


async def test_covered_behavior_change_is_declined_not_auto_reproduced(
    db: Database, tmp_path: Path
) -> None:
    """A genuine failure that asserts a covered-behaviour change routes to a human, not a fix."""
    project, config, wm, candidate = await _make(db, tmp_path)
    bus = EventBus(db)
    repro = ReproTest(path=_REPRO_PATH, body=_FAILING)

    result = await reproduce_bug(
        db, bus, project, config, candidate, repro,
        wm=wm, test_command=_pytest_command(_REPRO_PATH),
        asserts_covered_behavior_change=True,
    )

    assert result.outcome is ReproOutcome.declined
    assert result.candidate.status is BugStatus.declined_needs_human
    assert result.candidate.decline_reason  # a human-handoff note rides along
    # Not auto-reproduced and not pinned — a human confirms it first.
    assert result.candidate.repro_test_path is None

    events = await repo.list_events(db, project_id=project.id)
    assert len(_events(events, EventType.bug_declined)) == 1
    assert _events(events, EventType.bug_reproduced) == []


# --- eligibility guard ------------------------------------------------------


async def test_non_discovered_candidate_is_ineligible(db: Database, tmp_path: Path) -> None:
    """The gate only acts on a ``discovered`` candidate; anything else is a no-op."""
    project, config, wm, candidate = await _make(db, tmp_path)
    # Park the candidate so it is no longer in the actionable `discovered` state.
    deferred = await repo.transition_bug_status(db, candidate.id, BugStatus.deferred)
    assert deferred.status is BugStatus.deferred
    bus = EventBus(db)
    repro = ReproTest(path=_REPRO_PATH, body=_FAILING)

    result = await reproduce_bug(
        db, bus, project, config, deferred, repro,
        wm=wm, test_command=_pytest_command(_REPRO_PATH),
    )

    assert result.outcome is ReproOutcome.ineligible
    stored = await repo.get_bug_candidate(db, candidate.id)
    assert stored is not None and stored.status is BugStatus.deferred  # untouched
    assert not wm.path_for(f"repro-{candidate.id}").exists()
    events = await repo.list_events(db, project_id=project.id)
    assert _events(events, EventType.bug_reproduced) == []
