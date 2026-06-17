"""Unit tests for the daemon runtime (runtime.py)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conclave.config import ConclaveConfig
from conclave.db import ProjectMode
from conclave.engine import CycleOutcome, CycleResult
from conclave.runtime import ProjectWorker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_worker(
    idle_sleep: float = 0.01, max_idle_sleep: float = 0.16
) -> ProjectWorker:
    """Create a ProjectWorker with fast backoff for deterministic tests."""
    db = MagicMock()
    orch = AsyncMock()
    orch.recover = AsyncMock()
    orch.process_task = AsyncMock()
    # The bug-fixer controller borrows these off the orchestrator; give it a REAL cancel-event
    # dict so the worker's register→pop cancellation bookkeeping is observable, not a mock no-op.
    orch._cancel_events = {}
    return ProjectWorker(
        db, orch, "proj-1", idle_sleep=idle_sleep, max_idle_sleep=max_idle_sleep
    )


# ---------------------------------------------------------------------------
# Idle worker backoff — unit-level tests on _idle_backoff
# ---------------------------------------------------------------------------


async def test_idle_worker_backs_off_exponentially() -> None:
    """_idle_backoff doubles sleep each call until it hits the cap."""
    worker = _make_worker(idle_sleep=0.01, max_idle_sleep=0.16)

    sleeps: list[float] = []

    async def fake_sleep(duration: float) -> None:
        sleeps.append(duration)

    with patch("conclave.runtime.asyncio.sleep", new=fake_sleep):
        for _ in range(6):
            await worker._idle_backoff()

    assert sleeps == [0.01, 0.02, 0.04, 0.08, 0.16, 0.16]


async def test_idle_worker_resets_backoff_on_work() -> None:
    """After backing off, manually resetting _current_idle_sleep returns to base."""
    worker = _make_worker(idle_sleep=0.01, max_idle_sleep=0.16)

    async def fake_sleep(duration: float) -> None:
        pass

    with patch("conclave.runtime.asyncio.sleep", new=fake_sleep):
        for _ in range(3):
            await worker._idle_backoff()

    assert worker._current_idle_sleep == 0.08

    # Simulate work claimed: _loop resets to base.
    worker._current_idle_sleep = worker._idle_sleep
    assert worker._current_idle_sleep == 0.01


async def test_worker_backoff_capped_at_max() -> None:
    """Sleep must never exceed max_idle_sleep regardless of idle iterations."""
    worker = _make_worker(idle_sleep=2.0, max_idle_sleep=30.0)

    async def fake_sleep(duration: float) -> None:
        pass

    with patch("conclave.runtime.asyncio.sleep", new=fake_sleep):
        for _ in range(20):
            await worker._idle_backoff()

    assert worker._current_idle_sleep == 30.0


async def test_worker_initial_backoff_is_idle_sleep() -> None:
    """A freshly created worker starts with _current_idle_sleep == idle_sleep."""
    worker = _make_worker(idle_sleep=2.0, max_idle_sleep=30.0)
    assert worker._current_idle_sleep == 2.0


async def test_worker_loop_resets_backoff_after_processing_task() -> None:
    """After _loop processes a task, _current_idle_sleep resets to base."""
    worker = _make_worker(idle_sleep=0.01, max_idle_sleep=0.16)

    async def fake_sleep(duration: float) -> None:
        pass

    with patch("conclave.runtime.asyncio.sleep", new=fake_sleep):
        for _ in range(3):
            await worker._idle_backoff()

    assert worker._current_idle_sleep == 0.08

    # Simulate _loop claiming a task and resetting.
    worker._current_idle_sleep = worker._idle_sleep
    assert worker._current_idle_sleep == 0.01


# ---------------------------------------------------------------------------
# Mode dispatch — _loop routes by project.mode (task_queue vs autonomous_bug_fixer)
# ---------------------------------------------------------------------------


def _fake_project(mode: ProjectMode) -> SimpleNamespace:
    """A minimal stand-in for a Project row carrying just what _loop branches on."""
    return SimpleNamespace(id="proj-1", mode=mode, config={})


async def _run_one_loop_iteration(worker: ProjectWorker) -> None:
    """Drive ``_loop`` for exactly one tick by stopping the worker from inside the tick."""

    async def _stop_after(*_a: object, **_k: object) -> None:
        worker._stop.set()

    # Whichever tick fires, stop the loop so it returns after a single iteration.
    worker._task_queue_tick = AsyncMock(side_effect=_stop_after)  # type: ignore[method-assign]
    worker._bug_fixer_tick = AsyncMock(side_effect=_stop_after)  # type: ignore[method-assign]
    await worker._loop()


async def test_loop_dispatches_task_queue_for_task_queue_mode() -> None:
    """A ``task_queue`` project routes the tick to the normal claim→process_task path."""
    worker = _make_worker()
    project = _fake_project(ProjectMode.task_queue)

    with patch("conclave.runtime.repo.get_project", new=AsyncMock(return_value=project)):
        await _run_one_loop_iteration(worker)

    worker._task_queue_tick.assert_awaited_once()  # type: ignore[attr-defined]
    worker._bug_fixer_tick.assert_not_awaited()  # type: ignore[attr-defined]


async def test_loop_dispatches_bug_fixer_for_autonomous_mode() -> None:
    """An ``autonomous_bug_fixer`` project routes the tick to the controller cycle."""
    worker = _make_worker()
    project = _fake_project(ProjectMode.autonomous_bug_fixer)

    with patch("conclave.runtime.repo.get_project", new=AsyncMock(return_value=project)):
        await _run_one_loop_iteration(worker)

    worker._bug_fixer_tick.assert_awaited_once()  # type: ignore[attr-defined]
    worker._task_queue_tick.assert_not_awaited()  # type: ignore[attr-defined]


async def test_task_queue_tick_claims_and_processes() -> None:
    """``_task_queue_tick`` claims the next approved task and runs it through the orchestrator."""
    worker = _make_worker()
    task = SimpleNamespace(id="task-9")

    with patch(
        "conclave.runtime.repo.claim_next_approved", new=AsyncMock(return_value=task)
    ):
        await worker._task_queue_tick()

    worker._orchestrator.process_task.assert_awaited_once()  # type: ignore[attr-defined]
    # The per-task cancel entry is registered and then cleaned (no leak).
    assert "task-9" not in worker._orchestrator._cancel_events


# ---------------------------------------------------------------------------
# Autonomous session budget — caps the number of candidates pursued per session
# ---------------------------------------------------------------------------


async def test_bug_fixer_tick_runs_cycle_until_candidate_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker meters cycles: once ``max_candidates`` is reached it stops running new cycles."""
    worker = _make_worker()
    project = _fake_project(ProjectMode.autonomous_bug_fixer)

    # A cap of 2 candidates, no wall-clock limit.
    cfg = ConclaveConfig.model_validate(
        {"bug_fixer": {"max_candidates": 2, "wall_clock_budget_minutes": 0}}
    )
    monkeypatch.setattr("conclave.runtime.load_project_config", lambda _c: cfg)

    calls = 0

    async def _fake_cycle(*_a: object, **_k: object) -> CycleResult:
        nonlocal calls
        calls += 1
        return CycleResult(CycleOutcome.fixed)

    worker._bug_fixer.run_cycle = AsyncMock(side_effect=_fake_cycle)  # type: ignore[method-assign]

    async def _noop_sleep(_d: float) -> None:
        pass

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    # Two productive cycles consume the cap; the third tick is over budget and idles instead.
    await worker._bug_fixer_tick(project)
    await worker._bug_fixer_tick(project)
    await worker._bug_fixer_tick(project)

    assert calls == 2  # the 3rd tick short-circuited on the exhausted budget
    assert worker._session_budget is not None
    assert worker._session_budget.candidates_pursued == 2


async def test_bug_fixer_idle_cycle_does_not_count_against_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle (no-candidate) cycle backs off but does not consume a candidate slot."""
    worker = _make_worker()
    project = _fake_project(ProjectMode.autonomous_bug_fixer)

    cfg = ConclaveConfig.model_validate(
        {"bug_fixer": {"max_candidates": 1, "wall_clock_budget_minutes": 0}}
    )
    monkeypatch.setattr("conclave.runtime.load_project_config", lambda _c: cfg)
    worker._bug_fixer.run_cycle = AsyncMock(  # type: ignore[method-assign]
        return_value=CycleResult(CycleOutcome.idle)
    )

    async def _noop_sleep(_d: float) -> None:
        pass

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)

    await worker._bug_fixer_tick(project)
    await worker._bug_fixer_tick(project)

    assert worker._bug_fixer.run_cycle.await_count == 2  # idle never exhausts the candidate cap
    assert worker._session_budget is not None
    assert worker._session_budget.candidates_pursued == 0
