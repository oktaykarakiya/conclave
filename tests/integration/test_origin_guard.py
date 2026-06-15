"""Cross-origin guard: mutating requests from a foreign Origin are rejected (CSRF/CSWSH)."""

from __future__ import annotations

from pathlib import Path

import httpx
from fake_provider import FakeProvider
from httpx import ASGITransport

from conclave.db import Database
from conclave.runtime import Daemon
from conclave.web import create_app


async def test_origin_guard(db: Database, tmp_path: Path) -> None:
    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Cross-origin browser POST -> blocked by the guard with 403, before the handler.
        cross = await client.post(
            "/api/projects/bogus/pause", headers={"Origin": "http://evil.example"}
        )
        assert cross.status_code == 403, cross.text

        # Same-origin (Origin host == Host "test") -> guard passes (handler runs; not 403).
        same = await client.post(
            "/api/projects/bogus/pause", headers={"Origin": "http://test"}
        )
        assert same.status_code != 403

        # No Origin (non-browser client like curl) -> guard passes.
        none = await client.post("/api/projects/bogus/pause")
        assert none.status_code != 403

        # Read-only GET is never blocked (same-origin policy already protects the response).
        get = await client.get("/api/projects", headers={"Origin": "http://evil.example"})
        assert get.status_code == 200
