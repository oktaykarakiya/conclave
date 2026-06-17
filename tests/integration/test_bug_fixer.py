"""End-to-end tests for the Bug-Fixer MODE CONTROLLER (:class:`BugFixerController`).

These drive ``run_cycle`` through its REAL plumbing — a throwaway git repo, the shared
:class:`WorktreeManager`, the real :class:`Orchestrator` (so a fix actually runs as a Task with the
authoritative green-gate), the real reproduction gate, and the real status ledger — with only two
things faked: the provider (a deterministic, LLM-free double) and ``discover_bug`` (monkeypatched to
hand the cycle a known candidate, so the hunter/region/coverage stack, tested elsewhere, is out of
scope here).

Covered:

* idle — no candidate discovered → ``CycleOutcome.idle``, nothing written.
* reproduced → fix succeeds → candidate ``fixed`` (the repro test reaches the gate and passes).
* reproduced → fix fails → candidate ``deferred``.
* dismissed — repro passes on the unfixed code → ``dismissed_false_positive`` (gate-transitioned).
* declined — covered-behaviour change → ``declined_needs_human`` (gate-transitioned), put on the
  human work-queue.
* no usable repro → ``deferred`` (a legal edge, never a forced illegal transition).
"""

from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path

import pytest

from conclave.config import ConclaveConfig, load_project_config
from conclave.db import BugCandidate, BugStatus, Database, Project
from conclave.db import repositories as repo
from conclave.engine import BugFixerController, CycleOutcome, Orchestrator, run_git
from conclave.engine import bug_fixer as bug_fixer_mod
from conclave.events import EventBus
from conclave.providers import AgentResult, OnChunk, ResolvedProfile

# A buggy source function and its fix. ``answer`` returns the WRONG value on the unfixed checkpoint;
# the repro test asserts the RIGHT one (so it fails until the fix lands).
_BUGGY_SRC = "def answer() -> int:\n    return 41\n"
_FIXED_SRC = "def answer() -> int:\n    return 42\n"
_REPRO_PATH = "tests/repro/test_answer.py"
_REPRO_BODY = (
    "from src.app import answer\n\n\ndef test_answer() -> None:\n    assert answer() == 42\n"
)
_REPRO_BLOCK = f"```repro\npath: {_REPRO_PATH}\n{_REPRO_BODY}```"
# A repro test that PASSES on the unfixed code → drives the false-positive (dismissed) path.
_PASSING_BODY = "def test_already_true() -> None:\n    assert 1 == 1\n"
_PASSING_BLOCK = f"```repro\npath: {_REPRO_PATH}\n{_PASSING_BODY}```"

_PASS_VERDICT = '```json\n{"verdict": "pass", "reason": "looks correct", "evidence": []}\n```'


async def _init_repo(path: Path) -> None:
    """A throwaway git repo on ``main`` carrying the buggy source — the unfixed checkpoint."""
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    await run_git(path, "config", "user.email", "test@example.com")
    await run_git(path, "config", "user.name", "Test")
    (path / "src").mkdir()
    (path / "src" / "__init__.py").write_text("")
    (path / "src" / "app.py").write_text(_BUGGY_SRC, encoding="utf-8")
    await run_git(path, "add", "-A")
    await run_git(path, "commit", "-m", "initial commit (with bug)")


def _pytest_command() -> str:
    """Run pytest with the interpreter running this suite (guaranteed to have pytest)."""
    return f"{shlex.quote(sys.executable)} -m pytest -p no:cacheprovider -q"


