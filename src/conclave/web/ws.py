"""WebSocket endpoint streaming live events to the UI."""

from __future__ import annotations

from fastapi import APIRouter, WebSocket
from starlette.websockets import WebSocketDisconnect

from ..runtime import Daemon

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
