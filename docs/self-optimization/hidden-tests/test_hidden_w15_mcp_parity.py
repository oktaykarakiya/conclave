"""Hidden acceptance tests for W15 (MCP parity with REST, compact MCP responses).

Written by the pilot from the spec alone, run against the task branch before merging; not part
of the repository's own suite. Each new tool must return exactly the JSON its REST endpoint
returns for the same state; unknown or malformed ids are tool errors, never 500s; the listing
tools are compact by default and return today's full payload on request.
"""

from __future__ import annotations

import json
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
from conclave.engine import Orchestrator, run_git
from conclave.events import EventBus
from conclave.runtime import Daemon
from conclave.web import create_app

UNKNOWN = "0" * 32


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "conclave.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


async def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    await run_git(path, "config", "user.email", "test@example.com")
    await run_git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# parity\n", encoding="utf-8")
    await run_git(path, "add", "-A")
    await run_git(path, "commit", "-m", "initial")


class Env:
    def __init__(self, client: httpx.AsyncClient, project_id: str, task_id: str) -> None:
        self.client = client
        self.project_id = project_id
        self.task_id = task_id
        self._id = 0

    async def call(self, name: str, args: dict[str, Any]) -> tuple[bool, Any]:
        self._id += 1
        res = await self.client.post("/mcp", json={
            "jsonrpc": "2.0", "id": self._id, "method": "tools/call",
            "params": {"name": name, "arguments": args},
        })
        assert res.status_code == 200, res.text
        body = res.json()
        assert "error" not in body, body  # a bad input is a tool error, not a JSON-RPC failure
        result = body["result"]
        text = result["content"][0]["text"]
        if result.get("isError"):
            return True, text
        return False, json.loads(text)

    async def rest(self, path: str) -> Any:
        res = await self.client.get(path)
        assert res.status_code == 200, (path, res.status_code, res.text)
        return res.json()


@pytest_asyncio.fixture
async def env(db: Database, tmp_path: Path) -> AsyncIterator[Env]:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    await seed_global_defaults(db)
    project = await repo.create_project(
        db, name="parity", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main", "baseline_test_command": "echo ok"}},
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )
    provider = FakeProvider()
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True
    done = await repo.get_task(db, task.id)
    assert done is not None and done.state is TaskState.done

    daemon = Daemon(db, tmp_path / "home", provider, workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield Env(c, project.id, task.id)


# --- 1-7: each new tool mirrors its REST endpoint ---------------------------------------

async def test_each_new_tool_returns_exactly_the_rest_json(env: Env) -> None:
    pairs: list[tuple[str, dict[str, Any], str]] = [
        ("conclave_get_config_schema", {}, "/api/config/schema"),
        ("conclave_list_models", {}, "/api/models"),
        ("conclave_list_models", {"project_id": env.project_id},
         f"/api/models?project_id={env.project_id}"),
        ("conclave_get_task_usage", {"task_id": env.task_id},
         f"/api/tasks/{env.task_id}/usage"),
        ("conclave_get_project_usage", {"project_id": env.project_id},
         f"/api/projects/{env.project_id}/usage"),
        ("conclave_get_hitl_state", {"task_id": env.task_id}, f"/api/tasks/{env.task_id}/hitl"),
        ("conclave_get_quarantine_integrity", {"project_id": env.project_id},
         f"/api/projects/{env.project_id}/quarantine/integrity"),
        ("conclave_list_needs_human", {"project_id": env.project_id},
         f"/api/projects/{env.project_id}/needs-human"),
    ]
    for name, args, path in pairs:
        is_error, data = await env.call(name, args)
        assert not is_error, (name, data)
        assert data == await env.rest(path), name


async def test_usage_tools_carry_the_finished_tasks_usage(env: Env) -> None:
    _, task_usage = await env.call("conclave_get_task_usage", {"task_id": env.task_id})
    assert json.dumps(task_usage) != "{}"
    assert task_usage == await env.rest(f"/api/tasks/{env.task_id}/usage")


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("conclave_get_task_usage", "task_id"),
        ("conclave_get_hitl_state", "task_id"),
        ("conclave_get_project_usage", "project_id"),
        ("conclave_get_quarantine_integrity", "project_id"),
        ("conclave_list_needs_human", "project_id"),
    ],
)
@pytest.mark.parametrize("bad", [UNKNOWN, "../etc", "", "x" * 300])
async def test_unknown_or_malformed_ids_are_tool_errors(
    env: Env, name: str, key: str, bad: str
) -> None:
    is_error, text = await env.call(name, {key: bad})
    assert is_error
    assert "unknown MCP tool" not in text  # the tool exists and refused the input itself
    assert not text.startswith("Internal error")  # a validation error, never an unhandled one


