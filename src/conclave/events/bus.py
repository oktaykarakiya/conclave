"""In-process async pub/sub event bus.

Each :meth:`EventBus.emit` persists the event to the ``events`` table (durable log +
replayable audit trail) and fans it out to every matching live subscriber. Fan-out is
non-blocking: a slow subscriber's bounded queue drops its oldest events rather than
stalling the orchestrator — the full record is always in the DB for backfill.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from ..db import Database, EventRow
from ..db import repositories as repo
from .types import EventType


@dataclass(frozen=True)
class EventFilter:
    project_id: str | None = None
    task_id: str | None = None
    agent: str | None = None
    types: frozenset[str] | None = None

    def matches(self, event: EventRow) -> bool:
        if self.project_id is not None and event.project_id != self.project_id:
            return False
        if self.task_id is not None and event.task_id != self.task_id:
            return False
        if self.agent is not None and event.agent != self.agent:
            return False
        if self.types is not None and event.type not in self.types:
            return False
        return True


class Subscriber:
    """An async-iterable stream of events matching a filter.

    Use as an async iterator (``async for event in sub``) and as a context manager
    so the subscription is removed on exit.
    """

    def __init__(self, bus: EventBus, event_filter: EventFilter, maxsize: int = 1000) -> None:
        self._bus = bus
        self._filter = event_filter
        self.queue: asyncio.Queue[EventRow] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def offer(self, event: EventRow) -> None:
        if not self._filter.matches(event):
            return
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass
            try:
                self.queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def __aiter__(self) -> Subscriber:
        return self

    async def __anext__(self) -> EventRow:
        return await self.queue.get()

    def close(self) -> None:
        self._bus._unsubscribe(self)

    def __enter__(self) -> Subscriber:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class EventBus:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._subscribers: set[Subscriber] = set()

    async def emit(
        self,
        *,
        type: EventType | str,
        project_id: str | None = None,
        task_id: str | None = None,
        agent: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> EventRow:
        event = await repo.append_event(
            self._db,
            type=str(type),
            project_id=project_id,
            task_id=task_id,
            agent=agent,
            payload=payload,
        )
        for subscriber in list(self._subscribers):
            subscriber.offer(event)
        return event

    def subscribe(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        agent: str | None = None,
        types: list[str] | None = None,
        maxsize: int = 1000,
    ) -> Subscriber:
        event_filter = EventFilter(
            project_id=project_id,
            task_id=task_id,
            agent=agent,
            types=frozenset(types) if types else None,
        )
        subscriber = Subscriber(self, event_filter, maxsize=maxsize)
        self._subscribers.add(subscriber)
        return subscriber

    def _unsubscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.discard(subscriber)

    def subscriber_count(self) -> int:
        return len(self._subscribers)
