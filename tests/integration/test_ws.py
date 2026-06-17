"""Direct tests for the WebSocket handlers in ``conclave.web.ws``.

httpx's ASGI transport has no WebSocket support and Starlette's ``TestClient`` is
synchronous (and currently emits a deprecation warning that our strict ``error``
filter would reject), so these tests drive the handler coroutines directly with a
fake ``WebSocket`` double in the native async loop. That exercises the real
accept → subscribe → forward → cleanup path: a client connects, receives broadcast
bus events for its project (filtered), and the subscriber set shrinks on disconnect.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from starlette.websockets import WebSocketDisconnect

from conclave.db import Database
from conclave.db import repositories as repo
from conclave.events import EventType
from conclave.runtime import Daemon
from conclave.web.ws import planning_stream, stream


class _FakeWebSocket:
    """Minimal WebSocket double exercising the handlers' accept/send/close surface.

    ``send_json`` records each delivered message and raises :class:`WebSocketDisconnect`
    once ``disconnect_after`` messages have been delivered, so the handler's ``async for``
    loop terminates exactly as a real client going away would — letting the context
    manager unsubscribe and the subscriber set shrink.
    """

    def __init__(self, app: Any, *, disconnect_after: int | None = None) -> None:
        # The handlers read the daemon from ``websocket.app.state.daemon``.
        self.app = app
        self.accepted = False
        self.closed_code: int | None = None
        self.received: list[dict[str, Any]] = []
        self._disconnect_after = disconnect_after

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code

    async def send_json(self, data: dict[str, Any]) -> None:
        self.received.append(data)
        if self._disconnect_after is not None and len(self.received) >= self._disconnect_after:
            raise WebSocketDisconnect(code=1001)


def _make_daemon(db: Database, tmp_path: Path) -> Daemon:
    # workers_enabled=False: the bus is exercised directly, no project workers needed.
    return Daemon(db, tmp_path / "home", _NoopProvider(), workers_enabled=False)


class _NoopProvider:
    async def run_agent(self, **_kwargs: Any) -> Any:  # pragma: no cover - never dispatched
        raise AssertionError("provider must not be dispatched in ws tests")


async def _wait_for_subscribers(daemon: Daemon, count: int) -> None:
    """Spin until the bus reports *count* subscribers (the handler has subscribed)."""
    for _ in range(500):
        if daemon.bus.subscriber_count() == count:
            return
        await asyncio.sleep(0)
    raise AssertionError(
        f"expected {count} subscribers, got {daemon.bus.subscriber_count()}"
    )


class _AppShim:
    """Stand-in for ``websocket.app`` exposing only ``state.daemon``."""

    def __init__(self, daemon: Daemon) -> None:
        self.state = type("_State", (), {"daemon": daemon})()


async def test_stream_forwards_project_events_and_unsubscribes_on_disconnect(
    db: Database, tmp_path: Path,
) -> None:
    """A /ws/stream client subscribed to a project receives that project's broadcast bus
    events (and not another project's), then the subscriber set shrinks on disconnect."""
    daemon = _make_daemon(db, tmp_path)
    try:
        assert daemon.bus.subscriber_count() == 0
        # Disconnect after the first delivered event so the handler loop terminates.
        ws = _FakeWebSocket(_AppShim(daemon), disconnect_after=1)

        handler = asyncio.create_task(stream(ws, project_id="proj-1"))
        # Wait until the handler has accepted and subscribed before emitting.
        await _wait_for_subscribers(daemon, 1)
        assert ws.accepted is True

        # An event for a DIFFERENT project must be filtered out (not delivered).
        await daemon.bus.emit(type=EventType.log, project_id="proj-2", payload={"n": 0})
        # An event for THIS project must be delivered, which trips the disconnect.
        await daemon.bus.emit(type=EventType.log, project_id="proj-1", payload={"n": 1})

        await asyncio.wait_for(handler, timeout=5.0)

        # Exactly the matching event was forwarded.
        assert len(ws.received) == 1
        assert ws.received[0]["project_id"] == "proj-1"
        assert ws.received[0]["payload"] == {"n": 1}

        # The subscriber was cleaned up on disconnect — the set shrank back to empty.
        assert daemon.bus.subscriber_count() == 0
    finally:
        await daemon.shutdown()


async def test_planning_stream_rejects_unknown_session(
    db: Database, tmp_path: Path,
) -> None:
    """/ws/planning closes with a 1008 policy violation for a non-existent session and
    never accepts the connection or leaks a subscriber."""
    daemon = _make_daemon(db, tmp_path)
    try:
        ws = _FakeWebSocket(_AppShim(daemon))
        await planning_stream(ws, session_id="does-not-exist")

        assert ws.accepted is False
        assert ws.closed_code == 1008
        assert daemon.bus.subscriber_count() == 0
    finally:
        await daemon.shutdown()


async def test_planning_stream_forwards_planning_events_only_then_unsubscribes(
    db: Database, tmp_path: Path,
) -> None:
    """/ws/planning accepts a valid session, forwards only planning.* events for it, and
    the subscriber set shrinks on disconnect."""
    daemon = _make_daemon(db, tmp_path)
    try:
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        project = await repo.create_project(
            db, name="t", path=str(repo_path), default_branch="main",
        )
        session = await repo.create_planning_session(
            db, project_id=project.id, title="plan", prompt="do things",
        )

        ws = _FakeWebSocket(_AppShim(daemon), disconnect_after=1)
        handler = asyncio.create_task(planning_stream(ws, session_id=session.id))
        await _wait_for_subscribers(daemon, 1)
        assert ws.accepted is True

        # A non-planning event on the same session must be filtered out (type not planning.*).
        await daemon.bus.emit(
            type=EventType.log, planning_session_id=session.id, payload={"n": 0},
        )
        # A planning.* event for this session must be delivered, tripping the disconnect.
        await daemon.bus.emit(
            type=EventType.planning_agent_turn,
            planning_session_id=session.id,
            payload={"agent": "planner"},
        )

        await asyncio.wait_for(handler, timeout=5.0)

        assert len(ws.received) == 1
        assert ws.received[0]["type"] == "planning.agent_turn"
        assert ws.received[0]["planning_session_id"] == session.id

        # The subscriber was cleaned up on disconnect.
        assert daemon.bus.subscriber_count() == 0
    finally:
        await daemon.shutdown()
