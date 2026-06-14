"""API tests via httpx ASGI transport (workers disabled for determinism)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest_asyncio
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
    (path / "package.json").write_text('{"scripts": {"test": "echo ok"}}\n')
    await run_git(path, "add", "-A")
    await run_git(path, "commit", "-m", "init")


@pytest_asyncio.fixture
async def client(db: Database, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    await seed_global_defaults(db)
    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_health_and_schema(client: httpx.AsyncClient) -> None:
    health = await client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    schema = await client.get("/api/config/schema")
    assert schema.status_code == 200
    assert "execution" in schema.json()["properties"]


async def test_project_task_flow(client: httpx.AsyncClient, tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)

    created = await client.post(
        "/api/projects", json={"name": "demo", "path": str(repo_path), "default_branch": "main"}
    )
    assert created.status_code == 200, created.text
    project_id = created.json()["id"]

    # onboarding learned the test command from package.json
    knowledge = await client.get(f"/api/projects/{project_id}/knowledge")
    assert knowledge.json()["commands"]["test"] == "npm test"

    # create a task (inbox), then approve it
    task = await client.post(
        f"/api/projects/{project_id}/tasks", json={"request": "do a thing"}
    )
    assert task.status_code == 200
    task_id = task.json()["id"]
    assert task.json()["state"] == "inbox"

    approve = await client.post(f"/api/tasks/{task_id}/approve")
    assert approve.status_code == 200

    approved = await client.get(f"/api/projects/{project_id}/tasks", params={"state": "approved"})
    assert [t["id"] for t in approved.json()] == [task_id]

    # task events include creation + approval
    events = await client.get(f"/api/tasks/{task_id}/events")
    types = {e["type"] for e in events.json()}
    assert {"task.created", "task.approved"} <= types


async def test_project_requires_git_repo(client: httpx.AsyncClient, tmp_path: Path) -> None:
    plain = tmp_path / "not_a_repo"
    plain.mkdir()
    resp = await client.post("/api/projects", json={"name": "x", "path": str(plain)})
    assert resp.status_code == 400


async def test_engine_profiles_crud_and_test(client: httpx.AsyncClient) -> None:
    # create a DeepSeek-style env-routed profile with a secret token
    resp = await client.post(
        "/api/profiles",
        json={
            "name": "deepseek",
            "arg_mode": "env",
            "base_url": "https://api.deepseek.com/anthropic",
            "model": "deepseek-v4-pro",
            "subagent_model": "deepseek-v4-flash",
            "effort": "max",
            "auth_token": "sk-secret",
        },
    )
    assert resp.status_code == 200
    profile = resp.json()
    assert profile["model"] == "deepseek-v4-pro"
    assert profile["auth_secret_id"]  # token was stored as a secret and linked

    listing = await client.get("/api/profiles")
    names = {p["name"] for p in listing.json()}
    assert {"system-default", "deepseek"} <= names

    # the Test button: probe the system-default profile (fake provider => ok)
    test = await client.post(
        "/api/profiles/test", json={"name": "system-default", "arg_mode": "inherit"}
    )
    assert test.status_code == 200
    assert test.json()["ok"] is True


async def test_secrets_are_write_only(client: httpx.AsyncClient) -> None:
    await client.post("/api/secrets", json={"name": "my_key", "value": "super-secret"})
    listing = await client.get("/api/secrets")
    assert listing.json() == ["my_key"]  # names only, never values


async def test_task_response_includes_level(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    """Task GET/list responses must include the 'level' field (scale-adaptive planning)."""
    from conclave.db.repositories import create_task as repo_create_task

    repo_path = tmp_path / "repo_lvl"
    await _init_repo(repo_path)

    created = await client.post(
        "/api/projects",
        json={"name": "lvl", "path": str(repo_path), "default_branch": "main"},
    )
    assert created.status_code == 200, created.text
    project_id = created.json()["id"]

    # Persist a task with an explicit level via the repo, then read it back
    # through the API to prove the field round-trips through serialization.
    task = await repo_create_task(
        db, project_id=project_id, request="test level exposure", level=3
    )

    # Single-task GET
    resp = await client.get(f"/api/tasks/{task.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert "level" in body, f"level key missing from GET /api/tasks/{{id}}: {body}"
    assert body["level"] == 3

    # Task list GET
    list_resp = await client.get(f"/api/projects/{project_id}/tasks")
    assert list_resp.status_code == 200
    tasks = list_resp.json()
    assert len(tasks) == 1
    assert "level" in tasks[0], f"level key missing from task in list: {tasks[0]}"
    assert tasks[0]["level"] == 3


async def test_agents_seeded_and_editable(client: httpx.AsyncClient) -> None:
    agents = await client.get("/api/agents")
    names = {a["name"] for a in agents.json()}
    assert {"developer", "tester", "security", "reviewer", "planner"} <= names

    edit = await client.put(
        "/api/agents/developer", json={"role": "developer", "persona_md": "Edited persona"}
    )
    assert edit.status_code == 200
    assert edit.json()["persona_md"] == "Edited persona"
