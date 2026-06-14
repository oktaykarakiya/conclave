"""WebSocket endpoint streaming live events to the UI."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from ..db import repositories as repo
from ..events import EventType
from ..runtime import Daemon

_WS_POLICY_VIOLATION = 1008

router = APIRouter()


@router.websocket("/ws/stream")
async def stream(
    websocket: WebSocket, project_id: str | None = None, task_id: str | None = None
) -> None:
    await websocket.accept()
    daemon: Daemon = websocket.app.state.daemon
    subscriber = daemon.bus.subscribe(project_id=project_id, task_id=task_id)
    try:
        with subscriber:
            async for event in subscriber:
                await websocket.send_json(event.model_dump(mode="json"))
    except (WebSocketDisconnect, RuntimeError):
        # client went away; the context manager unsubscribes on exit
        return


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
    try:
        with subscriber:
            async for event in subscriber:
                await websocket.send_json(event.model_dump(mode="json"))
    except (WebSocketDisconnect, RuntimeError):
        return
