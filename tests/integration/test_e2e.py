"""Full end-to-end: create a task via the API, a real worker runs it to merge."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from fake_provider import FakeProvider
from httpx import ASGITransport

from conclave.bootstrap import seed_global_defaults
from conclave.db import Database
from conclave.engine import run_git
from conclave.runtime import Daemon
from conclave.web import create_app


async def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    await run_git(path, "config", "user.email", "t@example.com")
    await run_git(path, "config", "user.name", "T")
    (path / "README.md").write_text("# e2e\n")
    await run_git(path, "add", "-A")
    await run_git(path, "commit", "-m", "init")


async def test_full_cycle_via_api(db: Database, tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    await seed_global_defaults(db)

    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=True)
    app = create_app(daemon, manage_lifecycle=False)
    transport = ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        try:
            created = await client.post(
                "/api/projects",
                json={"name": "e2e", "path": str(repo_path), "default_branch": "main"},
            )
            assert created.status_code == 200, created.text
            project_id = created.json()["id"]

            task = await client.post(
                f"/api/projects/{project_id}/tasks",
                json={"request": "add a feature file", "auto_approve": True},
            )
            task_id = task.json()["id"]

            state = ""
            for _ in range(150):  # up to ~15s
                state = (await client.get(f"/api/tasks/{task_id}")).json()["state"]
                if state in ("done", "failed"):
                    break
                await asyncio.sleep(0.1)

            assert state == "done", f"task ended in state {state!r}"
            code, out = await run_git(repo_path, "show", "main:FEATURE.txt")
            assert code == 0 and "done" in out
        finally:
            await daemon.shutdown()
