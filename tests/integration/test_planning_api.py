"""Integration tests for agent-ception planning session API endpoints.

Uses FakeProvider for deterministic agent responses. Workers are disabled
so the planning orchestrator is the only async background activity.
"""

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
async def client_with_session(
    db: Database, tmp_path: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    """Create a client with a project already attached and return (client, project_id)."""
    await seed_global_defaults(db)
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/api/projects",
            json={"name": "demo", "path": str(repo_path), "default_branch": "main"},
        )
        project_id = resp.json()["id"]
        yield c, project_id


@pytest_asyncio.fixture
async def client_with_nested_session(
    db: Database, tmp_path: Path,
) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    """Client with a FakeProvider that emits nested JSON in planner task_changes."""
    await seed_global_defaults(db)
    repo_path = tmp_path / "repo_nested"
    await _init_repo(repo_path)
    daemon = Daemon(
        db, tmp_path / "home_nested", FakeProvider(use_nested_plan=True),
        workers_enabled=False,
    )
    app = create_app(daemon, manage_lifecycle=False)
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/api/projects",
            json={"name": "nested-demo", "path": str(repo_path), "default_branch": "main"},
        )
        project_id = resp.json()["id"]
        yield c, project_id


async def test_create_and_list_sessions(
    client_with_session: tuple[httpx.AsyncClient, str],
) -> None:
    client, pid = client_with_session

    # Create a session
    create = await client.post(
        f"/api/projects/{pid}/planning/sessions",
        json={"title": "Add OAuth", "prompt": "Implement OAuth2 login flow"},
    )
    assert create.status_code == 200, create.text
    data = create.json()
    assert data["status"] == "active"
    assert data["project_id"] == pid
    session_id = data["id"]

    # List sessions
    sessions = await client.get(f"/api/projects/{pid}/planning/sessions")
    assert sessions.status_code == 200
    assert len(sessions.json()) >= 1
    assert any(s["id"] == session_id for s in sessions.json())


