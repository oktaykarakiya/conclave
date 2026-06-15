"""Unit tests for the event bus."""

from __future__ import annotations

import asyncio

import pytest

from conclave.db import Database
from conclave.db import repositories as repo
from conclave.events import EventBus, EventType


async def test_emit_persists_and_delivers(db: Database) -> None:
    bus = EventBus(db)
    sub = bus.subscribe()
    event = await bus.emit(type=EventType.task_started, project_id="p", task_id="t")

    delivered = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
    assert delivered.id == event.id
    assert delivered.type == "task.started"

    persisted = await repo.list_events(db, task_id="t")
    assert [e.id for e in persisted] == [event.id]


async def test_filter_by_task(db: Database) -> None:
    bus = EventBus(db)
    sub = bus.subscribe(task_id="t1")
    await bus.emit(type="x", task_id="t2")  # filtered out
    await bus.emit(type="y", task_id="t1")
    delivered = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
    assert delivered.task_id == "t1"
    assert delivered.type == "y"


async def test_overflow_drops_oldest_without_blocking(db: Database) -> None:
    bus = EventBus(db)
    sub = bus.subscribe(maxsize=10)
    for i in range(50):
        await bus.emit(type=EventType.log, payload={"i": i})
    assert sub.queue.qsize() == 10
    assert sub.dropped == 40
    # the queue retains the most recent events
    latest = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
    assert latest.payload["i"] == 40


async def test_context_manager_unsubscribes(db: Database) -> None:
    bus = EventBus(db)
    with bus.subscribe() as sub:
        await bus.emit(type="a")
        assert (await asyncio.wait_for(sub.__anext__(), timeout=1.0)).type == "a"
        assert bus.subscriber_count() == 1
    assert bus.subscriber_count() == 0


async def test_subscriber_cap_raises_when_full(db: Database) -> None:
    """EventBus.subscribe() must raise RuntimeError when subscriber count reaches max."""
    bus = EventBus(db, max_subscribers=3)
    # Fill to capacity
    s1 = bus.subscribe()
    s2 = bus.subscribe()
    s3 = bus.subscribe()
    assert bus.subscriber_count() == 3

    with pytest.raises(RuntimeError, match="subscriber cap reached"):
        bus.subscribe()

    # After unsubscribing one, a new subscriber can join.
    s1.close()
    assert bus.subscriber_count() == 2
    s4 = bus.subscribe()
    assert bus.subscriber_count() == 3

    # Clean up so the test doesn't leak subscribers.
    s2.close()
    s3.close()
    s4.close()


async def test_default_queue_maxsize_is_256(db: Database) -> None:
    """The per-subscriber queue maxsize defaults to 256 (not the old 1000)."""
    bus = EventBus(db)
    sub = bus.subscribe()
    assert sub.queue.maxsize == 256
    sub.close()


# ---------------------------------------------------------------------------
# Gap detection — backpressure signalling
# ---------------------------------------------------------------------------


async def test_gap_detected_false_when_no_overflow(db: Database) -> None:
    """gap_detected stays False when the queue never overflows."""
    bus = EventBus(db)
    sub = bus.subscribe(maxsize=10)
    await bus.emit(type=EventType.log, payload={"i": 1})
    await bus.emit(type=EventType.log, payload={"i": 2})

    assert sub.gap_detected is False
    evt = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
    assert evt.payload["i"] == 1
    assert sub.gap_detected is False

    evt = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
    assert evt.payload["i"] == 2
    assert sub.gap_detected is False


async def test_gap_detected_set_when_queue_overflows(db: Database) -> None:
    """When the bounded queue overflows, gap_detected becomes True on the next read."""
    bus = EventBus(db)
    sub = bus.subscribe(maxsize=3)
    # Fill and overflow the queue.
    for i in range(5):
        await bus.emit(type=EventType.log, payload={"i": i})
    assert sub.dropped >= 2  # at least 2 events evicted

    # The next event delivered must have gap_detected=True.
    evt = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
    assert sub.gap_detected is True
    assert evt.payload is not None


async def test_gap_detected_resets_after_read(db: Database) -> None:
    """gap_detected must reset to False on the second read after the gap signal."""
    bus = EventBus(db)
    sub = bus.subscribe(maxsize=3)
    # Fill and overflow.
    for i in range(5):
        await bus.emit(type=EventType.log, payload={"i": i})

    # First read after overflow — gap_detected must be True.
    evt1 = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
    assert sub.gap_detected is True

    # Second read — gap_detected must reset to False.
    evt2 = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
    assert sub.gap_detected is False
    assert evt2.id != evt1.id


async def test_overflow_preserves_gap_detected_with_existing_test(db: Database) -> None:
    """The original overflow test behaviour still holds + gap_detected is signalled."""
    bus = EventBus(db)
    sub = bus.subscribe(maxsize=10)
    for i in range(50):
        await bus.emit(type=EventType.log, payload={"i": i})
    assert sub.queue.qsize() == 10
    assert sub.dropped == 40
    # The first read after overflow must signal a gap.
    latest = await asyncio.wait_for(sub.__anext__(), timeout=1.0)
    assert latest.payload["i"] == 40
    assert sub.gap_detected is True
