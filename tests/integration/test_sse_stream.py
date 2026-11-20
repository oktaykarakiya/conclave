"""The server-sent event stream ``GET /api/events/stream``: ``id:`` lines, the replay after
``after_id`` or ``Last-Event-ID``, the gap frame, and input validation."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fake_provider import FakeProvider
from httpx import ASGITransport

from conclave.db import Database, EventRow
from conclave.db.database import SQLITE_INT_MAX
from conclave.events import EventType
from conclave.events.stream import REPLAY_MAX
from conclave.runtime import Daemon
from conclave.web import create_app
from waits import wait_until, within


class _EventStream:
    """``GET`` an event stream through the ASGI app and read it as it arrives: httpx's ASGI
    transport answers only once the app has finished the response, which an event stream never
    does. Leaving the ``async with`` disconnects the client and waits for the app to end."""

    def __init__(self, app: Any, target: str, headers: dict[str, str] | None = None) -> None:
        path, _, query = target.partition("?")
        self._app = app
        self._scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "root_path": "",
            "query_string": query.encode(),
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
        }
        self._requested = False
        self._gone = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.status: int | None = None
        self.body = ""

    async def _receive(self) -> dict[str, Any]:
        if not self._requested:
            self._requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._gone.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"").decode()

    def messages(self) -> list[str]:
        """The complete server-sent events received so far."""
        return self.body.split("\n\n")[:-1]

    async def wait_for(self, count: int) -> list[str]:
        await wait_until(
            lambda: len(self.messages()) >= count, f"the stream never sent {count} events"
        )
        return self.messages()

    async def __aenter__(self) -> _EventStream:
        self._task = asyncio.create_task(self._app(self._scope, self._receive, self._send))
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._gone.set()
        assert self._task is not None
        await within(self._task, "the stream never ended after the client went away")


def _sse(event: EventRow) -> str:
    return f"id: {event.id}\ndata: {json.dumps(event.model_dump(mode='json'))}"


@pytest_asyncio.fixture
async def daemon(db: Database, tmp_path: Path) -> AsyncIterator[Daemon]:
    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    try:
        yield daemon
    finally:
        await daemon.shutdown()


async def _wait_for_subscriber(daemon: Daemon) -> None:
    await wait_until(lambda: daemon.bus.subscriber_count() == 1, "the stream never subscribed")


async def test_each_event_has_an_id_line_and_last_event_id_resumes_after_it(
    daemon: Daemon,
) -> None:
    """Each event carries an ``id:`` line, and a client reconnecting with ``Last-Event-ID``
    first gets the events emitted since then. The header wins over the query's after_id: an
    EventSource reconnects with its first URL and the last id it received in the header."""
    app = create_app(daemon, manage_lifecycle=False)
    seen = await daemon.bus.emit(type=EventType.log, payload={"n": 0})
    missed = await daemon.bus.emit(type=EventType.log, payload={"n": 1})

    target = "/api/events/stream?after_id=0"
    async with _EventStream(app, target, {"Last-Event-ID": str(seen.id)}) as stream:
        await stream.wait_for(1)
        live = await daemon.bus.emit(type=EventType.log, payload={"n": 2})
        await stream.wait_for(2)

    assert stream.status == 200
    assert stream.messages() == [_sse(missed), _sse(live)]
    assert daemon.bus.subscriber_count() == 0


async def test_after_id_replays_the_project_events_missed_since_then(daemon: Daemon) -> None:
    app = create_app(daemon, manage_lifecycle=False)
    seen = await daemon.bus.emit(type=EventType.log, project_id="p1", payload={"n": 0})
    await daemon.bus.emit(type=EventType.log, project_id="p2", payload={"n": 1})
    missed = await daemon.bus.emit(type=EventType.log, project_id="p1", payload={"n": 2})

    target = f"/api/events/stream?project_id=p1&after_id={seen.id}"
    async with _EventStream(app, target) as stream:
        await stream.wait_for(1)

    assert stream.messages() == [_sse(missed)]


async def test_without_after_id_the_stream_is_live_only(daemon: Daemon) -> None:
    app = create_app(daemon, manage_lifecycle=False)
    await daemon.bus.emit(type=EventType.log, payload={"n": 0})

    async with _EventStream(app, "/api/events/stream") as stream:
        await _wait_for_subscriber(daemon)
        live = await daemon.bus.emit(type=EventType.log, payload={"n": 1})
        await stream.wait_for(1)

    assert stream.messages() == [_sse(live)]


async def test_the_gap_frame_has_no_id_line(daemon: Daemon, db: Database) -> None:
    """More missed events than the replay sends: a gap frame without an ``id:`` line (so an
    EventSource keeps its last event id), then the newest events, each with its id."""
    app = create_app(daemon, manage_lifecycle=False)
    async with db.transaction() as conn:
        await conn.executemany(
            "INSERT INTO events(type, payload_json, ts) VALUES ('log', '{}', '')",
            [() for _ in range(REPLAY_MAX + 1)],
        )

    newest = await db.fetchall(
        "SELECT id FROM events ORDER BY id DESC LIMIT ?", (REPLAY_MAX,)
    )

    async with _EventStream(app, "/api/events/stream?after_id=0") as stream:
        messages = await stream.wait_for(REPLAY_MAX + 1)

    assert messages[0] == 'data: {"type": "stream.gap", "reason": "replay_truncated"}'
    assert [m.split("\n")[0] for m in messages[1:]] == [
        f"id: {row['id']}" for row in reversed(newest)
    ]


@pytest.mark.parametrize(
    ("query", "headers"),
    [
        ("after_id=-1", {}),
        ("after_id=abc", {}),
        ("after_id=1.5", {}),
        ("after_id=", {}),
        (f"after_id={SQLITE_INT_MAX + 1}", {}),
        ("", {"Last-Event-ID": "-1"}),
        ("", {"Last-Event-ID": "abc"}),
        ("after_id=3", {"Last-Event-ID": f"{SQLITE_INT_MAX + 1}"}),
    ],
)
async def test_a_bad_after_id_or_last_event_id_is_422(
    daemon: Daemon, query: str, headers: dict[str, str]
) -> None:
    """after_id and Last-Event-ID must be non-negative integers that SQLite can store: else 422,
    never a 500, and nothing subscribes."""
    app = create_app(daemon, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get(f"/api/events/stream?{query}", headers=headers)
    assert resp.status_code == 422, resp.text
    assert daemon.bus.subscriber_count() == 0