async def test_get_session_and_messages(
    client_with_session: tuple[httpx.AsyncClient, str],
) -> None:
    client, pid = client_with_session

    # Create a session (background discussion starts)
    create = await client.post(
        f"/api/projects/{pid}/planning/sessions",
        json={"title": "Test", "prompt": "Build feature X"},
    )
    session_id = create.json()["id"]

    # Get session details
    get_resp = await client.get(f"/api/planning/sessions/{session_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == session_id

    # Messages should appear (initial planner message at minimum)
    # The background discussion runs asynchronously; poll briefly
    import asyncio
    for _ in range(10):
        msgs = await client.get(f"/api/planning/sessions/{session_id}/messages")
        if msgs.status_code == 200 and len(msgs.json()) > 0:
            break
        await asyncio.sleep(0.5)

    messages = await client.get(f"/api/planning/sessions/{session_id}/messages")
    assert messages.status_code == 200
    msg_list = messages.json()
    assert len(msg_list) > 0, "Expected at least one message from the planner"
    # First message should be from the planner agent
    assert msg_list[0]["agent"] == "planner"


async def test_human_interjection(
    client_with_session: tuple[httpx.AsyncClient, str],
) -> None:
    client, pid = client_with_session

    create = await client.post(
        f"/api/projects/{pid}/planning/sessions",
        json={"title": "Test", "prompt": "Build feature X", "max_rounds": 2},
    )
    session_id = create.json()["id"]

    # Wait for the session to reach stable first (background discussion finishes)
    import asyncio
    for _ in range(15):
        get_resp = await client.get(f"/api/planning/sessions/{session_id}")
        if get_resp.status_code == 200:
            status = get_resp.json()["status"]
            if status in ("stable", "completed"):
                break
        await asyncio.sleep(0.5)

    # Now send a human message (session is stable, no background task racing)
    resp = await client.post(
        f"/api/planning/sessions/{session_id}/messages",
        json={"content": "Please focus on security first."},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["agent"] == "human"
    assert data["role"] == "human"
    assert "security" in data["content"]


async def test_task_nodes_appear(
    client_with_session: tuple[httpx.AsyncClient, str],
) -> None:
    client, pid = client_with_session

    create = await client.post(
        f"/api/projects/{pid}/planning/sessions",
        json={"title": "Test", "prompt": "Build feature X"},
    )
    session_id = create.json()["id"]

    # Task nodes should appear from the planner's initial breakdown
    import asyncio
    for _ in range(10):
        nodes = await client.get(f"/api/planning/sessions/{session_id}/tasks")
        if nodes.status_code == 200 and len(nodes.json()) > 0:
            break
        await asyncio.sleep(0.5)

    nodes = await client.get(f"/api/planning/sessions/{session_id}/tasks")
    assert nodes.status_code == 200
    task_list = nodes.json()
    assert len(task_list) > 0, "Expected task nodes from planner breakdown"
    assert all("title" in n for n in task_list)


async def test_nested_task_changes_parsed(
    client_with_nested_session: tuple[httpx.AsyncClient, str],
) -> None:
    """Nested JSON (e.g. metadata dicts) in task_changes must be parsed in full.

    A non-greedy regex would truncate at the first nested ``}`` and silently
    drop all task_changes.  The :func:`_extract_json_block` helper uses
    ``raw_decode`` which consumes the complete balanced object.
    """
    client, pid = client_with_nested_session

    create = await client.post(
        f"/api/projects/{pid}/planning/sessions",
        json={"title": "Test Nested", "prompt": "Feature with nested metadata"},
    )
    session_id = create.json()["id"]

    # All 3 nested task_changes must be parsed — not truncated at the first nested brace
    import asyncio
    for _ in range(10):
        nodes = await client.get(f"/api/planning/sessions/{session_id}/tasks")
        if nodes.status_code == 200 and len(nodes.json()) == 3:
            break
        await asyncio.sleep(0.5)

    nodes = await client.get(f"/api/planning/sessions/{session_id}/tasks")
    assert nodes.status_code == 200
    task_list = nodes.json()
    assert len(task_list) == 3, (
        f"Expected 3 task nodes from nested-JSON planner response, got {len(task_list)}. "
        "Non-greedy regex would truncate at first nested '}' and drop all changes."
    )
    titles = {n["title"] for n in task_list}
    assert titles == {"Add auth middleware", "Set up database schema", "Write integration tests"}


async def test_approve_creates_real_tasks(
    client_with_session: tuple[httpx.AsyncClient, str],
) -> None:
    client, pid = client_with_session

    # Create a session and wait for it to reach "stable" state
    create = await client.post(
        f"/api/projects/{pid}/planning/sessions",
        json={"title": "Test", "prompt": "Build feature X", "max_rounds": 2},
    )
    session_id = create.json()["id"]

    # Wait for the session to become stable (discussion loop runs in background)
    import asyncio
    status = "active"
    for _ in range(15):
        get_resp = await client.get(f"/api/planning/sessions/{session_id}")
        if get_resp.status_code == 200:
            status = get_resp.json()["status"]
            if status in ("stable", "completed"):
                break
        await asyncio.sleep(0.5)

    # It should have reached stable (all fake agents approve + planner ready)
    if status != "stable":
        # It might already be completed if something raced; that's fine for the test
        assert status in ("stable", "completed"), f"Expected stable/completed, got {status}"

    # Approve the session (even if already stable)
    if status == "stable":
        approve = await client.post(f"/api/planning/sessions/{session_id}/approve")
        assert approve.status_code == 200, approve.text
        result = approve.json()
        assert result["approved"] is True
        assert result["count"] > 0

        # Verify tasks were created in the task list
        tasks = await client.get(f"/api/projects/{pid}/tasks")
        assert tasks.status_code == 200
        task_list = tasks.json()
        created_ids = set(result["task_ids"])
        matching = [t for t in task_list if t["id"] in created_ids]
        assert len(matching) == result["count"]


async def test_cancel_session(
    client_with_session: tuple[httpx.AsyncClient, str],
) -> None:
    client, pid = client_with_session

    create = await client.post(
        f"/api/projects/{pid}/planning/sessions",
        json={"title": "Cancel me", "prompt": "Build feature Y"},
    )
    session_id = create.json()["id"]

    cancel = await client.post(f"/api/planning/sessions/{session_id}/cancel")
    assert cancel.status_code == 200
    assert cancel.json()["cancelled"] is True

    # Verify status
    get_resp = await client.get(f"/api/planning/sessions/{session_id}")
    assert get_resp.json()["status"] == "cancelled"


async def test_404_on_missing_session(
    client_with_session: tuple[httpx.AsyncClient, str],
) -> None:
    client, _pid = client_with_session
    resp = await client.get("/api/planning/sessions/nonexistent")
    assert resp.status_code == 404


async def test_404_missing_session_messages(
    client_with_session: tuple[httpx.AsyncClient, str],
) -> None:
    """GET /planning/sessions/{unknown}/messages must return 404, not 500 or empty list."""
    client, _pid = client_with_session
    resp = await client.get("/api/planning/sessions/nonexistent/messages")
    assert resp.status_code == 404, (
        f"Expected 404 for missing session messages, got {resp.status_code}: {resp.text}"
    )


async def test_404_missing_session_tasks(
    client_with_session: tuple[httpx.AsyncClient, str],
) -> None:
    """GET /planning/sessions/{unknown}/tasks must return 404, not 500 or empty list."""
    client, _pid = client_with_session
    resp = await client.get("/api/planning/sessions/nonexistent/tasks")
    assert resp.status_code == 404, (
        f"Expected 404 for missing session tasks, got {resp.status_code}: {resp.text}"
    )


async def test_404_missing_session_post_message(
    client_with_session: tuple[httpx.AsyncClient, str],
) -> None:
    """POST /planning/sessions/{unknown}/messages must return 404, not 500."""
    client, _pid = client_with_session
    resp = await client.post(
        "/api/planning/sessions/nonexistent/messages",
        json={"content": "hello"},
    )
    assert resp.status_code == 404, (
        f"Expected 404 for missing session post-message, got {resp.status_code}: {resp.text}"
    )


async def test_404_missing_session_approve(
    client_with_session: tuple[httpx.AsyncClient, str],
) -> None:
    """POST /planning/sessions/{unknown}/approve must return 404, not 500."""
    client, _pid = client_with_session
    resp = await client.post("/api/planning/sessions/nonexistent/approve")
    assert resp.status_code == 404, (
        f"Expected 404 for missing session approve, got {resp.status_code}: {resp.text}"
    )


async def test_404_missing_session_cancel(
    client_with_session: tuple[httpx.AsyncClient, str],
) -> None:
    """POST /planning/sessions/{unknown}/cancel must return 404, not 500."""
    client, _pid = client_with_session
    resp = await client.post("/api/planning/sessions/nonexistent/cancel")
    assert resp.status_code == 404, (
        f"Expected 404 for missing session cancel, got {resp.status_code}: {resp.text}"
    )
