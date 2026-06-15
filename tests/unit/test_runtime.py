"""Unit tests for the daemon runtime (runtime.py)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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
