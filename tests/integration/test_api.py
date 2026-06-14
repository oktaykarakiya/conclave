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
from conclave.db import repositories as repo
from conclave.db.models import TaskState
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


async def test_agents_seeded_and_editable(client: httpx.AsyncClient) -> None:
    agents = await client.get("/api/agents")
    names = {a["name"] for a in agents.json()}
    assert {"developer", "tester", "security", "reviewer", "planner"} <= names

    edit = await client.put(
        "/api/agents/developer", json={"role": "developer", "persona_md": "Edited persona"}
    )
    assert edit.status_code == 200
    assert edit.json()["persona_md"] == "Edited persona"


async def test_approve_in_progress_returns_409(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    """Approving a task that is already in_progress must return 409 and not change state."""
    repo_path = tmp_path / "repo409"
    await _init_repo(repo_path)

    created = await client.post(
        "/api/projects", json={"name": "p409", "path": str(repo_path), "default_branch": "main"}
    )
    assert created.status_code == 200
    project_id = created.json()["id"]

    task = await client.post(
        f"/api/projects/{project_id}/tasks", json={"request": "do a thing"}
    )
    assert task.status_code == 200
    task_id = task.json()["id"]

    # Manually move the task to in_progress (simulating a running worker)
    await repo.set_task_state(db, task_id, TaskState.in_progress)

    # Approving an in_progress task must fail with 409
    approve = await client.post(f"/api/tasks/{task_id}/approve")
    assert approve.status_code == 409
    assert "not in an approvable state" in approve.json()["detail"]

    # Task must still be in_progress
    get = await client.get(f"/api/tasks/{task_id}")
    assert get.json()["state"] == "in_progress"


async def test_approve_done_returns_409(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    """Approving a task that is already done must return 409."""
    repo_path = tmp_path / "repo409done"
    await _init_repo(repo_path)

    created = await client.post(
        "/api/projects", json={"name": "p409d", "path": str(repo_path), "default_branch": "main"}
    )
    assert created.status_code == 200
    project_id = created.json()["id"]

    task = await client.post(
        f"/api/projects/{project_id}/tasks", json={"request": "do a thing"}
    )
    assert task.status_code == 200
    task_id = task.json()["id"]

    # Manually set to done
    await repo.set_task_state(db, task_id, TaskState.done)

    approve = await client.post(f"/api/tasks/{task_id}/approve")
    assert approve.status_code == 409

    get = await client.get(f"/api/tasks/{task_id}")
    assert get.json()["state"] == "done"


async def test_approve_inbox_succeeds(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """Approving an inbox task must succeed (200) and transition to approved."""
    repo_path = tmp_path / "repo_inbox_ok"
    await _init_repo(repo_path)

    created = await client.post(
        "/api/projects",
        json={"name": "p_inbox_ok", "path": str(repo_path), "default_branch": "main"},
    )
    assert created.status_code == 200
    project_id = created.json()["id"]

    task = await client.post(
        f"/api/projects/{project_id}/tasks", json={"request": "do a thing"}
    )
    assert task.status_code == 200
    task_id = task.json()["id"]
    assert task.json()["state"] == "inbox"

    approve = await client.post(f"/api/tasks/{task_id}/approve")
    assert approve.status_code == 200

    get = await client.get(f"/api/tasks/{task_id}")
    assert get.json()["state"] == "approved"


async def test_approve_failed_succeeds(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    """Approving a failed task must succeed (200) and transition to approved."""
    repo_path = tmp_path / "repo_failed_ok"
    await _init_repo(repo_path)

    created = await client.post(
        "/api/projects",
        json={"name": "p_failed_ok", "path": str(repo_path), "default_branch": "main"},
    )
    assert created.status_code == 200
    project_id = created.json()["id"]

    task = await client.post(
        f"/api/projects/{project_id}/tasks", json={"request": "do a thing"}
    )
    assert task.status_code == 200
    task_id = task.json()["id"]

    # Manually set to failed
    await repo.set_task_state(db, task_id, TaskState.failed)

    # Approving a failed task must succeed
    approve = await client.post(f"/api/tasks/{task_id}/approve")
    assert approve.status_code == 200

    get = await client.get(f"/api/tasks/{task_id}")
    assert get.json()["state"] == "approved"


async def test_cascade_approve_failed_descendant(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    """Cascade-approve must approve a failed descendant of an inbox task."""
    repo_path = tmp_path / "repo_cascade_fail"
    await _init_repo(repo_path)

    created = await client.post(
        "/api/projects",
        json={"name": "p_cascade_fail", "path": str(repo_path), "default_branch": "main"},
    )
    assert created.status_code == 200
    project_id = created.json()["id"]

    # Create parent task
    parent = await client.post(
        f"/api/projects/{project_id}/tasks", json={"request": "parent task"}
    )
    assert parent.status_code == 200
    parent_id = parent.json()["id"]

    # Create child task linked to parent; we must use the repo directly since the
    # task creation API doesn't expose parent_task_id.
    child_task = await repo.create_task(
        db,
        project_id=project_id,
        request="child task",
        title="child",
        state=TaskState.failed,
        parent_task_id=parent_id,
    )

    # Cascade-approve the parent — the failed child must also be approved
    cascade = await client.post(f"/api/tasks/{parent_id}/cascade-approve")
    assert cascade.status_code == 200, cascade.text
    assert cascade.json()["count"] >= 2  # parent + child at minimum

    # Both parent and child must be approved
    get_parent = await client.get(f"/api/tasks/{parent_id}")
    assert get_parent.json()["state"] == "approved"

    get_child = await client.get(f"/api/tasks/{child_task.id}")
    assert get_child.json()["state"] == "approved"
