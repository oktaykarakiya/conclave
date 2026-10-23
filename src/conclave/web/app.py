"""FastAPI app factory. Serves the REST API, the WebSocket stream, and the built SPA."""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..bootstrap import seed_global_defaults
from ..db import repositories as repo
from ..runtime import Daemon
from .api import router as api_router
from .mcp_routes import router as mcp_router
from .ws import router as ws_router

# Maximum request body size (2 MiB) — DoS hardening (WEB-1).
_MAX_BODY_BYTES = 2 * 1024 * 1024

_Scope = dict[str, Any]
_Receive = Callable[[], Awaitable[dict[str, Any]]]
_Send = Callable[[dict[str, Any]], Awaitable[None]]
_ASGIApp = Callable[[_Scope, _Receive, _Send], Awaitable[None]]

_PLACEHOLDER = (
    "<!doctype html><html><head><title>Conclave</title></head><body "
    "style='font-family:system-ui;max-width:42rem;margin:4rem auto;color:#222'>"
    "<h1>Conclave</h1><p>The API is running at <code>/api</code> and the live event "
    "stream at <code>/ws/stream</code>.</p><p>The web UI has not been built yet — run "
    "<code>npm install &amp;&amp; npm run build</code> in <code>frontend/</code>.</p></body></html>"
)


class _BodySizeLimitMiddleware:
    """Reject request bodies larger than *max_bytes* (DoS hardening — WEB-1).

    Two code paths:
    1. **Content-Length** — 413 immediately if the declared size exceeds the cap. This is the
       fast path that handles virtually all real-world clients.
    2. **Chunked transfer** — buffer up to *max_bytes*, then 413 if exceeded. This path is
       rare but needed for streaming clients that don't send a Content-Length.
    """

    def __init__(self, app: _ASGIApp, max_bytes: int = _MAX_BODY_BYTES) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        if method not in ("POST", "PUT", "PATCH"):
            await self._app(scope, receive, send)
            return

        # Fast path — Content-Length is declared and exceeds the cap.
        content_length: int | None = None
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    content_length = int(value)
                except ValueError:
                    pass
                break

        if content_length is not None and content_length > self._max_bytes:
            await _send_413(send)
            return

        # Chunked transfer (no Content-Length) — buffer body chunks up to the cap.
        if content_length is None:
            await self._handle_chunked(scope, receive, send)
            return

        # Content-Length is within bounds — pass through.
        await self._app(scope, receive, send)

    async def _handle_chunked(
        self, scope: _Scope, receive: _Receive, send: _Send
    ) -> None:
        body = bytearray()
        more_body = True

        while more_body:
            message = await receive()
            if message["type"] == "http.request":
                body.extend(message.get("body", b""))
                more_body = message.get("more_body", False)

            if len(body) > self._max_bytes:
                await _send_413(send)
                # Drain remaining chunks so the client connection doesn't stall.
                while more_body:
                    message = await receive()
                    more_body = message.get("more_body", False)
                return

        # Replay the accumulated body as a single message for the downstream app.
        _replayed = False

        async def _reply_receive() -> dict[str, Any]:
            nonlocal _replayed
            if _replayed:
                return await receive()
            _replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self._app(scope, _reply_receive, send)


