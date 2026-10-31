"""WebSocket endpoint streaming live events to the UI."""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from ..db import repositories as repo
from ..events import EventType
from ..events.bus import Subscriber
from ..runtime import Daemon

_WS_POLICY_VIOLATION = 1008

router = APIRouter()


async def _forward_until_disconnect(websocket: WebSocket, subscriber: Subscriber) -> None:
    """Forward bus events to the client until either side ends the stream.

    Sending alone cannot detect a closed socket while the subscription is idle: the
    handler would block on the next event forever, leaking the subscriber (the bus caps
    them) and stalling graceful shutdown, which closes every socket and then waits for
    the handlers to return. So the client side is watched concurrently.
    """

    async def forward() -> None:
        async for event in subscriber:
            await websocket.send_json(event.model_dump(mode="json"))

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
    websocket: WebSocket, project_id: str | None = None, task_id: str | None = None
) -> None:
    await websocket.accept()
    daemon: Daemon = websocket.app.state.daemon
    norm_pid = project_id.strip() if project_id and project_id.strip() else None
    norm_tid = task_id.strip() if task_id and task_id.strip() else None
    subscriber = daemon.bus.subscribe(project_id=norm_pid, task_id=norm_tid)
    with subscriber:  # unsubscribes on exit
        await _forward_until_disconnect(websocket, subscriber)


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
    subscriber = daemon.bus.subscribe(
        planning_session_id=session_id,
        types=planning_types,
    )
    with subscriber:
        await _forward_until_disconnect(websocket, subscriber)
