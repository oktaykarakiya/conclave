"""API tests via httpx ASGI transport (workers disabled for determinism)."""

from __future__ import annotations

import asyncio
import logging
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
from conclave.engine import WorktreeManager, run_git
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
        try:
            yield c
        finally:
            await daemon.shutdown()


@pytest_asyncio.fixture
async def client_with_workers(
    db: Database, tmp_path: Path
) -> AsyncIterator[httpx.AsyncClient]:
    """Fixture with workers enabled for testing worker lifecycle (detach/onboard)."""
    await seed_global_defaults(db)
    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=True)
    app = create_app(daemon, manage_lifecycle=False)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        try:
            yield c
        finally:
            await daemon.shutdown()


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

    # onboarding now runs in the background; poll until knowledge is ready
    import asyncio as _asyncio
    knowledge_data: dict = {}
    for _ in range(20):
        knowledge = await client.get(f"/api/projects/{project_id}/knowledge")
        knowledge_data = knowledge.json()
        if knowledge_data and knowledge_data.get("commands"):
            break
        await _asyncio.sleep(0.1)
    assert knowledge_data["commands"]["test"] == "npm test"

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


async def test_cascade_approve_cycle_safe(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    """Cascade-approve terminates on a cyclic parent_task_id (A → B → A) and
    approves each node exactly once."""
    repo_path = tmp_path / "repo_cascade_cycle"
    await _init_repo(repo_path)

    created = await client.post(
        "/api/projects",
        json={"name": "p_cascade_cycle", "path": str(repo_path), "default_branch": "main"},
    )
    assert created.status_code == 200
    project_id = created.json()["id"]

    # Create task A (inbox) via API.
    task_a = await client.post(
        f"/api/projects/{project_id}/tasks", json={"request": "task A"}
    )
    assert task_a.status_code == 200
    task_a_id = task_a.json()["id"]

    # Create task B as a child of A via repo.
    task_b = await repo.create_task(
        db,
        project_id=project_id,
        request="task B",
        title="task-b",
        state=TaskState.inbox,
        parent_task_id=task_a_id,
    )

    # Create the cycle: point A's parent at B via raw SQL.
    await db.execute(
        "UPDATE tasks SET parent_task_id = ? WHERE id = ?",
        (task_b.id, task_a_id),
    )

    # Cascade-approve from A — must terminate and approve each node once.
    cascade = await client.post(f"/api/tasks/{task_a_id}/cascade-approve")
    assert cascade.status_code == 200, cascade.text
    data = cascade.json()
    assert data["count"] == 2, f"Expected 2 approved tasks, got {data}"

    # Both tasks must be approved.
    a = await client.get(f"/api/tasks/{task_a_id}")
    assert a.json()["state"] == "approved"
    b = await client.get(f"/api/tasks/{task_b.id}")
    assert b.json()["state"] == "approved"


# --- pagination tests (DoS hardening — WEB-1) --------------------------------


async def test_list_tasks_pagination_honors_limit_and_offset(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    """List endpoints return at most `limit` items and honor `offset`."""
    repo_path = tmp_path / "repo_pag"
    await _init_repo(repo_path)

    created = await client.post(
        "/api/projects",
        json={"name": "p_pag", "path": str(repo_path), "default_branch": "main"},
    )
    assert created.status_code == 200
    project_id = created.json()["id"]

    # Create 5 tasks (they'll be inbox by default).
    task_ids: list[str] = []
    for i in range(5):
        t = await client.post(
            f"/api/projects/{project_id}/tasks", json={"request": f"task-{i}"}
        )
        assert t.status_code == 200
        task_ids.append(t.json()["id"])

    # Default pagination (limit=50) returns all 5.
    all_resp = await client.get(f"/api/projects/{project_id}/tasks")
    assert all_resp.status_code == 200
    assert len(all_resp.json()) == 5

    # limit=2 returns exactly 2.
    page1 = await client.get(
        f"/api/projects/{project_id}/tasks", params={"limit": 2, "offset": 0}
    )
    assert page1.status_code == 200
    assert len(page1.json()) == 2

    # offset=2 skips the first two.
    page2 = await client.get(
        f"/api/projects/{project_id}/tasks", params={"limit": 2, "offset": 2}
    )
    assert page2.status_code == 200
    assert len(page2.json()) == 2

    # The two pages must be disjoint.
    ids1 = {t["id"] for t in page1.json()}
    ids2 = {t["id"] for t in page2.json()}
    assert ids1.isdisjoint(ids2)

    # offset past the end returns empty.
    tail = await client.get(
        f"/api/projects/{project_id}/tasks", params={"limit": 10, "offset": 50}
    )
    assert tail.status_code == 200
    assert tail.json() == []


async def test_pagination_limit_is_clamped_to_max(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """Sending limit=9999 must be clamped to the enforced max (500), returning at most 500."""
    repo_path = tmp_path / "repo_pag_max"
    await _init_repo(repo_path)

    created = await client.post(
        "/api/projects",
        json={"name": "p_pag_max", "path": str(repo_path), "default_branch": "main"},
    )
    assert created.status_code == 200
    project_id = created.json()["id"]

    # Create several tasks.
    for i in range(3):
        await client.post(
            f"/api/projects/{project_id}/tasks", json={"request": f"task-{i}"}
        )

    # limit=9999 is clamped to 500 (Query(le=500) on the FastAPI param).
    resp = await client.get(
        f"/api/projects/{project_id}/tasks", params={"limit": 9999}
    )
    # FastAPI Query(le=500) will return 422 for values > 500.
    # Actually, Query(le=500) means the validation constraint is le=500.
    # If user sends 9999, FastAPI returns 422 Unprocessable Entity.
    assert resp.status_code == 422, (
        f"Expected 422 for limit > max, got {resp.status_code}: {resp.text}"
    )

    # limit=500 (at the cap) should succeed.
    resp_ok = await client.get(
        f"/api/projects/{project_id}/tasks", params={"limit": 500}
    )
    assert resp_ok.status_code == 200


async def test_get_task_usage_uses_sql_aggregation(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    """GET /api/tasks/{id}/usage returns SQL-aggregated totals, not per-row entries."""
    repo_path = tmp_path / "repo_usage_agg"
    await _init_repo(repo_path)

    created = await client.post(
        "/api/projects",
        json={"name": "p_usa", "path": str(repo_path), "default_branch": "main"},
    )
    assert created.status_code == 200
    project_id = created.json()["id"]

    t = await client.post(
        f"/api/projects/{project_id}/tasks", json={"request": "do work"}
    )
    assert t.status_code == 200
    task_id = t.json()["id"]

    # Add usage rows via the repo directly.
    await repo.add_usage(
        db, agent="dev", task_id=task_id, project_id=project_id,
        num_turns=2, input_tokens=100, output_tokens=50,
    )
    await repo.add_usage(
        db, agent="tester", task_id=task_id, project_id=project_id,
        num_turns=1, input_tokens=80, output_tokens=30,
    )

    usage = await client.get(f"/api/tasks/{task_id}/usage")
    assert usage.status_code == 200
    data = usage.json()
    assert data["task_id"] == task_id
    assert data["total_turns"] == 3
    assert data["input_tokens"] == 180
    assert data["output_tokens"] == 80
    assert data["agent_count"] == 2
    # The old "entries" key (unbounded per-row list) must NOT be present.
    assert "entries" not in data


# --- body-size rejection tests (DoS hardening — WEB-1) -----------------------


async def test_body_size_middleware_rejects_content_length_over_limit(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """POST with Content-Length > 2 MiB returns 413."""

    async def _send_oversized() -> httpx.Response:
        return await client.post(
            "/api/projects",
            headers={"Content-Length": str(3 * 1024 * 1024)},  # 3 MiB
            content="x",  # httpx will override with the declared length
        )

    # httpx may not let us send a mismatched Content-Length easily.
    # Instead, test with a body that genuinely exceeds 2 MiB by sending
    # it as raw bytes with a properly matching Content-Length header.
    # We use a smaller-than-max test to keep the test lightweight:
    # send an empty body but declare Content-Length as 3 MiB.
    import httpx as _httpx

    transport = _httpx.ASGITransport(app=client._transport.app)  # type: ignore[union-attr]
    async with _httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/api/secrets",
            json={"name": "test", "value": "val"},
        )
        # Normal request passes through.
        assert resp.status_code == 200

        # Send with Content-Length > 2 MiB via raw request construction.
        resp_big = await c.request(
            "POST",
            "/api/secrets",
            headers={"Content-Length": str(3 * 1024 * 1024)},
            content=b"x" * 100,
        )
        assert resp_big.status_code == 413
        assert "size limit" in resp_big.json()["detail"].lower()


async def test_body_size_middleware_accepts_at_or_below_limit(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """Normal-sized POST body passes through the middleware."""
    repo_path = tmp_path / "repo_bs_ok"
    await _init_repo(repo_path)

    # This body is well under 2 MiB — must succeed.
    resp = await client.post(
        "/api/projects",
        json={"name": "bs_ok", "path": str(repo_path), "default_branch": "main"},
    )
    assert resp.status_code == 200, resp.text


# --- task-state filter validation --------------------------------------------


async def test_list_tasks_rejects_bogus_state(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """GET .../tasks?state=bogus must return 422 (FastAPI enum validation), not 500."""
    repo_path = tmp_path / "repo_state_val"
    await _init_repo(repo_path)

    created = await client.post(
        "/api/projects",
        json={"name": "state_val", "path": str(repo_path), "default_branch": "main"},
    )
    assert created.status_code == 200
    project_id = created.json()["id"]

    resp = await client.get(
        f"/api/projects/{project_id}/tasks", params={"state": "bogus"}
    )
    assert resp.status_code == 422, (
        f"Expected 422 for bogus state, got {resp.status_code}: {resp.text}"
    )


async def test_list_tasks_accepts_valid_state(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """GET .../tasks?state=approved must return 200 with filtered results."""
    repo_path = tmp_path / "repo_state_ok"
    await _init_repo(repo_path)

    created = await client.post(
        "/api/projects",
        json={"name": "state_ok", "path": str(repo_path), "default_branch": "main"},
    )
    assert created.status_code == 200
    project_id = created.json()["id"]

    resp = await client.get(
        f"/api/projects/{project_id}/tasks", params={"state": "approved"}
    )
    assert resp.status_code == 200


# --- quarantine until-date validation ----------------------------------------


async def test_add_quarantine_rejects_malformed_until(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """POST .../quarantine with a malformed until date must return 422."""
    repo_path = tmp_path / "repo_q_malformed"
    await _init_repo(repo_path)

    created = await client.post(
        "/api/projects",
        json={"name": "q_mal", "path": str(repo_path), "default_branch": "main"},
    )
    assert created.status_code == 200
    project_id = created.json()["id"]

    resp = await client.post(
        f"/api/projects/{project_id}/quarantine",
        json={"pattern": "*.tmp", "reason": "test", "until": "not-a-date"},
    )
    assert resp.status_code == 422, (
        f"Expected 422 for malformed until, got {resp.status_code}: {resp.text}"
    )


async def test_add_quarantine_accepts_valid_until(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """POST .../quarantine with a valid YYYY-MM-DD date must return 200 and create the entry."""
    repo_path = tmp_path / "repo_q_valid"
    await _init_repo(repo_path)

    created = await client.post(
        "/api/projects",
        json={"name": "q_ok", "path": str(repo_path), "default_branch": "main"},
    )
    assert created.status_code == 200
    project_id = created.json()["id"]

    resp = await client.post(
        f"/api/projects/{project_id}/quarantine",
        json={"pattern": "*.tmp", "reason": "test", "until": "2026-12-31"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["pattern"] == "*.tmp"
    assert data["until"] == "2026-12-31"

    # Verify it appears in the listing
    listing = await client.get(f"/api/projects/{project_id}/quarantine")
    assert listing.status_code == 200
    entries = listing.json()
    assert any(e["until"] == "2026-12-31" for e in entries)


# --- detach_project tests ----------------------------------------------------


async def test_detach_project_stops_worker_and_cleans_orphans(
    client_with_workers: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    """DELETE /projects/{id} stops the worker, cleans in-progress worktrees,
    and cascade-deletes all child rows."""
    repo_path = tmp_path / "repo_detach"
    await _init_repo(repo_path)

    created = await client_with_workers.post(
        "/api/projects",
        json={"name": "detach-me", "path": str(repo_path), "default_branch": "main"},
    )
    assert created.status_code == 200, created.text
    project_id = created.json()["id"]

    # Verify the worker was registered.
    daemon = client_with_workers._transport.app.state.daemon
    assert project_id in daemon._workers

    # Create an in_progress task directly in the DB (simulating a running worker).
    task = await repo.create_task(
        db, project_id=project_id, request="test task", state=TaskState.in_progress
    )

    # Create a real git worktree so we can verify disk cleanup.
    wm = WorktreeManager(
        repo_path, daemon.home / "projects" / project_id / "worktrees"
    )
    task_branch = f"conclave/{task.id}"
    worktree_path = await wm.create(task.id, "main", task_branch)
    assert worktree_path.is_dir(), "worktree was not created on disk"

    # --- detach ---
    resp = await client_with_workers.delete(f"/api/projects/{project_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"detached": project_id}

    # Worker must be removed from the registry.
    assert project_id not in daemon._workers, "worker was not removed from _workers"

    # Worktree directory must be gone.
    assert not worktree_path.exists(), "worktree directory was not cleaned up"

    # Project must be deleted from DB.
    assert await repo.get_project(db, project_id) is None, "project row still exists"

    # Task must be cascade-deleted.
    assert await repo.get_task(db, task.id) is None, "task row was not cascade-deleted"


async def test_detach_project_idempotent(
    client_with_workers: httpx.AsyncClient, tmp_path: Path
) -> None:
    """Detaching the same project twice must not 500."""
    repo_path = tmp_path / "repo_detach_idem"
    await _init_repo(repo_path)

    created = await client_with_workers.post(
        "/api/projects",
        json={"name": "detach-twice", "path": str(repo_path), "default_branch": "main"},
    )
    assert created.status_code == 200, created.text
    project_id = created.json()["id"]

    # First detach.
    r1 = await client_with_workers.delete(f"/api/projects/{project_id}")
    assert r1.status_code == 200, r1.text

    # Second detach — must not 500 (project already gone).
    r2 = await client_with_workers.delete(f"/api/projects/{project_id}")
    assert r2.status_code == 200, f"second detach returned {r2.status_code}: {r2.text}"
    assert r2.json() == {"detached": project_id}


# --- create_project tests ----------------------------------------------------


async def test_create_project_returns_before_onboarding_finishes(
    client_with_workers: httpx.AsyncClient, tmp_path: Path
) -> None:
    """POST /projects returns before the background onboarding completes."""
    import conclave.runtime as runtime_mod

    repo_path = tmp_path / "repo_fast_create"
    await _init_repo(repo_path)

    # Replace onboard with a blocking stub so onboarding never finishes
    # during the test window.
    original_onboard = runtime_mod.onboard
    block_event = asyncio.Event()

    async def _blocking_onboard(*args, **kwargs):  # type: ignore[no-untyped-def]
        await block_event.wait()

    runtime_mod.onboard = _blocking_onboard
    try:
        created = await client_with_workers.post(
            "/api/projects",
            json={"name": "fast-create", "path": str(repo_path), "default_branch": "main"},
        )
        assert created.status_code == 200, created.text
        project_id = created.json()["id"]

        # Project must be immediately queryable.
        get_resp = await client_with_workers.get(f"/api/projects/{project_id}")
        assert get_resp.status_code == 200, "project not queryable after create"

        # Worker must have been started.
        daemon = client_with_workers._transport.app.state.daemon
        assert project_id in daemon._workers, "worker was not started"

        # Knowledge must NOT exist yet (onboarding hasn't finished).
        knowledge = await client_with_workers.get(
            f"/api/projects/{project_id}/knowledge"
        )
        assert knowledge.json() == {}, "knowledge exists before onboarding finished"
    finally:
        # Unblock the stubbed onboard so the background task can drain.
        runtime_mod.onboard = original_onboard
        block_event.set()
        # Give the background task a moment to complete.
        await asyncio.sleep(0.2)


async def test_create_project_onboarding_failure_not_fatal(
    client_with_workers: httpx.AsyncClient, tmp_path: Path, caplog
) -> None:
    """POST /projects returns 200 even when onboarding raises; project is usable."""
    import conclave.runtime as runtime_mod

    repo_path = tmp_path / "repo_onboard_fail"
    await _init_repo(repo_path)

    original_onboard = runtime_mod.onboard

    async def _failing_onboard(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated onboarding failure")

    caplog.set_level(logging.ERROR, logger="conclave.runtime")
    runtime_mod.onboard = _failing_onboard
    try:
        created = await client_with_workers.post(
            "/api/projects",
            json={"name": "onboard-fail", "path": str(repo_path), "default_branch": "main"},
        )
        assert created.status_code == 200, (
            f"expected 200 despite onboarding failure, got {created.status_code}: {created.text}"
        )
        project_id = created.json()["id"]

        # Project must be queryable.
        get_resp = await client_with_workers.get(f"/api/projects/{project_id}")
        assert get_resp.status_code == 200, "project not queryable after failed onboarding"

        # Worker must have been started.
        daemon = client_with_workers._transport.app.state.daemon
        assert project_id in daemon._workers, "worker was not started after onboarding failure"

        # The failure must have been logged.
        await asyncio.sleep(0.2)
        error_records = [r for r in caplog.records if "onboarding failed" in r.message]
        assert len(error_records) >= 1, (
            f"expected onboarding failure to be logged, got records: "
            f"{[r.message for r in caplog.records]}"
        )
    finally:
        runtime_mod.onboard = original_onboard
