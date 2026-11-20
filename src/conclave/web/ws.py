"""WebSocket endpoint streaming live events to the UI."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Query, WebSocket
from starlette.websockets import WebSocketDisconnect

from ..db import EventRow
from ..db import repositories as repo
from ..db.database import SQLITE_INT_MAX
from ..events import EventType
from ..events.stream import StreamGap, stream_frames
from ..runtime import Daemon

_WS_POLICY_VIOLATION = 1008
_WS_TRY_AGAIN_LATER = 1013  # the event bus is at its subscriber cap

router = APIRouter()


async def _forward_until_disconnect(
    websocket: WebSocket, frames: AsyncIterator[EventRow | StreamGap]
) -> None:
    """Forward the stream's events and gap frames to the client until either side ends it.

    Sending alone cannot detect a closed socket while the subscription is idle: the
    handler would block on the next event forever, leaking the subscriber (the bus caps
    them) and stalling graceful shutdown, which closes every socket and then waits for
    the handlers to return. So the client side is watched concurrently.
    """

    async def forward() -> None:
        async for frame in frames:
            if isinstance(frame, StreamGap):
                await websocket.send_json(frame.frame())
            else:
                await websocket.send_json(frame.model_dump(mode="json"))

    async def until_client_disconnects() -> None:
        while (await websocket.receive())["type"] != "websocket.disconnect":
            pass  # the UI sends nothing meaningful; ignore anything it does send

    tasks = [asyncio.create_task(forward()), asyncio.create_task(until_client_disconnects())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, RuntimeError):
                await t
    for t in done:
        exc = t.exception()
        # A disconnect surfacing as a send/receive error is the normal way out.
        if exc is not None and not isinstance(exc, WebSocketDisconnect | RuntimeError):
            raise exc


@router.websocket("/ws/stream")
async def stream(
    websocket: WebSocket,
    project_id: str | None = None,
    task_id: str | None = None,
    after_id: Annotated[int | None, Query(ge=0, le=SQLITE_INT_MAX)] = None,
) -> None:
    """Live events, optionally of one project or task, and a gap frame where events were lost.
    With ``after_id`` (the id of the last event the client received), the events missed since
    then come first (:mod:`conclave.events.stream`). FastAPI closes the socket with 1008 when
    ``after_id`` is not a non-negative integer."""
    await websocket.accept()
    daemon: Daemon = websocket.app.state.daemon
    norm_pid = project_id.strip() if project_id and project_id.strip() else None
    norm_tid = task_id.strip() if task_id and task_id.strip() else None
    try:
        subscriber = daemon.bus.subscribe(project_id=norm_pid, task_id=norm_tid)
    except RuntimeError:  # the bus is at its subscriber cap
        await websocket.close(code=_WS_TRY_AGAIN_LATER)
        return
    frames = stream_frames(
        daemon.db, subscriber, project_id=norm_pid, task_id=norm_tid, after_id=after_id
    )
    with subscriber:  # unsubscribes on exit
        async with contextlib.aclosing(frames):
            await _forward_until_disconnect(websocket, frames)


@router.websocket("/ws/planning")
async def planning_stream(
    websocket: WebSocket, session_id: str
) -> None:
    """Stream only planning.* events for a specific session."""
    daemon: Daemon = websocket.app.state.daemon
    # Refuse subscriptions to sessions that don't exist (prevents fishing for
    # arbitrary session ids and silent dead connections).
    if await repo.get_planning_session(daemon.db, session_id) is None:
        await websocket.close(code=_WS_POLICY_VIOLATION)
        return
    await websocket.accept()
    planning_types = [str(t) for t in EventType if t.value.startswith("planning.")]
    try:
        subscriber = daemon.bus.subscribe(
            planning_session_id=session_id,
            types=planning_types,
        )
    except RuntimeError:  # the bus is at its subscriber cap
        await websocket.close(code=_WS_TRY_AGAIN_LATER)
        return
    with subscriber:
        await _forward_until_disconnect(websocket, subscriber)
