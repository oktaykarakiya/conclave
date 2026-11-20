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
import functools
from pathlib import Path
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from conclave.db import Database
from conclave.db import repositories as repo
from conclave.db.database import SQLITE_INT_MAX
from conclave.events import EventType
from conclave.runtime import Daemon
from conclave.web import create_app
from conclave.web.ws import planning_stream, stream
from waits import wait_until, within


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
        self._client_gone = asyncio.Event()

    def client_disconnects(self) -> None:
        """Simulate the browser closing the socket (tab closed / server shutdown)."""
        self._client_gone.set()

    async def receive(self) -> dict[str, Any]:
        # The UI never sends anything: block until the client goes away.
        await self._client_gone.wait()
        return {"type": "websocket.disconnect", "code": 1001}

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
    """Wait until the bus reports *count* subscribers (the handler has subscribed).

    The planning-stream handler awaits an aiosqlite lookup in a worker thread before
    it subscribes, so a fixed number of event-loop ticks is not a time bound.
    """
    await wait_until(
        lambda: daemon.bus.subscriber_count() == count,
        f"the bus never had {count} subscribers",
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


async def test_idle_stream_releases_subscriber_when_client_disconnects(
    db: Database, tmp_path: Path,
) -> None:
    """With no events flowing, a client disconnect must still end the handler promptly.

    Regression: the handler used to notice a closed socket only on its next send, so an
    idle subscription blocked forever — leaking the subscriber (the bus is capped) and
    hanging graceful shutdown, which waits for every handler to return."""
    daemon = _make_daemon(db, tmp_path)
    try:
        ws = _FakeWebSocket(_AppShim(daemon))
        handler = asyncio.create_task(stream(ws, project_id="idle-project"))
        await _wait_for_subscribers(daemon, 1)

        ws.client_disconnects()
        await asyncio.wait_for(handler, timeout=5.0)

        assert ws.received == []
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


async def test_streams_at_the_subscriber_cap_close_with_try_again_later(
    db: Database, tmp_path: Path,
) -> None:
    """With the event bus at its subscriber cap, both stream handlers close the accepted
    socket with 1013 (Try Again Later) and return, instead of letting the bus's RuntimeError
    escape from the handler."""
    daemon = _make_daemon(db, tmp_path)
    fillers = []
    try:
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        project = await repo.create_project(
            db, name="t", path=str(repo_path), default_branch="main",
        )
        session = await repo.create_planning_session(
            db, project_id=project.id, title="plan", prompt="do things",
        )
        for _ in range(10_000):
            try:
                fillers.append(daemon.bus.subscribe())
            except RuntimeError:
                break

        live = _FakeWebSocket(_AppShim(daemon))
        await asyncio.wait_for(stream(live, project_id=project.id), timeout=5.0)
        planning = _FakeWebSocket(_AppShim(daemon))
        await asyncio.wait_for(planning_stream(planning, session_id=session.id), timeout=5.0)

        for ws in (live, planning):
            assert ws.accepted is True
            assert ws.closed_code == 1013
            assert ws.received == []
        assert daemon.bus.subscriber_count() == len(fillers)
    finally:
        for subscriber in fillers:
            subscriber.close()
        await daemon.shutdown()


class _SlowWebSocket(_FakeWebSocket):
    """A client that reads nothing until released: the handler's first send waits for it."""

    def __init__(self, app: Any, *, disconnect_after: int | None = None) -> None:
        super().__init__(app, disconnect_after=disconnect_after)
        self.sending = asyncio.Event()
        self.release = asyncio.Event()

    async def send_json(self, data: dict[str, Any]) -> None:
        self.sending.set()
        await self.release.wait()
        await super().send_json(data)


async def _open_through_app(app: Any, query: str) -> list[dict[str, Any]]:
    """Open ``/ws/stream?<query>`` through the ASGI app, where FastAPI validates the query (a
    direct call of the handler skips that), and return what the app sent: the client goes away
    once the app accepts or closes the socket."""
    sent: list[dict[str, Any]] = []
    answered = asyncio.Event()
    connected = False

    async def receive() -> dict[str, Any]:
        nonlocal connected
        if not connected:
            connected = True
            return {"type": "websocket.connect"}
        await answered.wait()
        return {"type": "websocket.disconnect", "code": 1000}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)
        if message["type"] in ("websocket.accept", "websocket.close"):
            answered.set()

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "http_version": "1.1",
        "path": "/ws/stream",
        "raw_path": b"/ws/stream",
        "root_path": "",
        "query_string": query.encode(),
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }
    await within(app(scope, receive, send), f"/ws/stream?{query} never ended")
    return sent