class _BugFixerProvider:
    """Deterministic provider for the controller cycle: repro synth, developer fix, reviewers.

    Keyed on prompt discriminators, mirroring the integration ``FakeProvider``:

    * the ``repro`` synthesis dispatch (``Write ONE focused test``) → a fenced repro block;
    * the developer dispatch → writes the repro test file AND the source fix into the worktree
      (``fix_succeeds``), or only the (failing) repro test (``fix_succeeds=False``) so the gate
      stays red and the fix task fails;
    * reviewers / planner → a passing verdict / a trivial plan.
    """

    def __init__(self, *, repro_block: str = _REPRO_BLOCK, fix_succeeds: bool = True) -> None:
        self._repro_block = repro_block
        self._fix_succeeds = fix_succeeds

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
        # Reproduction-test synthesis (controller-issued, before any worktree write).
        if "Write ONE focused test" in prompt:
            return AgentResult(ok=True, text=self._repro_block, model_reported="fake", cost_usd=0.0)
        # Planner (orchestrator) — keyed on its unique instruction.
        if "Produce a structured plan" in prompt:
            return AgentResult(
                ok=True,
                text='```json\n{"approach": "fix it", "files_to_touch": ["src/app.py"]}\n```',
                model_reported="fake",
                cost_usd=0.0,
            )
        # Reviewers (orchestrator) — keyed on the review instruction; always pass.
        if "Review the changes made for this task" in prompt:
            return AgentResult(ok=True, text=_PASS_VERDICT, model_reported="fake", cost_usd=0.0)
        # Developer fallback: add the repro test (+ the fix when the scenario wants a green gate).
        if cwd is not None:
            repro_target = Path(cwd) / _REPRO_PATH
            repro_target.parent.mkdir(parents=True, exist_ok=True)
            repro_target.write_text(_REPRO_BODY, encoding="utf-8")
            if self._fix_succeeds:
                (Path(cwd) / "src" / "app.py").write_text(_FIXED_SRC, encoding="utf-8")
        return AgentResult(
            ok=True, text="Implemented. VERDICT: PASS", model_reported="fake", cost_usd=0.01,
        )


async def _make(
    db: Database, tmp_path: Path, provider: _BugFixerProvider
) -> tuple[Project, ConclaveConfig, BugFixerController]:
    """Stand up a repo + project + orchestrator + controller, with the gate wired to real pytest."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    # Pin the project's test command so the green-gate (and the repro gate) run real pytest.
    project = await repo.create_project(
        db,
        name="t",
        path=str(repo_path),
        default_branch="main",
        config={"execution": {"baseline_test_command": _pytest_command()}},
    )
    config = load_project_config(project.config)
    bus = EventBus(db)
    orchestrator = Orchestrator(db, bus, provider, tmp_path / "home")  # type: ignore[arg-type]
    return project, config, BugFixerController(orchestrator)


def _patch_discovery(
    monkeypatch: pytest.MonkeyPatch, candidate: BugCandidate | None
) -> None:
    """Replace ``discover_bug`` in the controller's namespace with a fixed return."""

    async def _fake_discover(*_args: object, **_kwargs: object) -> BugCandidate | None:
        return candidate

    monkeypatch.setattr(bug_fixer_mod, "discover_bug", _fake_discover)


async def _seed_candidate(db: Database, project: Project) -> BugCandidate:
    return await repo.create_bug_candidate(
        db,
        project_id=project.id,
        fingerprint="fp-answer",
        claim="answer() returns 41 but should return 42",
        file="src/app.py",
        symbol="answer",
        region="src",
    )


# --- idle: nothing discovered -----------------------------------------------


