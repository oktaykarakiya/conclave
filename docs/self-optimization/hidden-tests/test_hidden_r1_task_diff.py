"""HIDDEN acceptance tests — bake-off round 1 (conclave_get_task_diff). Never shown to agents.

Copied into tests/integration/ of a clone of each candidate branch and run there.
"""

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
from conclave.engine.worktree import WorktreeManager
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


async def _setup(db: Database, tmp_path: Path, *, run_task: bool) -> tuple[Path, Any, Any, Daemon]:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main", "auto_merge": False}},
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved,
    )
    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    if run_task:
        claimed = await repo.claim_next_approved(db, project.id)
        assert claimed is not None
        assert await daemon.orchestrator.process_task(claimed) is True
    return repo_path, project, task, daemon


async def _mcp(client: httpx.AsyncClient, name: str, args: dict[str, Any]) -> tuple[bool, str]:
    r = await client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": name, "arguments": args}},
    )
    assert r.status_code == 200
    result = r.json()["result"]
    text = "".join(c.get("text", "") for c in result.get("content", []))
    return bool(result.get("isError")), text


def _client(daemon: Daemon) -> httpx.AsyncClient:
    app = create_app(daemon, manage_lifecycle=False)
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_branch_diff_rest_and_mcp_identical(db: Database, tmp_path: Path) -> None:
    repo_path, _project, task, daemon = await _setup(db, tmp_path, run_task=True)
    branch = f"conclave/{task.id}"
    _, base = await run_git(repo_path, "merge-base", "main", branch)
    _, head = await run_git(repo_path, "rev-parse", branch)
    try:
        async with _client(daemon) as client:
            r = await client.get(f"/api/tasks/{task.id}/diff")
            assert r.status_code == 200, r.text
            body = r.json()
            assert {"task_id", "source", "base", "head", "stat", "diff", "truncated"} <= set(body)
            assert body["task_id"] == task.id
            assert body["source"] == "branch"
            assert _SHA.match(body["base"]) and body["base"] == base.strip()
            assert body["head"] == head.strip()
            assert "FEATURE.txt" in body["stat"]
            assert "+done" in body["diff"]
            assert body["truncated"] is False

            is_err, text = await _mcp(client, "conclave_get_task_diff", {"task_id": task.id})
            assert not is_err, text
            assert json.loads(text) == body
    finally:
        await daemon.shutdown()


async def test_stat_only(db: Database, tmp_path: Path) -> None:
    _repo, _p, task, daemon = await _setup(db, tmp_path, run_task=True)
    try:
        async with _client(daemon) as client:
            r = await client.get(f"/api/tasks/{task.id}/diff", params={"stat_only": "true"})
            assert r.status_code == 200, r.text
            assert r.json()["diff"] == ""
            assert "FEATURE.txt" in r.json()["stat"]
            is_err, text = await _mcp(
                client, "conclave_get_task_diff", {"task_id": task.id, "stat_only": True},
            )
            assert not is_err and json.loads(text)["diff"] == ""
    finally:
        await daemon.shutdown()


async def test_truncation_respects_max_bytes(db: Database, tmp_path: Path) -> None:
    _repo, _p, task, daemon = await _setup(db, tmp_path, run_task=True)
    try:
        async with _client(daemon) as client:
            r = await client.get(f"/api/tasks/{task.id}/diff", params={"max_bytes": 40})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["truncated"] is True
            assert 0 < len(body["diff"].encode("utf-8")) <= 40
    finally:
        await daemon.shutdown()


async def test_max_bytes_validation(db: Database, tmp_path: Path) -> None:
    _repo, _p, task, daemon = await _setup(db, tmp_path, run_task=True)
    try:
        async with _client(daemon) as client:
            for bad in (0, -5, 2_000_001):
                r = await client.get(f"/api/tasks/{task.id}/diff", params={"max_bytes": bad})
                assert r.status_code == 422, (bad, r.status_code)
    finally:
        await daemon.shutdown()


async def test_unknown_task(db: Database, tmp_path: Path) -> None:
    _repo, _p, _task, daemon = await _setup(db, tmp_path, run_task=False)
    try:
        async with _client(daemon) as client:
            r = await client.get("/api/tasks/does-not-exist/diff")
            assert r.status_code == 404
            is_err, _ = await _mcp(client, "conclave_get_task_diff", {"task_id": "does-not-exist"})
            assert is_err
    finally:
        await daemon.shutdown()


async def test_no_diff_available(db: Database, tmp_path: Path) -> None:
    _repo, _p, task, daemon = await _setup(db, tmp_path, run_task=False)
    try:
        async with _client(daemon) as client:
            r = await client.get(f"/api/tasks/{task.id}/diff")
            assert r.status_code == 404
            assert "no diff available" in r.text
            is_err, _ = await _mcp(client, "conclave_get_task_diff", {"task_id": task.id})
            assert is_err
    finally:
        await daemon.shutdown()


async def test_worktree_source_untracked_included_caches_excluded_read_only(
    db: Database, tmp_path: Path,
) -> None:
    repo_path, project, task, daemon = await _setup(db, tmp_path, run_task=False)
    wm = WorktreeManager(repo_path, tmp_path / "home" / "projects" / project.id / "worktrees")
    wt = await wm.create(task.id, "main", f"conclave/{task.id}")
    (wt / "NEW.txt").write_text("brand new\n")
    (wt / "README.md").write_text("# test repo\nchanged\n")
    cache = wt / "pkg" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "mod.cpython-312.pyc").write_bytes(b"\x00\x01")
    _, status_before = await run_git(wt, "status", "--porcelain", "--untracked-files=all")
    try:
        async with _client(daemon) as client:
            r = await client.get(f"/api/tasks/{task.id}/diff")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["source"] == "worktree"
            assert body["head"] is None
            assert "NEW.txt" in body["stat"] and "README.md" in body["stat"]
            assert "+brand new" in body["diff"]
            assert "__pycache__" not in body["stat"] and "__pycache__" not in body["diff"]
        _, status_after = await run_git(wt, "status", "--porcelain", "--untracked-files=all")
        assert status_after == status_before  # nothing staged, nothing deleted
        assert (cache / "mod.cpython-312.pyc").exists()
    finally:
        await daemon.shutdown()


async def test_tool_listed_with_schema(db: Database, tmp_path: Path) -> None:
    _repo, _p, _task, daemon = await _setup(db, tmp_path, run_task=False)
    try:
        async with _client(daemon) as client:
            r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            tools = {t["name"]: t for t in r.json()["result"]["tools"]}
            assert "conclave_get_task_diff" in tools
            schema = tools["conclave_get_task_diff"]["inputSchema"]
            assert {"task_id", "stat_only", "max_bytes"} <= set(schema["properties"])
            assert schema.get("required") == ["task_id"]
    finally:
        await daemon.shutdown()