async def _send_413(send: _Send) -> None:
    """Send a 413 Payload Too Large response directly over ASGI."""
    body = (
        f'{{"detail":"Request body exceeds the {_MAX_BODY_BYTES // (1024 * 1024)} MiB '
        f'size limit."}}'.encode()
    )
    await send({
        "type": "http.response.start",
        "status": 413,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


def _allowed_origins() -> set[str]:
    raw = os.environ.get("CONCLAVE_ALLOWED_ORIGINS", "")
    return {o.strip() for o in raw.split(",") if o.strip()}


class _OriginGuardMiddleware:
    """Block cross-origin **mutating** HTTP requests (CSRF) and **WebSocket** connections
    (CSWSH) from a browser origin that is neither same-origin nor allowlisted.

    Rationale: Conclave is unauthenticated by design, so a malicious site the operator
    visits could otherwise POST to the API or open ``ws://`` to read the event stream
    (WebSockets bypass the same-origin policy). Same-origin requests — the SPA, including
    from another device where the page is served by and connects to the same host:port —
    and non-browser clients (no ``Origin`` header, e.g. curl) are always allowed.
    Read-only cross-origin GETs are already prevented from reading the response by the
    browser's same-origin policy, so they are not blocked here. Extra origins may be
    permitted via the ``CONCLAVE_ALLOWED_ORIGINS`` env (comma-separated).
    """

    _MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

    def __init__(self, app: _ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        typ = scope.get("type")
        if typ not in ("http", "websocket") or (
            typ == "http" and scope.get("method", "GET") not in self._MUTATING
        ):
            await self._app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        origin = headers.get(b"origin")
        if origin is None:  # non-browser client (curl, server-to-server, tests)
            await self._app(scope, receive, send)
            return

        host = headers.get(b"host", b"").decode("latin-1")
        origin_s = origin.decode("latin-1")
        same_origin = origin_s.split("://", 1)[-1] == host
        if same_origin or origin_s in _allowed_origins():
            await self._app(scope, receive, send)
            return

        # Cross-origin browser request to a guarded surface — reject.
        if typ == "http":
            await _send_403_origin(send)
        else:
            with contextlib.suppress(Exception):
                await receive()  # consume websocket.connect before refusing the handshake
            await send({"type": "websocket.close", "code": 1008})


async def _send_403_origin(send: _Send) -> None:
    body = b'{"detail":"Cross-origin request blocked (Origin not allowed)."}'
    await send({
        "type": "http.response.start",
        "status": 403,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


async def _send_json(
    send: _Send,
    status: int,
    payload: Any,
    *,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> None:
    body = json.dumps(payload).encode("utf-8")
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": headers,
    })
    await send({"type": "http.response.body", "body": body})


class _IdempotencyMiddleware:
    """Persist and replay 2xx JSON responses for mutating requests carrying ``Idempotency-Key``."""

    _METHODS = frozenset({"POST", "PUT", "PATCH"})
    _MAX_KEY_LEN = 128

    def __init__(self, app: _ASGIApp, daemon: Daemon) -> None:
        self._app = app
        self._daemon = daemon

    async def __call__(self, scope: _Scope, receive: _Receive, send: _Send) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        method = str(scope.get("method", "GET")).upper()
        if method not in self._METHODS:
            await self._app(scope, receive, send)
            return

        key: str | None = None
        for name, value in scope.get("headers", []):
            if name.lower() == b"idempotency-key":
                key = value.decode("latin-1").strip()
                break

        if not key:
            await self._app(scope, receive, send)
            return

        if len(key) > self._MAX_KEY_LEN:
            await _send_json(
                send,
                400,
                {"detail": "Idempotency-Key exceeds 128 characters"},
            )
            return

        path = str(scope.get("path", ""))
        existing = await repo.get_idempotency_record(self._daemon.db, key)
        if existing is not None:
            if existing.method != method or existing.path != path:
                await _send_json(
                    send,
                    409,
                    {
                        "detail": (
                            "Idempotency-Key already used with a different request method or path"
                        )
                    },
                )
                return
            await _send_json(
                send,
                existing.status_code,
                existing.response,
                extra_headers=[(b"x-idempotency-replayed", b"true")],
            )
            return

        status_code = 200
        body_chunks = bytearray()

        async def _capturing_send(message: dict[str, Any]) -> None:
            nonlocal status_code
            msg_type = message.get("type")
            if msg_type == "http.response.start":
                status_code = int(message.get("status", 200))
                await send(message)
                return
            if msg_type == "http.response.body":
                body_chunks.extend(message.get("body", b""))
                if not message.get("more_body", False) and 200 <= status_code < 300 and body_chunks:
                    with contextlib.suppress(Exception):
                        parsed_body = json.loads(bytes(body_chunks).decode("utf-8"))
                        await repo.save_idempotency_record(
                            self._daemon.db,
                            key=key,
                            method=method,
                            path=path,
                            status_code=status_code,
                            response=parsed_body,
                        )
                await send(message)
                return
            await send(message)

        await self._app(scope, receive, _capturing_send)


def create_app(daemon: Daemon, *, manage_lifecycle: bool = True) -> FastAPI:
    """Build the app. When ``manage_lifecycle`` is set, the app connects the DB, seeds
    defaults, and starts workers on startup (and tears down on shutdown). Tests that
    manage the DB themselves pass ``manage_lifecycle=False``."""

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if manage_lifecycle:
            await daemon.db.connect()
            await seed_global_defaults(daemon.db)
            await daemon.start()
        yield
        if manage_lifecycle:
            await daemon.shutdown()
            await daemon.db.close()

    app = FastAPI(title="Conclave", version=__version__, lifespan=lifespan)
    app.state.daemon = daemon
    app.include_router(api_router)
    app.include_router(mcp_router)
    app.include_router(ws_router)
    app.add_middleware(_IdempotencyMiddleware, daemon=daemon)
    app.add_middleware(_BodySizeLimitMiddleware, max_bytes=_MAX_BODY_BYTES)
    app.add_middleware(_OriginGuardMiddleware)

    static_dir = Path(__file__).parent / "static"
    if (static_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="spa")
    else:

        @app.get("/", response_class=HTMLResponse)
        async def root() -> str:
            return _PLACEHOLDER

    return app
