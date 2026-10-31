"""Integration tests for the read-only task diff (REST + MCP surfaces)."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest_asyncio
from fake_provider import FakeProvider
from httpx import ASGITransport

from conclave.bootstrap import seed_global_defaults
from conclave.db import Database
from conclave.engine.gitio import run_git
from conclave.engine.worktree import WorktreeManager
from conclave.runtime import Daemon
from conclave.web import create_app

_DIFF_KEYS = {"task_id", "source", "base", "head", "stat", "diff", "truncated"}


async def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    (path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (path / "app.txt").write_text("base\n", encoding="utf-8")
    await run_git(path, "add", "-A")
    assert (await run_git(path, "commit", "-m", "init"))[0] == 0
    code, out = await run_git(path, "rev-parse", "HEAD")
    assert code == 0
    return out.strip()


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


async def _make_task(
    client: httpx.AsyncClient, repo_path: Path, request: str = "do a thing"
) -> tuple[str, str]:
    created = await client.post(
        "/api/projects",
        json={"name": "diff-demo", "path": str(repo_path), "default_branch": "main"},
    )
    assert created.status_code == 200, created.text
    project_id = created.json()["id"]
    task = await client.post(
        f"/api/projects/{project_id}/tasks", json={"request": request}
    )
    assert task.status_code == 200, task.text
    return project_id, task.json()["id"]


async def _mcp_call(
    client: httpx.AsyncClient, args: dict[str, Any]
) -> tuple[bool, str]:
    res = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "conclave_get_task_diff", "arguments": args},
        },
    )
    assert res.status_code == 200, res.text
    result = res.json()["result"]
    return bool(result["isError"]), result["content"][0]["text"]


async def _branch_task(
    client: httpx.AsyncClient, tmp_path: Path, *, content: str = "task change\n"
) -> tuple[str, str, str, str]:
    """Create a task with a committed branch and a main commit that came later."""
    repo_path = tmp_path / "repo"
    initial = await _init_repo(repo_path)
    project_id, task_id = await _make_task(client, repo_path)
    wm = WorktreeManager(
        repo_path, tmp_path / "home" / "projects" / project_id / "worktrees"
    )
    branch = f"conclave/{task_id}"
    worktree = await wm.create(task_id, "main", branch)
    (worktree / "task.txt").write_text(content, encoding="utf-8")
    await run_git(worktree, "add", "-A")
    assert (await run_git(worktree, "commit", "-m", "task work"))[0] == 0
    code, out = await run_git(repo_path, "rev-parse", branch)
    assert code == 0
    branch_sha = out.strip()
    await wm.cleanup(task_id)
    (repo_path / "main-only.txt").write_text("later main work\n", encoding="utf-8")
    await run_git(repo_path, "add", "-A")
    assert (await run_git(repo_path, "commit", "-m", "main moves on"))[0] == 0
    return project_id, task_id, initial, branch_sha


async def test_branch_source_rest_and_mcp_identical(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    _, task_id, initial, branch_sha = await _branch_task(client, tmp_path)

    res = await client.get(f"/api/tasks/{task_id}/diff")
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == _DIFF_KEYS
    assert body["task_id"] == task_id
    assert body["source"] == "branch"
    assert body["head"] == branch_sha
    assert body["base"] == initial
    assert re.fullmatch(r"[0-9a-f]{40}", body["base"])
    assert "task.txt" in body["diff"]
    assert "main-only.txt" not in body["diff"]
    assert "task.txt" in body["stat"]
    assert body["truncated"] is False

    is_error, text = await _mcp_call(client, {"task_id": task_id})
    assert is_error is False
    assert json.loads(text) == body


async def test_stat_only_omits_diff_and_matches_full_stat(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    _, task_id, _, _ = await _branch_task(client, tmp_path)
    full = (await client.get(f"/api/tasks/{task_id}/diff")).json()
    stat = (
        await client.get(f"/api/tasks/{task_id}/diff", params={"stat_only": "true"})
    ).json()
    assert stat["diff"] == ""
    assert stat["truncated"] is False
    assert stat["stat"] == full["stat"]

    is_error, text = await _mcp_call(client, {"task_id": task_id, "stat_only": True})
    assert is_error is False
    assert json.loads(text) == stat


async def test_truncation_never_splits_a_multibyte_character(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    _, task_id, _, _ = await _branch_task(client, tmp_path, content="日本語" * 200 + "\n")
    full = (await client.get(f"/api/tasks/{task_id}/diff")).json()
    full_diff = full["diff"]
    idx = full_diff.encode("utf-8").find("日".encode())
    assert idx > 0

    res = await client.get(
        f"/api/tasks/{task_id}/diff", params={"max_bytes": idx + 1}
    )
    assert res.status_code == 200, res.text
    cut = res.json()
    assert cut["truncated"] is True
    assert len(cut["diff"].encode("utf-8")) == idx
    assert len(cut["diff"].encode("utf-8")) <= idx + 1
    assert full_diff.startswith(cut["diff"])
    assert "\ufffd" not in cut["diff"]


async def test_max_bytes_validation(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    _, task_id, _, _ = await _branch_task(client, tmp_path)
    for bad in ("0", "2000001", "abc"):
        res = await client.get(
            f"/api/tasks/{task_id}/diff", params={"max_bytes": bad}
        )
        assert res.status_code == 422, (bad, res.text)

    ok = await client.get(
        f"/api/tasks/{task_id}/diff", params={"max_bytes": 2_000_000}
    )
    assert ok.status_code == 200, ok.text

    is_error, _ = await _mcp_call(client, {"task_id": task_id, "max_bytes": 0})
    assert is_error is True


async def test_unknown_task_is_404_on_rest_and_error_on_mcp(
    client: httpx.AsyncClient,
) -> None:
    res = await client.get("/api/tasks/nope/diff")
    assert res.status_code == 404
    assert res.json()["detail"] == "task not found"

    is_error, _ = await _mcp_call(client, {"task_id": "nope"})
    assert is_error is True


async def test_task_with_no_diff_source_is_404(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    _, task_id = await _make_task(client, repo_path)

    res = await client.get(f"/api/tasks/{task_id}/diff")
    assert res.status_code == 404
    assert res.json()["detail"] == "no diff available for this task"

    is_error, text = await _mcp_call(client, {"task_id": task_id})
    assert is_error is True
    assert "no diff available for this task" in text


async def test_worktree_source_includes_untracked_and_ignores_caches(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project_id, task_id = await _make_task(client, repo_path)
    wm = WorktreeManager(
        repo_path, tmp_path / "home" / "projects" / project_id / "worktrees"
    )
    worktree = await wm.create(task_id, "main", f"conclave/{task_id}")

    (worktree / "app.txt").write_text("modified\n", encoding="utf-8")
    (worktree / "untracked.txt").write_text("brand new\n", encoding="utf-8")
    (worktree / "debug.log").write_text("gitignored\n", encoding="utf-8")
    cache_files = [
        worktree / "__pycache__" / "m.pyc",
        worktree / ".pytest_cache" / "v",
        worktree / "pkg" / ".mypy_cache" / "x",
        worktree / ".ruff_cache" / "y",
    ]
    for path in cache_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("cache\n", encoding="utf-8")

    code, out = await run_git(worktree, "rev-parse", "--git-path", "index")
    assert code == 0
    index_path = Path(out.strip())
    if not index_path.is_absolute():
        index_path = worktree / index_path
    index_before = index_path.read_bytes()
    _, status_before = await run_git(
        worktree, "--no-optional-locks", "status", "--porcelain", "--untracked-files=all"
    )
    code, head_before = await run_git(worktree, "rev-parse", "HEAD")
    assert code == 0
    code, base_out = await run_git(worktree, "merge-base", "main", "HEAD")
    assert code == 0

    res = await client.get(f"/api/tasks/{task_id}/diff")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["source"] == "worktree"
    assert body["head"] is None
    assert body["base"] == base_out.strip()
    assert "app.txt" in body["diff"]
    assert "untracked.txt" in body["diff"]
    assert "debug.log" not in body["diff"]
    assert "app.txt" in body["stat"]
    assert "untracked.txt" in body["stat"]
    for cache_name in ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"):
        assert cache_name not in body["diff"]
        assert cache_name not in body["stat"]

    is_error, text = await _mcp_call(client, {"task_id": task_id})
    assert is_error is False
    assert json.loads(text) == body

    assert index_path.read_bytes() == index_before
    _, status_after = await run_git(
        worktree, "--no-optional-locks", "status", "--porcelain", "--untracked-files=all"
    )
    assert status_after == status_before
    _, head_after = await run_git(worktree, "rev-parse", "HEAD")
    assert head_after == head_before
    for path in cache_files:
        assert path.is_file(), f"cache file removed: {path}"


async def test_tool_schema_listed_by_tools_list(
    client: httpx.AsyncClient,
) -> None:
    res = await client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    )
    tools = {t["name"]: t for t in res.json()["result"]["tools"]}
    schema = tools["conclave_get_task_diff"]["inputSchema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["task_id"]
    props = schema["properties"]
    assert props["task_id"]["type"] == "string"
    assert props["stat_only"]["type"] == "boolean"
    assert props["stat_only"]["default"] is False
    assert props["max_bytes"]["type"] == "integer"
    assert props["max_bytes"]["default"] == 200_000
