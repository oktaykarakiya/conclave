"""FastAPI app factory. Serves the REST API, the WebSocket stream, and the built SPA."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..bootstrap import seed_global_defaults
from ..runtime import Daemon
from .api import router as api_router
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
    app.include_router(ws_router)
    app.add_middleware(_BodySizeLimitMiddleware, max_bytes=_MAX_BODY_BYTES)

    static_dir = Path(__file__).parent / "static"
    if (static_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="spa")
    else:

        @app.get("/", response_class=HTMLResponse)
        async def root() -> str:
            return _PLACEHOLDER

    return app
