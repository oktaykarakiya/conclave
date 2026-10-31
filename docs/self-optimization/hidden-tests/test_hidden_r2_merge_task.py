"""HIDDEN acceptance tests — bake-off round 2 (conclave_merge_task). Never shown to agents."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
from fake_provider import FakeProvider
from httpx import ASGITransport

from conclave.db import Database, TaskState
from conclave.db import repositories as repo
from conclave.engine import run_git
from conclave.runtime import Daemon
from conclave.web import create_app

_SHA = re.compile(r"^[0-9a-f]{40}$")


async def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    await run_git(path, "config", "user.email", "test@example.com")
    await run_git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# test repo\n")
    await run_git(path, "add", "-A")
    await run_git(path, "commit", "-m", "initial commit")


async def _project(db: Database, tmp_path: Path) -> tuple[Path, Any, Daemon]:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main", "auto_merge": False}},
    )
    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    return repo_path, project, daemon


async def _done_task(db: Database, project: Any, daemon: Daemon) -> Any:
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved,
    )
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None and claimed.id == task.id
    assert await daemon.orchestrator.process_task(claimed) is True
    done = await repo.get_task(db, task.id)
    assert done is not None and done.state is TaskState.done
    return done


async def _mcp(client: httpx.AsyncClient, name: str, args: dict[str, Any]) -> tuple[bool, str]:
    r = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": args}},
    )
    assert r.status_code == 200
    result = r.json()["result"]
    return bool(result.get("isError")), "".join(c.get("text", "") for c in result.get("content", []))


def _client(daemon: Daemon) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=ASGITransport(app=create_app(daemon, manage_lifecycle=False)),
        base_url="http://test",
    )


async def _branch_exists(repo_path: Path, branch: str) -> bool:
    _, out = await run_git(repo_path, "branch", "--list", branch)
    return branch in out


async def test_merge_via_rest(db: Database, tmp_path: Path) -> None:
    repo_path, project, daemon = await _project(db, tmp_path)
    try:
        task = await _done_task(db, project, daemon)
        branch = f"conclave/{task.id}"
        assert await _branch_exists(repo_path, branch)
        code, _ = await run_git(repo_path, "show", "main:FEATURE.txt")
        assert code != 0  # not merged yet
        async with _client(daemon) as client:
            r = await client.post(f"/api/tasks/{task.id}/merge")
            assert r.status_code == 200, r.text
            body = r.json()
        assert body["task_id"] == task.id and body["merged"] is True
        assert body["target"] == "main" and body["branch"] == branch
        _, main_sha = await run_git(repo_path, "rev-parse", "main")
        assert _SHA.match(body["target_sha"]) and body["target_sha"] == main_sha.strip()
        code, out = await run_git(repo_path, "show", "main:FEATURE.txt")
        assert code == 0 and "done" in out
        assert not await _branch_exists(repo_path, branch)
        types = [e.type for e in await repo.list_events(db, task_id=task.id)]
        assert types.count("task.merged") == 1
        after = await repo.get_task(db, task.id)
        assert after is not None and after.state is TaskState.done
        assert "merged" in (after.result_summary or "").lower()
    finally:
        await daemon.shutdown()


async def test_merge_via_mcp_and_second_merge_rejected(db: Database, tmp_path: Path) -> None:
    repo_path, project, daemon = await _project(db, tmp_path)
    try:
        task = await _done_task(db, project, daemon)
        async with _client(daemon) as client:
            is_err, text = await _mcp(client, "conclave_merge_task", {"task_id": task.id})
            assert not is_err, text
            body = json.loads(text)
            assert body["merged"] is True and body["target"] == "main"
            r = await client.post(f"/api/tasks/{task.id}/merge")
            assert r.status_code in (404, 409), r.text
            is_err2, _ = await _mcp(client, "conclave_merge_task", {"task_id": task.id})
            assert is_err2
        code, _ = await run_git(repo_path, "show", "main:FEATURE.txt")
        assert code == 0
    finally:
        await daemon.shutdown()


async def test_not_done_task_rejected(db: Database, tmp_path: Path) -> None:
    _repo_path, project, daemon = await _project(db, tmp_path)
    try:
        task = await repo.create_task(
            db, project_id=project.id, request="later", state=TaskState.approved,
        )
        async with _client(daemon) as client:
            r = await client.post(f"/api/tasks/{task.id}/merge")
            assert r.status_code == 409, r.text
            assert "not done" in r.text
    finally:
        await daemon.shutdown()


async def test_unknown_task(db: Database, tmp_path: Path) -> None:
    _repo_path, _project_, daemon = await _project(db, tmp_path)
    try:
        async with _client(daemon) as client:
            r = await client.post("/api/tasks/nope/merge")
            assert r.status_code == 404
            is_err, _ = await _mcp(client, "conclave_merge_task", {"task_id": "nope"})
            assert is_err
    finally:
        await daemon.shutdown()


async def test_conflict_leaves_everything_intact(db: Database, tmp_path: Path) -> None:
    repo_path, project, daemon = await _project(db, tmp_path)
    try:
        task = await _done_task(db, project, daemon)
        branch = f"conclave/{task.id}"
        # A conflicting change lands on main after the task branched off.
        (repo_path / "FEATURE.txt").write_text("someone else\n")
        await run_git(repo_path, "add", "-A")
        await run_git(repo_path, "commit", "-m", "conflicting change")
        _, main_before = await run_git(repo_path, "rev-parse", "main")
        async with _client(daemon) as client:
            r = await client.post(f"/api/tasks/{task.id}/merge")
            assert r.status_code == 409, r.text
            assert "conflict" in r.text.lower()
        _, main_after = await run_git(repo_path, "rev-parse", "main")
        assert main_after == main_before
        assert await _branch_exists(repo_path, branch)
        _, status = await run_git(repo_path, "status", "--porcelain")
        assert status.strip() == ""
        assert not (repo_path / ".git" / "MERGE_HEAD").exists()
        after = await repo.get_task(db, task.id)
        assert after is not None and after.state is TaskState.done
    finally:
        await daemon.shutdown()


async def test_tool_listed_with_schema(db: Database, tmp_path: Path) -> None:
    _repo_path, _project_, daemon = await _project(db, tmp_path)
    try:
        async with _client(daemon) as client:
            r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            tools = {t["name"]: t for t in r.json()["result"]["tools"]}
            assert "conclave_merge_task" in tools
            schema = tools["conclave_merge_task"]["inputSchema"]
            assert "task_id" in schema["properties"]
            assert schema.get("required") == ["task_id"]
    finally:
        await daemon.shutdown()