async def test_idle_when_no_candidate(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cycle that discovers nothing returns idle and writes no candidate row."""
    provider = _BugFixerProvider()
    project, config, controller = await _make(db, tmp_path, provider)
    _patch_discovery(monkeypatch, None)

    result = await controller.run_cycle(project, config)

    assert result.outcome is CycleOutcome.idle
    assert result.outcome.did_work is False
    assert result.candidate is None
    assert await repo.list_bug_candidates(db, project.id) == []


# --- reproduced → fix succeeds → fixed ---------------------------------------


async def test_reproduced_fix_succeeds_marks_fixed(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The happy path: reproduce, run a fix Task whose gate (with the repro test) goes green.

    Proves the repro test reaches the authoritative gate: the developer writes it into the worktree
    and the fix only "passes" because the source change makes that very test pass.
    """
    provider = _BugFixerProvider(fix_succeeds=True)
    project, config, controller = await _make(db, tmp_path, provider)
    candidate = await _seed_candidate(db, project)
    _patch_discovery(monkeypatch, candidate)

    result = await controller.run_cycle(project, config)

    assert result.outcome is CycleOutcome.fixed
    assert result.outcome.did_work is True
    stored = await repo.get_bug_candidate(db, candidate.id)
    assert stored is not None
    assert stored.status is BugStatus.fixed
    assert stored.fixed_at is not None
    assert stored.attempts == 1  # one in-flight auto-fix == one attempt (entering `fixing`)
    assert stored.task_id is not None  # the driving task is linked

    # The fix really ran as a bug_fixer Task that reached done, and the source is repaired + merged.
    task = await repo.get_task(db, stored.task_id)
    assert task is not None and task.origin.value == "bug_fixer"
    assert (Path(project.path) / "src" / "app.py").read_text() == _FIXED_SRC


# --- reproduced → fix fails → deferred --------------------------------------


async def test_reproduced_fix_fails_marks_deferred(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the fix task cannot make the gate green (bug unfixed), the candidate is deferred."""
    provider = _BugFixerProvider(fix_succeeds=False)  # writes the failing repro test but no fix
    project, config, controller = await _make(db, tmp_path, provider)
    candidate = await _seed_candidate(db, project)
    _patch_discovery(monkeypatch, candidate)

    result = await controller.run_cycle(project, config)

    assert result.outcome is CycleOutcome.deferred
    stored = await repo.get_bug_candidate(db, candidate.id)
    assert stored is not None
    assert stored.status is BugStatus.deferred
    assert stored.attempts == 1  # the fix WAS attempted before it failed
    assert stored.decline_reason and "did not merge" in stored.decline_reason
    # The operator's checkout is untouched — the failed fix never merged its branch.
    assert (Path(project.path) / "src" / "app.py").read_text() == _BUGGY_SRC


# --- dismissed: repro passes on unfixed code → false positive ----------------


async def test_dismissed_false_positive(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repro that already PASSES on the unfixed code dismisses the candidate (gate path)."""
    provider = _BugFixerProvider(repro_block=_PASSING_BLOCK)
    project, config, controller = await _make(db, tmp_path, provider)
    candidate = await _seed_candidate(db, project)
    _patch_discovery(monkeypatch, candidate)

    result = await controller.run_cycle(project, config)

    assert result.outcome is CycleOutcome.dismissed
    stored = await repo.get_bug_candidate(db, candidate.id)
    assert stored is not None
    assert stored.status is BugStatus.dismissed_false_positive
    # No fix task was created for a dismissed candidate.
    assert stored.task_id is None


# --- declined: covered-behaviour change → needs a human ----------------------


async def test_declined_routes_to_human(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine failure flagged as a covered-behaviour change is declined to a human (gate path).

    The gate's heightened-scrutiny signal is exercised by stubbing ``reproduce_bug`` to report the
    ``declined`` outcome (the gate's own coverage logic is unit-tested in ``test_repro_gate``); the
    controller must surface it as ``declined`` and onto the human work-queue without re-driving the
    status itself.
    """
    from conclave.engine.repro_gate import ReproOutcome, ReproResult

    provider = _BugFixerProvider()
    project, config, controller = await _make(db, tmp_path, provider)
    candidate = await _seed_candidate(db, project)
    _patch_discovery(monkeypatch, candidate)

    declined_row = await repo.transition_bug_status(
        db, candidate.id, BugStatus.declined_needs_human,
        decline_reason="covered-behaviour change — needs human review",
    )

    async def _fake_reproduce(*_args: object, **_kwargs: object) -> ReproResult:
        return ReproResult(outcome=ReproOutcome.declined, candidate=declined_row)

    monkeypatch.setattr(bug_fixer_mod, "reproduce_bug", _fake_reproduce)

    result = await controller.run_cycle(project, config)

    assert result.outcome is CycleOutcome.declined
    queue = await repo.list_needs_human(db, project.id)
    assert [c.id for c in queue] == [candidate.id]


# --- no usable repro → deferred (legal edge, not a forced illegal transition) -


async def test_no_usable_repro_defers(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the repro persona breaks its one-block contract, the candidate is deferred."""
    provider = _BugFixerProvider(repro_block="no fenced repro block here, just prose")
    project, config, controller = await _make(db, tmp_path, provider)
    candidate = await _seed_candidate(db, project)
    _patch_discovery(monkeypatch, candidate)

    result = await controller.run_cycle(project, config)

    assert result.outcome is CycleOutcome.deferred
    stored = await repo.get_bug_candidate(db, candidate.id)
    assert stored is not None
    assert stored.status is BugStatus.deferred
    assert stored.attempts == 0  # never reached a fix attempt
    assert stored.decline_reason and "no usable repro" in stored.decline_reason
