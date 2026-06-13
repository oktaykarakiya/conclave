"""Unit tests for the event bus."""

from __future__ import annotations

import asyncio

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
