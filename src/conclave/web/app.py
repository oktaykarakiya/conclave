"""FastAPI app factory. Serves the REST API, the WebSocket stream, and the built SPA."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..bootstrap import seed_global_defaults
from ..runtime import Daemon
from .api import router as api_router
from .ws import router as ws_router

_PLACEHOLDER = (
    "<!doctype html><html><head><title>Conclave</title></head><body "
    "style='font-family:system-ui;max-width:42rem;margin:4rem auto;color:#222'>"
    "<h1>Conclave</h1><p>The API is running at <code>/api</code> and the live event "
    "stream at <code>/ws/stream</code>.</p><p>The web UI has not been built yet — run "
    "<code>npm install &amp;&amp; npm run build</code> in <code>frontend/</code>.</p></body></html>"
)


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

    static_dir = Path(__file__).parent / "static"
    if (static_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="spa")
    else:

        @app.get("/", response_class=HTMLResponse)
        async def root() -> str:
            return _PLACEHOLDER

    return app
