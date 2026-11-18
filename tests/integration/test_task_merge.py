"""Integration tests for the operator-triggered task merge (REST + MCP surfaces)."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fake_provider import FakeProvider
from httpx import ASGITransport

from conclave.bootstrap import seed_global_defaults
from conclave.db import Database, TaskState
from conclave.db import repositories as repo
from conclave.engine.gitio import run_git
from conclave.engine.orchestrator import MergeResult, Orchestrator
from conclave.engine.task_merge import TaskMergeError, merge_task
from conclave.engine.worktree import WorktreeManager
from conclave.events import EventBus
from conclave.runtime import Daemon
from conclave.web import create_app

_MERGE_KEYS = {"task_id", "merged", "target", "branch", "target_sha"}


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


async def _git(cwd: Path, *args: str) -> str:
    code, out = await run_git(cwd, *args)
    assert code == 0, (args, out)
    return out.strip()


async def _commit_file(cwd: Path, name: str, content: str, message: str) -> None:
    (cwd / name).write_text(content, encoding="utf-8")
    await _git(cwd, "add", "-A")
    await _git(cwd, "commit", "-m", message)


async def _mcp_call(client: httpx.AsyncClient, task_id: str) -> tuple[bool, str]:
    res = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "conclave_merge_task", "arguments": {"task_id": task_id}},
        },
    )
    assert res.status_code == 200, res.text
    result = res.json()["result"]
    return bool(result["isError"]), result["content"][0]["text"]


async def _task(
    client: httpx.AsyncClient,
    db: Database,
    tmp_path: Path,
    *,
    branch_file: tuple[str, str] | None = ("task.txt", "task change\n"),
    done: bool = True,
) -> tuple[Path, str, str, str]:
    """Create a project + task; commit ``branch_file`` on a kept task branch.

    Returns ``(repo_path, project_id, task_id, branch)``.
    """
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    await _git(repo_path, "init", "-b", "main")
    await _commit_file(repo_path, "app.txt", "base\n", "init")
    created = await client.post(
        "/api/projects",
        json={"name": "merge-demo", "path": str(repo_path), "default_branch": "main"},
    )
    assert created.status_code == 200, created.text
    project_id = created.json()["id"]
    task = await client.post(f"/api/projects/{project_id}/tasks", json={"request": "do it"})
    assert task.status_code == 200, task.text
    task_id = task.json()["id"]
    branch = f"conclave/{task_id}"

    if branch_file is not None:
        wm = WorktreeManager(repo_path, tmp_path / "home" / "projects" / project_id / "worktrees")
        worktree = await wm.create(task_id, "main", branch)
        await _commit_file(worktree, *branch_file, "task work")
        await wm.cleanup(task_id)  # branch kept, like a done task with auto_merge off
    if done:
        await repo.finalize_task(
            db, task_id, state=TaskState.done,
            result_summary="completed in 1 attempt(s); merged=False",
        )
    return repo_path, project_id, task_id, branch


async def _ref(repo_path: Path, branch: str) -> str | None:
    code, out = await run_git(
        repo_path, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"
    )
    return out.strip() if code == 0 else None


async def _merged_events(client: httpx.AsyncClient, task_id: str) -> list[dict[str, Any]]:
    res = await client.get(f"/api/tasks/{task_id}/events")
    assert res.status_code == 200, res.text
    return [e for e in res.json() if e["type"] == "task.merged"]


async def test_rest_merge_commit_into_checked_out_target(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    repo_path, _, task_id, branch = await _task(client, db, tmp_path)
    await _commit_file(repo_path, "main-only.txt", "later\n", "main moves on")

    res = await client.post(f"/api/tasks/{task_id}/merge")
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == _MERGE_KEYS
    assert body["task_id"] == task_id
    assert body["merged"] is True
    assert body["target"] == "main"
    assert body["branch"] == branch
    assert re.fullmatch(r"[0-9a-f]{40}", body["target_sha"])
    assert body["target_sha"] == await _git(repo_path, "rev-parse", "main")

    assert await _git(repo_path, "show", "main:task.txt") == "task change"
    assert await _git(repo_path, "show", "main:main-only.txt") == "later"
    assert await _ref(repo_path, branch) is None
    assert [e["payload"] for e in await _merged_events(client, task_id)] == [
        {"target": "main"}
    ]
    task = (await client.get(f"/api/tasks/{task_id}")).json()
    assert task["state"] == "done"
    assert "main" in task["result_summary"]
    assert "operator" in task["result_summary"]


async def test_mcp_fast_forward_returns_rest_shaped_json(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    repo_path, _, task_id, branch = await _task(client, db, tmp_path)
    branch_sha = await _ref(repo_path, branch)
    await _git(repo_path, "checkout", "-b", "side")  # target not checked out: update-ref

    is_error, text = await _mcp_call(client, task_id)
    assert is_error is False, text
    body = json.loads(text)
    assert body == {
        "task_id": task_id,
        "merged": True,
        "target": "main",
        "branch": branch,
        "target_sha": branch_sha,
    }
    assert await _git(repo_path, "rev-parse", "main") == branch_sha
    assert await _ref(repo_path, branch) is None
    assert len(await _merged_events(client, task_id)) == 1


async def test_second_merge_reports_missing_branch(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    _, _, task_id, _ = await _task(client, db, tmp_path)
    assert (await client.post(f"/api/tasks/{task_id}/merge")).status_code == 200

    res = await client.post(f"/api/tasks/{task_id}/merge")
    assert res.status_code == 404
    assert "branch not found" in res.json()["detail"]
    is_error, text = await _mcp_call(client, task_id)
    assert is_error is True
    assert "branch not found" in text
    assert len(await _merged_events(client, task_id)) == 1


async def test_branch_already_contained_in_target_is_409(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    repo_path, _, task_id, branch = await _task(client, db, tmp_path)
    await _git(repo_path, "merge", "--ff-only", branch)
    main_before = await _git(repo_path, "rev-parse", "main")

    res = await client.post(f"/api/tasks/{task_id}/merge")
    assert res.status_code == 409
    assert "already merged" in res.json()["detail"]
    assert await _git(repo_path, "rev-parse", "main") == main_before
    assert await _ref(repo_path, branch) is not None
    assert await _merged_events(client, task_id) == []


async def test_task_not_done_is_409(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    repo_path, _, task_id, branch = await _task(client, db, tmp_path, done=False)
    main_before = await _git(repo_path, "rev-parse", "main")

    res = await client.post(f"/api/tasks/{task_id}/merge")
    assert res.status_code == 409
    assert "not done" in res.json()["detail"]
    is_error, text = await _mcp_call(client, task_id)
    assert is_error is True
    assert "not done" in text
    assert await _git(repo_path, "rev-parse", "main") == main_before
    assert await _ref(repo_path, branch) is not None


async def test_done_task_without_branch_is_404(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    _, _, task_id, _ = await _task(client, db, tmp_path, branch_file=None)

    res = await client.post(f"/api/tasks/{task_id}/merge")
    assert res.status_code == 404
    assert "branch not found" in res.json()["detail"]


async def test_unknown_task_is_404_on_rest_and_error_on_mcp(
    client: httpx.AsyncClient,
) -> None:
    res = await client.post("/api/tasks/nope/merge")
    assert res.status_code == 404
    assert res.json()["detail"] == "task not found"

    is_error, text = await _mcp_call(client, "nope")
    assert is_error is True
    assert "task not found" in text


@pytest.mark.parametrize("checkout", ["main", "side"])
async def test_conflict_leaves_target_branch_and_checkout_untouched(
    client: httpx.AsyncClient, db: Database, tmp_path: Path, checkout: str
) -> None:
    repo_path, _, task_id, branch = await _task(
        client, db, tmp_path, branch_file=("app.txt", "branch edit\n")
    )
    await _commit_file(repo_path, "app.txt", "main edit\n", "conflicting main work")
    if checkout != "main":
        await _git(repo_path, "checkout", "-b", checkout)
    main_before = await _git(repo_path, "rev-parse", "main")
    branch_before = await _ref(repo_path, branch)

    res = await client.post(f"/api/tasks/{task_id}/merge")
    assert res.status_code == 409
    assert "merge conflict" in res.json()["detail"]
    is_error, text = await _mcp_call(client, task_id)
    assert is_error is True
    assert "merge conflict" in text

    assert await _git(repo_path, "rev-parse", "main") == main_before
    assert await _ref(repo_path, branch) == branch_before
    assert await _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD") == checkout
    assert await _git(repo_path, "status", "--porcelain") == ""
    assert not (repo_path / ".git" / "MERGE_HEAD").exists()
    assert len((await _git(repo_path, "worktree", "list")).splitlines()) == 1
    assert list(tmp_path.glob(".conclave-merge-*")) == []
    assert (await client.get(f"/api/tasks/{task_id}")).json()["state"] == "done"
    assert await _merged_events(client, task_id) == []


async def test_transient_merge_error_is_503(
    client: httpx.AsyncClient,
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _error(
        self: Orchestrator, repo_path: Path, target: str, branch: str, task_id: str
    ) -> MergeResult:
        return MergeResult.error

    monkeypatch.setattr(Orchestrator, "_merge", _error)
    repo_path, _, task_id, branch = await _task(client, db, tmp_path)

    res = await client.post(f"/api/tasks/{task_id}/merge")
    assert res.status_code == 503
    assert "retry" in res.json()["detail"]
    assert await _ref(repo_path, branch) is not None
    task = (await client.get(f"/api/tasks/{task_id}")).json()
    assert task["state"] == "done"
    assert task["result_summary"] == "completed in 1 attempt(s); merged=False"
    assert await _merged_events(client, task_id) == []


async def test_missing_target_branch_is_404(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    repo_path, project_id, task_id, branch = await _task(client, db, tmp_path)
    await repo.update_project_config(db, project_id, {"execution": {"target_branch": "nope"}})

    res = await client.post(f"/api/tasks/{task_id}/merge")
    assert res.status_code == 404
    assert "target branch not found" in res.json()["detail"]
    assert await _ref(repo_path, branch) is not None


async def test_tool_schema_listed_by_tools_list(client: httpx.AsyncClient) -> None:
    res = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in res.json()["result"]["tools"]}
    schema = tools["conclave_merge_task"]["inputSchema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["task_id"]
    assert schema["properties"]["task_id"]["type"] == "string"


async def _project_and_task(db: Database, project_id: str, task_id: str) -> tuple[Any, Any]:
    project = await repo.get_project(db, project_id)
    task = await repo.get_task(db, task_id)
    assert project is not None and task is not None
    return project, task


def _assert_no_merge_lock_left(orchestrator: Orchestrator) -> None:
    assert orchestrator._merge_locks == {}
    assert orchestrator._merge_lock_refs == {}
    assert orchestrator._merge_lock_holders == {}


@pytest.mark.parametrize("checkout", ["main", "side"])
async def test_concurrent_merges_of_one_task_merge_it_once(
    client: httpx.AsyncClient, db: Database, tmp_path: Path, checkout: str
) -> None:
    """Both requests pass the old unlocked checks; under the lock the second sees the merge."""
    repo_path, project_id, task_id, branch = await _task(client, db, tmp_path)
    await _commit_file(repo_path, "main-only.txt", "later\n", "main moves on")
    if checkout != "main":
        await _git(repo_path, "checkout", "-b", checkout)  # merges in a temporary worktree
    project, task = await _project_and_task(db, project_id, task_id)
    orchestrator = Orchestrator(db, EventBus(db), FakeProvider(), tmp_path / "home")

    results = await asyncio.gather(
        merge_task(orchestrator, project, task),
        merge_task(orchestrator, project, task),
        return_exceptions=True,
    )

    merged = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, TaskMergeError)]
    assert len(merged) == 1 and len(refused) == 1, results
    assert refused[0].status_code == 409
    assert "already merged" in refused[0].detail
    assert merged[0]["target_sha"] == await _git(repo_path, "rev-parse", "main")
    assert await _git(repo_path, "show", "main:task.txt") == "task change"
    assert await _git(repo_path, "show", "main:main-only.txt") == "later"
    assert await _git(repo_path, "rev-parse", "--abbrev-ref", "HEAD") == checkout
    assert await _ref(repo_path, branch) is None
    assert len(await _merged_events(client, task_id)) == 1
    assert list(tmp_path.glob(".conclave-merge-*")) == []
    assert len((await _git(repo_path, "worktree", "list")).splitlines()) == 1
    _assert_no_merge_lock_left(orchestrator)


async def test_merge_reports_its_own_commit_when_a_worker_merge_follows(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    """A worker merge queued behind the operator's runs only after its target SHA is read."""
    repo_path, project_id, task_id, branch = await _task(client, db, tmp_path)
    await _git(repo_path, "checkout", "-b", "other")
    await _commit_file(repo_path, "other.txt", "other work\n", "other task work")
    await _git(repo_path, "checkout", "main")
    await _commit_file(repo_path, "main-only.txt", "later\n", "main moves on")
    branch_sha = await _ref(repo_path, branch)
    project, task = await _project_and_task(db, project_id, task_id)
    orchestrator = Orchestrator(db, EventBus(db), FakeProvider(), tmp_path / "home")
    real_merge_locked = orchestrator._merge_locked
    worker: list[asyncio.Task[MergeResult]] = []

    async def _merge_locked_then_queue_worker(
        repo: Path, target: str, task_branch: str, merging_task: str
    ) -> MergeResult:
        result = await real_merge_locked(repo, target, task_branch, merging_task)
        if not worker:  # the operator merge: a worker's merge into main queues behind it
            worker.append(
                asyncio.create_task(orchestrator._merge(repo, "main", "other", "worker-task"))
            )
            for _ in range(20):
                await asyncio.sleep(0)
            assert orchestrator._merge_lock_refs["main"] == 2
        return result

    orchestrator._merge_locked = _merge_locked_then_queue_worker  # type: ignore[method-assign]

    body = await merge_task(orchestrator, project, task)
    assert await worker[0] is MergeResult.success

    # The operator's merge commit, not the worker's later one.
    assert await _git(repo_path, "rev-parse", f"{body['target_sha']}^2") == branch_sha
    assert await _git(repo_path, "rev-parse", "main^1") == body["target_sha"]
    assert await _git(repo_path, "show", "main:other.txt") == "other work"
    _assert_no_merge_lock_left(orchestrator)


async def test_merge_under_the_held_merge_lock_runs_without_waiting_on_itself(
    db: Database, tmp_path: Path
) -> None:
    orchestrator = Orchestrator(db, EventBus(db), FakeProvider(), tmp_path / "home")
    merged: list[str] = []

    async def _fake_locked(
        repo: Path, target: str, task_branch: str, merging_task: str
    ) -> MergeResult:
        merged.append(task_branch)
        return MergeResult.success

    orchestrator._merge_locked = _fake_locked  # type: ignore[method-assign]

    async with asyncio.timeout(5), orchestrator.merge_lock("main"):
        assert orchestrator._merge_lock_holders["main"] is asyncio.current_task()
        # Another task's merge into the same target still waits for the lock.
        other = asyncio.create_task(orchestrator._merge(tmp_path, "main", "other", "task-2"))
        for _ in range(20):
            await asyncio.sleep(0)
        assert not other.done()
        assert await orchestrator._merge(tmp_path, "main", "feature", "task-1") is (
            MergeResult.success
        )
        assert merged == ["feature"]
    assert await other is MergeResult.success
    assert merged == ["feature", "other"]
    _assert_no_merge_lock_left(orchestrator)