async def test_stream_replays_the_events_missed_after_after_id_then_goes_live(
    db: Database, tmp_path: Path,
) -> None:
    """A client reconnecting with after_id (the last event it received) first gets the events
    of its project emitted since then, then the live ones."""
    daemon = _make_daemon(db, tmp_path)
    try:
        seen = await daemon.bus.emit(type=EventType.log, project_id="proj-1", payload={"n": 0})
        await daemon.bus.emit(type=EventType.log, project_id="proj-2", payload={"n": 1})
        missed = await daemon.bus.emit(
            type=EventType.log, project_id="proj-1", payload={"n": 2}
        )

        ws = _FakeWebSocket(_AppShim(daemon), disconnect_after=2)
        handler = asyncio.create_task(stream(ws, project_id="proj-1", after_id=seen.id))
        await wait_until(lambda: len(ws.received) == 1, "the missed event was never replayed")
        live = await daemon.bus.emit(type=EventType.log, project_id="proj-1", payload={"n": 3})
        await within(handler, "the handler never ended")

        assert ws.received == [missed.model_dump(mode="json"), live.model_dump(mode="json")]
        assert daemon.bus.subscriber_count() == 0
    finally:
        await daemon.shutdown()


async def test_stream_without_after_id_sends_live_events_only(
    db: Database, tmp_path: Path,
) -> None:
    """Without after_id nothing is replayed: the stream starts with the next live event,
    sent as before."""
    daemon = _make_daemon(db, tmp_path)
    try:
        await daemon.bus.emit(type=EventType.log, project_id="proj-1", payload={"n": 0})

        ws = _FakeWebSocket(_AppShim(daemon), disconnect_after=1)
        handler = asyncio.create_task(stream(ws, project_id="proj-1"))
        await _wait_for_subscribers(daemon, 1)
        live = await daemon.bus.emit(type=EventType.log, project_id="proj-1", payload={"n": 1})
        await within(handler, "the handler never ended")

        assert ws.received == [live.model_dump(mode="json")]
    finally:
        await daemon.shutdown()


async def test_stream_sends_a_gap_frame_when_the_client_reads_too_slowly(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While the client is not reading, the subscription's queue overflows and drops events:
    the client gets a gap frame before the next event it receives."""
    daemon = _make_daemon(db, tmp_path)
    try:
        monkeypatch.setattr(
            daemon.bus, "subscribe", functools.partial(daemon.bus.subscribe, maxsize=2)
        )
        ws = _SlowWebSocket(_AppShim(daemon), disconnect_after=4)
        handler = asyncio.create_task(stream(ws))
        await _wait_for_subscribers(daemon, 1)
        await daemon.bus.emit(type=EventType.log, payload={"n": 0})
        await within(ws.sending.wait(), "the handler never sent the first event")
        for n in range(1, 5):
            await daemon.bus.emit(type=EventType.log, payload={"n": n})  # the queue keeps 3, 4
        ws.release.set()
        await within(handler, "the handler never ended")

        assert ws.received[0]["payload"] == {"n": 0}
        assert ws.received[1] == {"type": "stream.gap", "reason": "overflow"}
        assert [m["payload"] for m in ws.received[2:]] == [{"n": 3}, {"n": 4}]
        assert daemon.bus.subscriber_count() == 0
    finally:
        await daemon.shutdown()


@pytest.mark.parametrize("after_id", ["-1", "abc", "1.5", "", str(SQLITE_INT_MAX + 1)])
async def test_stream_closes_with_1008_on_a_bad_after_id(
    db: Database, tmp_path: Path, after_id: str,
) -> None:
    """after_id must be a non-negative integer (that SQLite can store): otherwise the socket is
    closed with 1008 before it is accepted, and nothing subscribes."""
    daemon = _make_daemon(db, tmp_path)
    app = create_app(daemon, manage_lifecycle=False)
    try:
        sent = await _open_through_app(app, f"after_id={after_id}")
        assert [(m["type"], m.get("code")) for m in sent] == [("websocket.close", 1008)]
        assert daemon.bus.subscriber_count() == 0

        accepted = await _open_through_app(app, "after_id=0")  # a valid one is accepted
        assert accepted[0]["type"] == "websocket.accept"
        assert daemon.bus.subscriber_count() == 0
    finally:
        await daemon.shutdown()