async def test_missing_required_argument_is_a_tool_error(env: Env) -> None:
    is_error, text = await env.call("conclave_get_task_usage", {})
    assert is_error
    assert "unknown MCP tool" not in text and not text.startswith("Internal error")


async def test_tools_list_schemas_mark_required_ids(env: Env) -> None:
    res = await env.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in res.json()["result"]["tools"]}
    expected = {
        "conclave_get_config_schema": [],
        "conclave_list_models": [],
        "conclave_get_task_usage": ["task_id"],
        "conclave_get_project_usage": ["project_id"],
        "conclave_get_hitl_state": ["task_id"],
        "conclave_get_quarantine_integrity": ["project_id"],
        "conclave_list_needs_human": ["project_id"],
    }
    for name, required in expected.items():
        assert name in tools, name
        assert sorted(tools[name]["inputSchema"].get("required", [])) == required, name
        assert tools[name]["description"].strip(), name


# --- 8: compact responses ---------------------------------------------------------------

_PROJECT_SUMMARY = {"id", "name", "path", "default_branch", "mode", "created_at"}


async def test_list_projects_is_compact_by_default(env: Env) -> None:
    _, projects = await env.call("conclave_list_projects", {})
    assert projects and all(set(p) <= _PROJECT_SUMMARY for p in projects)
    assert all({"id", "name", "path", "default_branch"} <= set(p) for p in projects)
    _, full = await env.call("conclave_list_projects", {"include_config": True})
    assert all("config" in p for p in full)
    assert full[0]["config"]["execution"]["baseline_test_command"] == "echo ok"


async def test_get_task_is_compact_by_default(env: Env) -> None:
    _, compact = await env.call("conclave_get_task", {"task_id": env.task_id})
    assert compact["task"]["request"] == "add a feature file"
    assert compact["task"]["plan"] is not None
    assert compact["checkpoints"], "a checkpoint summary is still returned"
    for cp in compact["checkpoints"]:
        assert {"node_id", "attempt", "status", "git_tree_sha"} <= set(cp)
        assert "artifact" not in cp
    assert compact["verdicts"]
    for v in compact["verdicts"]:
        assert {"agent", "verdict", "reason"} <= set(v)
        assert "evidence" not in v

    _, full = await env.call(
        "conclave_get_task",
        {"task_id": env.task_id, "include_checkpoints": True, "include_evidence": True},
    )
    assert all("artifact" in cp for cp in full["checkpoints"])
    assert all("evidence" in v for v in full["verdicts"])
    assert len(json.dumps(compact)) < len(json.dumps(full))


async def test_decide_workflow_proposal_omits_graphs_unless_asked(
    env: Env, db: Database
) -> None:
    graph_a = {"name": "a", "nodes": [], "edges": [], "marker": "GRAPH-A"}
    graph_b = {"name": "b", "nodes": [], "edges": [], "marker": "GRAPH-B"}
    first, _ = await repo.add_workflow_proposal(
        db, project_id=env.project_id, reason={"action": "shrink", "summary": "s"},
        proposed_graph=graph_a, base_graph=graph_b,
    )
    is_error, decided = await env.call("conclave_decide_workflow_proposal", {
        "project_id": env.project_id, "proposal_id": first.id, "decision": "reject",
        "reason": "keep it",
    })
    assert not is_error, decided
    text = json.dumps(decided)
    assert "GRAPH-A" not in text and "GRAPH-B" not in text
    assert first.id in text and "reject" in text

    second, _ = await repo.add_workflow_proposal(
        db, project_id=env.project_id, reason={"action": "grow", "summary": "g"},
        proposed_graph={**graph_a, "marker": "GRAPH-C"}, base_graph=graph_b,
    )
    _, full = await env.call("conclave_decide_workflow_proposal", {
        "project_id": env.project_id, "proposal_id": second.id, "decision": "reject",
        "include_graphs": True,
    })
    assert "GRAPH-C" in json.dumps(full) and "GRAPH-B" in json.dumps(full)
