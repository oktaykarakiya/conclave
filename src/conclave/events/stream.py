"""What the live event stream sends (``/ws/stream`` and the SSE ``/api/events/stream``).

A client that passes ``after_id``, the id of the last event it received, first gets the
persisted events after it that match its filters (project, task), oldest first, then the live
ones. Without ``after_id`` the stream is live only. The subscription is taken before the replay
is read, so an event emitted in between is both replayed and queued live: the live copy is
skipped (its id is at most the last replayed id), and no event is lost or sent twice.

Where the stream cannot deliver every event, it sends a gap frame instead,
``{"type": "stream.gap", "reason": ...}``. It has no ``id``, which every event has, so a client
tells it apart by its shape. It means events were lost at that point of the stream, so the
client refetches what it derives from events. The reasons:

- ``replay_truncated``: more than :data:`REPLAY_MAX` events were missed. The gap frame comes
  first, then the newest :data:`REPLAY_MAX` of them.
- ``overflow``: the live subscription's queue overflowed (the client reads too slowly) and
  dropped events (:attr:`Subscriber.gap_detected`). The gap frame comes before the next event.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Literal

from ..db import Database, EventRow
from ..db import repositories as repo
from .bus import Subscriber

REPLAY_MAX = 1000
"""The most missed events a stream replays: past it, a ``replay_truncated`` gap frame."""

GAP_TYPE = "stream.gap"


@dataclass(frozen=True)
class StreamGap:
    """Events were lost at this point of the stream (see the module docstring)."""

    reason: Literal["replay_truncated", "overflow"]

    def frame(self) -> dict[str, Any]:
        return {"type": GAP_TYPE, "reason": self.reason}


async def stream_frames(
    db: Database,
    subscriber: Subscriber,
    *,
    project_id: str | None,
    task_id: str | None,
    after_id: int | None,
) -> AsyncGenerator[EventRow | StreamGap, None]:
    """The stream of *subscriber*, which was subscribed with *project_id* and *task_id*: the
    events missed after *after_id* when it is given, then the live ones, with gap frames."""
    last_replayed: int | None = None
    if after_id is not None:
        missed = await repo.list_missed_events(
            db, after_id=after_id, limit=REPLAY_MAX + 1, project_id=project_id, task_id=task_id
        )
        if len(missed) > REPLAY_MAX:
            yield StreamGap("replay_truncated")
            missed = missed[1:]
        for event in missed:
            yield event
            last_replayed = event.id
    async for event in subscriber:
        # Checked before the skip below, which would otherwise consume the flag unseen.
        if subscriber.gap_detected:
            yield StreamGap("overflow")
        if last_replayed is not None and event.id <= last_replayed:
            continue  # emitted after the subscription and before the replay read: sent already
        yield event
