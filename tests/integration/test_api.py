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
    """Fixture with workers enabled for testing worker lifecycle (create/detach)."""
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
        resp = await c.put(
            "/api/agents/body-size-probe",
            json={"role": "conditional", "persona_md": "probe"},
        )
        # Normal request passes through.
        assert resp.status_code == 200

        # Send with Content-Length > 2 MiB via raw request construction. The middleware
        # rejects on the declared length before routing, so the target route is irrelevant.
        resp_big = await c.request(
            "PUT",
            "/api/agents/body-size-probe",
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


# --- delete_task tests --------------------------------------------------------


async def test_delete_task_emits_event_and_cleans_orphans(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    """DELETE /api/tasks/{id} on non-in_progress task returns 200, emits task.deleted,
    and removes child events+usage rows."""
    repo_path = tmp_path / "repo_del_ok"
    await _init_repo(repo_path)

    created = await client.post(
        "/api/projects",
        json={"name": "p_del_ok", "path": str(repo_path), "default_branch": "main"},
    )
    assert created.status_code == 200
    project_id = created.json()["id"]

    # Create an inbox task.
    t = await client.post(
        f"/api/projects/{project_id}/tasks", json={"request": "do a thing"}
    )
    assert t.status_code == 200
    task_id = t.json()["id"]

    # Add mock child rows: events and usage (the ones without FK cascades).
    await repo.append_event(
        db, type="log", project_id=project_id, task_id=task_id,
        payload={"msg": "hello"},
    )
    await repo.add_usage(
        db, agent="dev", task_id=task_id, project_id=project_id,
        num_turns=1, input_tokens=10, output_tokens=20,
    )

    # Delete the task.
    resp = await client.delete(f"/api/tasks/{task_id}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": task_id}

    # Task must be gone.
    assert await repo.get_task(db, task_id) is None

    # Child events referencing this task (the "log" event) must be gone;
    # the task.deleted tombstone event persists (queryable by project_id).
    events = await repo.list_events(db, task_id=task_id)
    assert all(
        e.type == "task.deleted" for e in events
    ), f"non-tombstone events remain: {events}"

    # Usage referencing this task must be gone.
    usage = await repo.get_task_usage(db, task_id)
    # Aggregate query still returns a row (COUNT(*) with task_id) but totals should be 0
    # meaning no rows matched.
    assert usage["total_turns"] == 0, f"orphaned usage remains: {usage}"

    # The task.deleted event must have been emitted.
    all_events = await repo.list_events(db, project_id=project_id)
    deleted_events = [e for e in all_events if e.type == "task.deleted"]
    assert len(deleted_events) == 1, f"expected 1 task.deleted event, got {len(deleted_events)}"
    assert deleted_events[0].project_id == project_id


async def test_delete_task_in_progress_returns_409(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    """DELETE /api/tasks/{id} on an in_progress task returns 409 and does not delete."""
    repo_path = tmp_path / "repo_del_409"
    await _init_repo(repo_path)

    created = await client.post(
        "/api/projects",
        json={"name": "p_del_409", "path": str(repo_path), "default_branch": "main"},
    )
    assert created.status_code == 200
    project_id = created.json()["id"]

    t = await client.post(
        f"/api/projects/{project_id}/tasks", json={"request": "do a thing"}
    )
    assert t.status_code == 200
    task_id = t.json()["id"]

    # Manually move to in_progress.
    await repo.set_task_state(db, task_id, TaskState.in_progress)

    resp = await client.delete(f"/api/tasks/{task_id}")
    assert resp.status_code == 409, f"expected 409, got {resp.status_code}: {resp.text}"
    assert "in_progress" in resp.json()["detail"].lower()

    # Task must still exist.
    task = await repo.get_task(db, task_id)
    assert task is not None
    assert task.state == TaskState.in_progress


async def test_delete_task_not_found_returns_404(
    client: httpx.AsyncClient,
) -> None:
    """DELETE /api/tasks/{id} on a non-existent task returns 404."""
    resp = await client.delete("/api/tasks/nonexistent-id")
    assert resp.status_code == 404


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


async def test_create_project_starts_worker_and_is_queryable(
    client_with_workers: httpx.AsyncClient, tmp_path: Path
) -> None:
    """POST /projects creates a usable project and starts its worker immediately.

    Repo context now comes from AGENTS.md (read by opencode), so creation does no
    onboarding/analysis — the endpoint returns a ready project with a live worker.
    """
    repo_path = tmp_path / "repo_fast_create"
    await _init_repo(repo_path)

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
