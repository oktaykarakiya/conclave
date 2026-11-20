"""What the live event stream sends: the replay after a client's ``after_id``, the boundary
between the replay and the live events, and the gap frames (``conclave.events.stream``)."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest

from conclave.db import Database, EventRow
from conclave.db import repositories as repo
from conclave.events import EventBus, EventType, Subscriber
from conclave.events.stream import GAP_TYPE, REPLAY_MAX, StreamGap, stream_frames
from waits import within

Frame = EventRow | StreamGap


async def _next(frames: AsyncGenerator[Frame, None]) -> Frame:
    return await within(anext(frames), "the stream never sent its next frame")


async def _take(frames: AsyncGenerator[Frame, None], count: int) -> list[Frame]:
    return [await _next(frames) for _ in range(count)]


def _queued(subscriber: Subscriber) -> list[EventRow]:
    """What a live subscriber has received and not yet read."""
    return [subscriber.queue.get_nowait() for _ in range(subscriber.queue.qsize())]


def _frames(
    db: Database,
    subscriber: Subscriber,
    *,
    after_id: int | None,
    project_id: str | None = None,
    task_id: str | None = None,
) -> AsyncGenerator[Frame, None]:
    return stream_frames(
        db, subscriber, project_id=project_id, task_id=task_id, after_id=after_id
    )


async def _insert_missed(db: Database, count: int) -> None:
    """*count* events emitted while no client was connected, in one transaction: emitting them
    one by one through the bus commits each, which takes seconds for a thousand."""
    async with db.transaction() as conn:
        await conn.executemany(
            "INSERT INTO events(type, payload_json, ts) VALUES ('log', ?, '')",
            [(json.dumps({"n": n}),) for n in range(count)],
        )


@pytest.mark.parametrize(
    ("project_id", "task_id", "expected"),
    [
        (None, None, ["p1", "p2", "none", "p1/t1", "p2/t2", "t1"]),
        # A project's stream also carries the events of no project.
        ("p1", None, ["p1", "none", "p1/t1", "t1"]),
        ("p2", None, ["p2", "none", "p2/t2", "t1"]),
        ("p1", "t1", ["p1/t1", "t1"]),
        (None, "t1", ["p1/t1", "t1"]),
    ],
)
async def test_the_replay_is_the_missed_events_of_the_filter_in_id_order(
    db: Database, project_id: str | None, task_id: str | None, expected: list[str]
) -> None:
    """With after_id, the stream first sends the persisted events after it that its project and
    task filters match, in id order, which are the events a live subscription with the same
    filters received, then the live ones."""
    bus = EventBus(db)
    witness = bus.subscribe(project_id=project_id, task_id=task_id)
    cursor = await bus.emit(type=EventType.log, project_id="p1", payload={"name": "before"})
    for name, pid, tid in [
        ("p1", "p1", None),
        ("p2", "p2", None),
        ("none", None, None),
        ("p1/t1", "p1", "t1"),
        ("p2/t2", "p2", "t2"),
        ("t1", None, "t1"),
    ]:
        await bus.emit(type=EventType.log, project_id=pid, task_id=tid, payload={"name": name})
    received_live = [e for e in _queued(witness) if e.id > cursor.id]

    with bus.subscribe(project_id=project_id, task_id=task_id) as subscriber:
        frames = _frames(
            db, subscriber, after_id=cursor.id, project_id=project_id, task_id=task_id
        )
        replayed = await _take(frames, len(expected))
        assert [f.payload["name"] for f in replayed if isinstance(f, EventRow)] == expected
        assert replayed == received_live

        live = await bus.emit(
            type=EventType.log, project_id=project_id, task_id=task_id, payload={"name": "live"}
        )
        assert await _next(frames) == live
        await frames.aclose()
    witness.close()


async def test_an_event_emitted_while_the_replay_is_read_is_sent_once(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The subscription is taken before the replay is read. An event emitted after the
    subscription and before the read is both replayed and queued live: it is sent once. One
    emitted after the read, before the replay is sent, is sent live. None is lost."""
    bus = EventBus(db)
    first = await bus.emit(type=EventType.log, payload={"n": 1})
    read = repo.list_missed_events

    async def read_between_two_events(db: Database, **kwargs: Any) -> list[EventRow]:
        await bus.emit(type=EventType.log, payload={"n": 2})  # replayed and queued live
        missed = await read(db, **kwargs)
        await bus.emit(type=EventType.log, payload={"n": 3})  # queued live only
        return missed

    monkeypatch.setattr(repo, "list_missed_events", read_between_two_events)
    with bus.subscribe() as subscriber:
        frames = _frames(db, subscriber, after_id=first.id - 1)
        sent = await _take(frames, 3)
        assert [f.payload["n"] for f in sent if isinstance(f, EventRow)] == [1, 2, 3]

        # Had the live copy of event 2 not been skipped, it would come next.
        later = await bus.emit(type=EventType.log, payload={"n": 4})
        assert await _next(frames) == later
        await frames.aclose()


@pytest.mark.parametrize("missed", [REPLAY_MAX, REPLAY_MAX + 1, REPLAY_MAX + 7])
async def test_a_replay_past_the_cap_sends_a_gap_frame_then_the_newest_events(
    db: Database, missed: int
) -> None:
    """At most REPLAY_MAX missed events are replayed. When more were missed, a gap frame comes
    first, then the newest REPLAY_MAX of them, then the live events."""
    bus = EventBus(db)
    await _insert_missed(db, missed)
    with bus.subscribe() as subscriber:
        frames = _frames(db, subscriber, after_id=0)
        if missed > REPLAY_MAX:
            gap = await _next(frames)
            assert gap == StreamGap("replay_truncated")
            assert isinstance(gap, StreamGap)
            assert gap.frame() == {"type": GAP_TYPE, "reason": "replay_truncated"}
        replayed = await _take(frames, REPLAY_MAX)
        assert [f.payload["n"] for f in replayed if isinstance(f, EventRow)] == list(
            range(missed - REPLAY_MAX, missed)
        )

        live = await bus.emit(type=EventType.log, payload={"n": "live"})
        assert await _next(frames) == live
        await frames.aclose()


async def test_an_overflowing_subscriber_sends_a_gap_frame_before_the_next_event(
    db: Database,
) -> None:
    """When the live subscription's queue overflows (the client reads too slowly), the events
    it dropped are announced by one gap frame before the next event it delivers."""
    bus = EventBus(db)
    with bus.subscribe(maxsize=2) as subscriber:
        frames = _frames(db, subscriber, after_id=None)
        for n in range(5):
            await bus.emit(type=EventType.log, payload={"n": n})  # the queue keeps 3 and 4

        assert await _next(frames) == StreamGap("overflow")
        kept = await _take(frames, 2)
        assert [f.payload["n"] for f in kept if isinstance(f, EventRow)] == [3, 4]

        later = await bus.emit(type=EventType.log, payload={"n": 5})
        assert await _next(frames) == later  # one gap frame per overflow
        await frames.aclose()


async def test_without_after_id_the_stream_is_live_only(db: Database) -> None:
    bus = EventBus(db)
    await bus.emit(type=EventType.log, payload={"n": "before"})
    with bus.subscribe() as subscriber:
        frames = _frames(db, subscriber, after_id=None)
        live = await bus.emit(type=EventType.log, payload={"n": "live"})
        assert await _next(frames) == live
        await frames.aclose()


def test_no_event_type_is_the_gap_frames_type() -> None:
    """A gap frame has no id, which every event has, and a type no event has."""
    assert GAP_TYPE not in {t.value for t in EventType}
    assert "id" not in StreamGap("overflow").frame()
