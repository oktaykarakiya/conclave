"""MCP ↔ REST parity: each tool that mirrors a REST endpoint returns that endpoint's JSON.

Every tool is called over ``POST /mcp`` (JSON-RPC ``tools/call``) and its parsed text content is
compared with the REST answer for the same state; bad ids and arguments come back as ``isError``
tool results, never an internal error. The compact defaults of the project, task and workflow
decision tools are checked against the full payloads their ``include_*`` flags return.
Deterministic: the FakeProvider, no LLM.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fake_provider import FakeProvider
from httpx import ASGITransport

from conclave.db import BugStatus, Database, TaskState
from conclave.db import repositories as repo
from conclave.engine import run_git
from conclave.runtime import Daemon
from conclave.web import create_app

PROJECT_FIELDS = ("id", "name", "path", "default_branch", "mode", "created_at")
CHECKPOINT_FIELDS = ("node_id", "attempt", "status", "git_tree_sha")


@pytest_asyncio.fixture
async def daemon(db: Database, tmp_path: Path) -> AsyncIterator[Daemon]:
    instance = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    try:
        yield instance
    finally:
        await instance.shutdown()


@pytest_asyncio.fixture
async def client(daemon: Daemon) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(daemon, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _call(client: httpx.AsyncClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """POST one ``tools/call`` to ``/mcp`` and return its JSON-RPC ``result``."""
    resp = await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert resp.status_code == 200, resp.text
    result: dict[str, Any] = resp.json()["result"]
    return result


async def _tool(client: httpx.AsyncClient, name: str, arguments: dict[str, Any]) -> Any:
    """A successful tool call's payload, parsed from its text content."""
    result = await _call(client, name, arguments)
    text = result["content"][0]["text"]
    assert result["isError"] is False, text
    return json.loads(text)


async def _tool_error(client: httpx.AsyncClient, name: str, arguments: dict[str, Any]) -> str:
    """A failed tool call's message."""
    result = await _call(client, name, arguments)
    text: str = result["content"][0]["text"]
    assert result["isError"] is True, text
    return text


async def _rest(client: httpx.AsyncClient, path: str, **params: Any) -> Any:
    resp = await client.get(path, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _graphs(payload: Any) -> int:
    """How many workflow graphs (objects with both nodes and edges) *payload* holds."""
    if isinstance(payload, dict):
        own = 1 if {"nodes", "edges"} <= payload.keys() else 0
        return own + sum(_graphs(value) for value in payload.values())
    if isinstance(payload, list):
        return sum(_graphs(value) for value in payload)
    return 0


def _engines(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """opencode present (the test interpreter), claude missing, on this "host"."""
    monkeypatch.setenv("CONCLAVE_OPENCODE_BIN", sys.executable)
    monkeypatch.setenv("CONCLAVE_CLAUDE_BIN", str(tmp_path / "no-claude"))


async def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    await run_git(path, "config", "user.email", "test@example.com")
    await run_git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# parity\n", encoding="utf-8")
    await run_git(path, "add", "-A")
    await run_git(path, "commit", "-m", "initial commit")


@dataclass
class Seeded:
    project_id: str
    task_id: str
    needs_human: list[str]  # the declined_needs_human candidates, oldest first


async def _seed(daemon: Daemon, client: httpx.AsyncClient, db: Database, tmp_path: Path) -> Seeded:
    """A project with a finished fake task, usage rows, a verdict with evidence, an armed HITL
    checkpoint, an active and an expired quarantine entry, and needs-human candidates."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    resp = await client.post("/api/projects", json={"name": "parity", "path": str(repo_path)})
    assert resp.status_code == 200, resp.text
    pid: str = resp.json()["id"]
    resp = await client.patch(
        f"/api/projects/{pid}/config",
        json={
            "config": {
                "execution": {"baseline_test_command": "echo ok"},
                "agent_defaults": {"model": "p/proj"},  # the project's catalog differs
            }
        },
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/projects/{pid}/tasks",
        json={"request": "add a feature file", "title": "feature", "auto_approve": True},
    )
    assert resp.status_code == 200, resp.text
    tid: str = resp.json()["id"]
    claimed = await repo.claim_next_approved(db, pid)
    assert claimed is not None and claimed.id == tid
    assert await daemon.orchestrator.process_task(claimed) is True
    finished = await repo.get_task(db, tid)
    assert finished is not None and finished.state == "done"

    for agent, cost, turns, tokens_in, tokens_out in (
        ("developer", 0.25, 3, 1200, 340),
        ("reviewer", 0.5, 2, 800, 60),
    ):
        await repo.add_usage(
            db,
            agent=agent,
            project_id=pid,
            task_id=tid,
            model_reported="fake/model",
            cost_usd=cost,
            num_turns=turns,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cache_read_tokens=50,
            cache_creation_tokens=7,
        )
    await repo.add_verdict(
        db,
        task_id=tid,
        attempt=1,
        agent="security",
        verdict="pass",
        reason="no secrets in the change",
        source="json",
        grounded_count=1,
        evidence=[{"file": "README.md", "line": 1, "quote": "# parity"}],
    )
    armed = await _tool(
        client, "conclave_set_hitl_checkpoint", {"task_id": tid, "target_id": "gate"}
    )
    assert armed["armed"] == ["gate"]

    resp = await client.post(
        f"/api/projects/{pid}/quarantine",
        json={"pattern": "tests/test_flaky.py", "reason": "flaky", "until": "2999-12-31"},
    )
    assert resp.status_code == 200, resp.text
    await repo.add_quarantine(
        db, project_id=pid, pattern="tests/test_old.py", reason="was flaky", until="2000-01-01"
    )

    declined: list[str] = []
    for idx in range(3):
        cand = await repo.create_bug_candidate(
            db, project_id=pid, fingerprint=f"fp-{idx}", claim=f"bug {idx}", file="app.py"
        )
        if idx < 2:  # the last one stays discovered, so it is not the human's
            await repo.transition_bug_status(
                db, cand.id, BugStatus.declined_needs_human, decline_reason="needs a person"
            )
            declined.append(cand.id)
    return Seeded(project_id=pid, task_id=tid, needs_human=declined)


# --- the new tools return their REST endpoint's JSON ----------------------------


async def test_each_new_tool_returns_its_rest_endpoints_json(
    daemon: Daemon,
    client: httpx.AsyncClient,
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engines(monkeypatch, tmp_path)
    seeded = await _seed(daemon, client, db, tmp_path)
    pid, tid = seeded.project_id, seeded.task_id

    pairs: list[tuple[str, dict[str, Any], str, dict[str, Any]]] = [
        ("conclave_get_config_schema", {}, "/api/config/schema", {}),
        ("conclave_list_models", {}, "/api/models", {}),
        ("conclave_list_models", {"project_id": "global"}, "/api/models", {}),
        ("conclave_list_models", {"project_id": pid}, "/api/models", {"project_id": pid}),
        ("conclave_get_task_usage", {"task_id": tid}, f"/api/tasks/{tid}/usage", {}),
        ("conclave_get_project_usage", {"project_id": pid}, f"/api/projects/{pid}/usage", {}),
        ("conclave_get_hitl_state", {"task_id": tid}, f"/api/tasks/{tid}/hitl", {}),
        (
            "conclave_get_quarantine_integrity",
            {"project_id": pid},
            f"/api/projects/{pid}/quarantine/integrity",
            {},
        ),
        ("conclave_list_needs_human", {"project_id": pid}, f"/api/projects/{pid}/needs-human", {}),
        (
            "conclave_list_needs_human",
            {"project_id": pid, "limit": 1},
            f"/api/projects/{pid}/needs-human",
            {"limit": 1},
        ),
    ]
    answers: dict[str, Any] = {}
    for name, args, path, params in pairs:
        mcp_answer = await _tool(client, name, args)
        assert mcp_answer == await _rest(client, path, **params), (name, args)
        answers[f"{name}:{json.dumps(args, sort_keys=True)}"] = mcp_answer

    # The state behind each answer is real, so the equalities above compare something.
    schema = answers["conclave_get_config_schema:{}"]
    assert "execution" in schema["properties"] and "$defs" in schema
    glob = answers["conclave_list_models:{}"]
    assert glob == answers['conclave_list_models:{"project_id": "global"}']
    proj = answers[f'conclave_list_models:{{"project_id": "{pid}"}}']
    assert proj["default_model"] == "p/proj" != glob["default_model"]
    assert "p/proj" in proj["models"] and "p/proj" not in glob["models"]
    assert proj["engines"] == {
        "default": "opencode",
        "claude_code_prefix": "claude-code/",
        "available": {"opencode": True, "claude": False},
    }
    assert not any(model.startswith("claude-code/") for model in glob["models"])

    usage = answers[f'conclave_get_task_usage:{{"task_id": "{tid}"}}']
    assert usage["task_id"] == tid
    assert (usage["input_tokens"], usage["output_tokens"]) == (2000, 400)
    assert (usage["cache_read_tokens"], usage["cache_creation_tokens"]) == (100, 14)
    assert usage["agent_count"] >= 2
    project_usage = answers[f'conclave_get_project_usage:{{"project_id": "{pid}"}}']
    assert project_usage["calls"] >= 2 and project_usage["total_cost_usd"] >= 0.75

    hitl = answers[f'conclave_get_hitl_state:{{"task_id": "{tid}"}}']
    assert hitl == {"task_id": tid, "armed": ["gate"], "waiting": []}

    integrity = answers[f'conclave_get_quarantine_integrity:{{"project_id": "{pid}"}}']
    assert (integrity["total"], integrity["active"], integrity["expired"]) == (2, 1, 1)
    assert integrity["expired_patterns"] == ["tests/test_old.py"]
    assert integrity["healthy"] is False

    queue = answers[f'conclave_list_needs_human:{{"project_id": "{pid}"}}']
    assert [c["id"] for c in queue] == seeded.needs_human[::-1]  # newest first
    assert {c["status"] for c in queue} == {"declined_needs_human"}
    one = answers[f'conclave_list_needs_human:{{"limit": 1, "project_id": "{pid}"}}']
    assert [c["id"] for c in one] == [seeded.needs_human[-1]]


async def test_list_needs_human_clamps_its_limit_like_the_other_list_tools(
    daemon: Daemon, client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    project = await repo.create_project(db, name="p", path=str(tmp_path), default_branch="main")
    ids: list[str] = []
    for idx in range(3):
        cand = await repo.create_bug_candidate(
            db, project_id=project.id, fingerprint=f"fp-{idx}", claim=f"bug {idx}"
        )
        await repo.transition_bug_status(db, cand.id, BugStatus.declined_needs_human)
        ids.append(cand.id)
    newest_first = ids[::-1]

    for limit, expected in ((0, newest_first[:1]), (2, newest_first[:2]), (10_000, newest_first)):
        rows = await _tool(
            client, "conclave_list_needs_human", {"project_id": project.id, "limit": limit}
        )
        assert [c["id"] for c in rows] == expected, limit
    rest = await _rest(client, f"/api/projects/{project.id}/needs-human", limit=2, offset=1)
    assert [c["id"] for c in rest] == newest_first[1:]


# --- bad input ------------------------------------------------------------------


async def test_bad_ids_and_arguments_are_tool_errors_and_rest_4xx(
    daemon: Daemon, client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    project = await repo.create_project(db, name="p", path=str(tmp_path), default_branch="main")
    task = await repo.create_task(db, project_id=project.id, request="r")
    pid = project.id

    cases: list[tuple[str, dict[str, Any]]] = [
        ("conclave_list_models", {"project_id": "nope"}),
        ("conclave_list_models", {"project_id": 42}),
        ("conclave_get_task_usage", {}),
        ("conclave_get_task_usage", {"task_id": "nope"}),
        ("conclave_get_task_usage", {"task_id": [task.id]}),
        ("conclave_get_project_usage", {}),
        ("conclave_get_project_usage", {"project_id": "nope"}),
        ("conclave_get_project_usage", {"project_id": {"id": pid}}),
        ("conclave_get_hitl_state", {}),
        ("conclave_get_hitl_state", {"task_id": "nope"}),
        ("conclave_get_hitl_state", {"task_id": 7}),
        ("conclave_get_quarantine_integrity", {}),
        ("conclave_get_quarantine_integrity", {"project_id": "nope"}),
        ("conclave_get_quarantine_integrity", {"project_id": True}),
        ("conclave_list_needs_human", {}),
        ("conclave_list_needs_human", {"project_id": "nope"}),
        ("conclave_list_needs_human", {"project_id": [pid]}),
        ("conclave_list_needs_human", {"project_id": pid, "limit": "many"}),
        ("conclave_list_needs_human", {"project_id": pid, "limit": [1]}),
        ("conclave_get_task", {"task_id": "nope", "include_checkpoints": True}),
        ("conclave_list_projects", {"limit": "all", "include_config": True}),
    ]
    for name, args in cases:
        text = await _tool_error(client, name, args)
        assert text.startswith("Error:"), (name, args, text)  # a handled error, not a crash

    for path in (
        "/api/projects/nope/usage",
        "/api/projects/nope/quarantine/integrity",
        "/api/projects/nope/needs-human",
        "/api/tasks/nope/usage",
        "/api/tasks/nope/hitl",
    ):
        assert (await client.get(path)).status_code == 404, path
    assert (await client.get("/api/models", params={"project_id": "nope"})).status_code == 404
    for bad in (0, 501):
        resp = await client.get(f"/api/projects/{pid}/needs-human", params={"limit": bad})
        assert resp.status_code == 422, bad


# --- tools/list -----------------------------------------------------------------


async def test_tools_list_describes_the_new_tools_and_the_compact_flags(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 200, resp.text
    tools = {t["name"]: t for t in resp.json()["result"]["tools"]}

    expected: dict[str, tuple[list[str], dict[str, str], str]] = {
        "conclave_get_config_schema": ([], {}, "GET /api/config/schema"),
        "conclave_list_models": ([], {"project_id": "string"}, "GET /api/models"),
        "conclave_get_task_usage": (
            ["task_id"], {"task_id": "string"}, "GET /api/tasks/{task_id}/usage"
        ),
        "conclave_get_project_usage": (
            ["project_id"], {"project_id": "string"}, "GET /api/projects/{project_id}/usage"
        ),
        "conclave_get_hitl_state": (
            ["task_id"], {"task_id": "string"}, "GET /api/tasks/{task_id}/hitl"
        ),
        "conclave_get_quarantine_integrity": (
            ["project_id"],
            {"project_id": "string"},
            "GET /api/projects/{project_id}/quarantine/integrity",
        ),
        "conclave_list_needs_human": (
            ["project_id"],
            {"project_id": "string", "limit": "integer"},
            "GET /api/projects/{project_id}/needs-human",
        ),
    }
    for name, (required, types, endpoint) in expected.items():
        tool = tools[name]
        schema = tool["inputSchema"]
        assert schema["type"] == "object", name
        assert schema["required"] == required, name
        assert {key: prop["type"] for key, prop in schema["properties"].items()} == types, name
        assert endpoint in tool["description"], name
    limit = tools["conclave_list_needs_human"]["inputSchema"]["properties"]["limit"]
    assert limit["default"] == 50 and "(1-500, default 50)" in limit["description"]
    assert "newest first, at most limit" in tools["conclave_list_needs_human"]["description"]

    flags = {
        "conclave_list_projects": ["include_config"],
        "conclave_get_project_status": ["include_config"],
        "conclave_set_project_mode": ["include_config"],
        "conclave_get_task": ["include_checkpoints", "include_evidence"],
        "conclave_decide_workflow_proposal": ["include_graphs"],
        "conclave_revert_workflow": ["include_graphs"],
        "conclave_evolve_workflow": ["include_graphs"],
    }
    for name, names in flags.items():
        props = tools[name]["inputSchema"]["properties"]
        for flag in names:
            assert props[flag]["type"] == "boolean", (name, flag)
            assert props[flag]["default"] is False, (name, flag)
            assert f"{flag}=true" in tools[name]["description"], (name, flag)


# --- compact defaults and the flags that return the full payloads ---------------


async def test_get_task_summarizes_checkpoints_and_leaves_verdict_evidence_out(
    daemon: Daemon,
    client: httpx.AsyncClient,
    db: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engines(monkeypatch, tmp_path)
    tid = (await _seed(daemon, client, db, tmp_path)).task_id
    rest_task = await _rest(client, f"/api/tasks/{tid}")
    rest_checkpoints = await _rest(client, f"/api/tasks/{tid}/checkpoints")
    rest_verdicts = await _rest(client, f"/api/tasks/{tid}/verdicts", limit=100)
    rest_usage = await _rest(client, f"/api/tasks/{tid}/usage")
    rest_hitl = await _rest(client, f"/api/tasks/{tid}/hitl")
    assert rest_checkpoints and any(cp["artifact"] for cp in rest_checkpoints)
    assert any(v["evidence"] for v in rest_verdicts)

    default = await _tool(client, "conclave_get_task", {"task_id": tid})
    assert set(default) == {"task", "checkpoints", "verdicts", "usage", "hitl"}
    assert default["task"] == rest_task
    assert default["task"]["request"] == "add a feature file" and default["task"]["plan"]
    assert default["checkpoints"] == [
        {field: cp[field] for field in CHECKPOINT_FIELDS} for cp in rest_checkpoints
    ]
    assert default["verdicts"] == [
        {key: value for key, value in v.items() if key != "evidence"} for v in rest_verdicts
    ]
    assert default["usage"] == rest_usage
    assert default["hitl"] == {k: v for k, v in rest_hitl.items() if k != "task_id"}

    full = await _tool(
        client,
        "conclave_get_task",
        {"task_id": tid, "include_checkpoints": True, "include_evidence": True},
    )
    assert full == {
        "task": rest_task,
        "checkpoints": rest_checkpoints,
        "verdicts": rest_verdicts,
        "usage": rest_usage,
        "hitl": default["hitl"],
    }
    assert len(json.dumps(default)) < len(json.dumps(full))

    only_checkpoints = await _tool(
        client, "conclave_get_task", {"task_id": tid, "include_checkpoints": True}
    )
    assert only_checkpoints == {**default, "checkpoints": rest_checkpoints}
    only_evidence = await _tool(
        client, "conclave_get_task", {"task_id": tid, "include_evidence": True}
    )
    assert only_evidence == {**default, "verdicts": rest_verdicts}


async def test_project_tools_leave_the_stored_config_out_by_default(
    daemon: Daemon, client: httpx.AsyncClient, tmp_path: Path
) -> None:
    repo_path = tmp_path / "repo"
    (repo_path / ".git").mkdir(parents=True)
    resp = await client.post(
        "/api/projects",
        json={"name": "graph", "path": str(repo_path), "workflow_template": "fast_track"},
    )
    assert resp.status_code == 200, resp.text
    pid = resp.json()["id"]
    rest_rows = await _rest(client, "/api/projects")
    assert _graphs(rest_rows) == 1  # the stored config holds the whole fast_track graph
    summary = {field: rest_rows[0][field] for field in PROJECT_FIELDS}

    assert await _tool(client, "conclave_list_projects", {}) == [summary]
    assert await _tool(client, "conclave_list_projects", {"include_config": True}) == rest_rows

    status = await _tool(client, "conclave_get_project_status", {"project_id": pid})
    assert set(status) == {"project", "paused", "mode", "usage"}
    assert status["project"] == summary
    full_status = await _tool(
        client, "conclave_get_project_status", {"project_id": pid, "include_config": True}
    )
    assert full_status == {**status, "project": await _rest(client, f"/api/projects/{pid}")}

    moded = await _tool(
        client, "conclave_set_project_mode", {"project_id": pid, "mode": "autonomous_bug_fixer"}
    )
    after = await _rest(client, f"/api/projects/{pid}")
    assert after["mode"] == "autonomous_bug_fixer"
    assert moded == {field: after[field] for field in PROJECT_FIELDS}
    full_mode = await _tool(
        client,
        "conclave_set_project_mode",
        {"project_id": pid, "mode": "task_queue", "include_config": True},
    )
    assert full_mode == await _rest(client, f"/api/projects/{pid}")
    assert full_mode["mode"] == "task_queue" and _graphs(full_mode) == 1


def _strip(row: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: value for key, value in row.items() if key not in keys}


async def test_workflow_decisions_leave_graphs_out_by_default(
    daemon: Daemon, client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    async def project(name: str) -> str:
        created = await repo.create_project(
            db, name=name, path=str(tmp_path / name), default_branch="main"
        )
        return created.id

    async def latest_version(pid: str) -> dict[str, Any]:
        versions = await _rest(client, f"/api/projects/{pid}/workflow/versions")
        newest: dict[str, Any] = versions[0]
        return newest

    async def proposal_row(pid: str, proposal_id: str) -> dict[str, Any]:
        rows = await _rest(client, f"/api/projects/{pid}/workflow/proposals")
        (row,) = [r for r in rows if r["id"] == proposal_id]
        found: dict[str, Any] = row
        return found

    async def evolve(pid: str, **flags: Any) -> dict[str, Any]:
        """A manual evolve after a failed task: a grow proposal in the default suggest mode."""
        outcome: dict[str, Any] = await _tool(
            client, "conclave_evolve_workflow", {"project_id": pid, "task_failed": True, **flags}
        )
        return outcome

    # Evolve (suggest mode): the outcome without graphs, then with them, equal to REST's.
    first = await project("first")
    evolved = await evolve(first)
    assert evolved["mode"] == "suggest" and evolved["proposal"]["state"] == "pending"
    assert _graphs(evolved) == 0 and '"nodes"' not in json.dumps(evolved)
    evolved_full = await evolve(first, include_graphs=True)
    rest_evolved = await client.post(
        f"/api/projects/{first}/workflow/evolve", json={"task_failed": True}
    )
    assert rest_evolved.status_code == 200, rest_evolved.text
    assert evolved_full == rest_evolved.json()  # the same pending proposal, not stored again
    assert _graphs(evolved_full) == 3  # workflow, proposed_graph, base_graph
    assert evolved == {
        **_strip(evolved_full, "workflow"),
        "proposal": _strip(evolved_full["proposal"], "proposed_graph", "base_graph"),
    }

    # Approve, one line by default.
    proposal_id = evolved["proposal"]["id"]
    approved = await _tool(
        client,
        "conclave_decide_workflow_proposal",
        {"project_id": first, "proposal_id": proposal_id, "decision": "approve"},
    )
    row = await proposal_row(first, proposal_id)
    version = await latest_version(first)
    assert approved == {
        "decision": "approve",
        "proposal_id": proposal_id,
        "state": "approved",
        "summary": row["reason"]["summary"],
        "changes": row["reason"]["changes"],
        "version_id": version["id"],
    }
    assert approved["summary"] and approved["changes"] and row["version_id"] == version["id"]

    # Approve with include_graphs: the full result, equal to the REST rows it changed.
    second = await project("second")
    pending = await evolve(second)
    approved_full = await _tool(
        client,
        "conclave_decide_workflow_proposal",
        {
            "project_id": second,
            "proposal_id": pending["proposal"]["id"],
            "decision": "approve",
            "include_graphs": True,
        },
    )
    assert set(approved_full) == {"decision", "proposal", "version", "workflow", "is_custom"}
    assert approved_full["workflow"] == (await _rest(client, f"/api/projects/{second}/workflow"))[
        "workflow"
    ]
    assert approved_full["version"] == await latest_version(second)
    assert approved_full["proposal"] == await proposal_row(second, pending["proposal"]["id"])
    assert approved_full["is_custom"] is True

    # Reject: one line by default, the decided proposal with its graphs on request.
    third = await project("third")
    pending = await evolve(third)
    rejected = await _tool(
        client,
        "conclave_decide_workflow_proposal",
        {"project_id": third, "proposal_id": pending["proposal"]["id"], "decision": "reject"},
    )
    row = await proposal_row(third, pending["proposal"]["id"])
    assert rejected == {
        "decision": "reject",
        "proposal_id": row["id"],
        "state": "rejected",
        "summary": row["reason"]["summary"],
        "changes": row["reason"]["changes"],
        "version_id": None,
    }
    again = await evolve(third)
    # The rejection stands on this workflow; the compact answer still says which one stopped it.
    assert (again["proposal"], again["suppressed_by"]) == (None, row["id"])
    assert "workflow" not in again
    resp = await client.put(f"/api/projects/{third}/workflow", json={"template_id": "fast_track"})
    assert resp.status_code == 200, resp.text
    again = await evolve(third)
    assert again["proposal"]["id"] != row["id"]  # the workflow changed, so a new one
    assert again["suppressed_by"] is None
    rejected_full = await _tool(
        client,
        "conclave_decide_workflow_proposal",
        {
            "project_id": third,
            "proposal_id": again["proposal"]["id"],
            "decision": "reject",
            "include_graphs": True,
        },
    )
    assert rejected_full == {
        "decision": "reject",
        "proposal": await proposal_row(third, again["proposal"]["id"]),
        "version": None,
    }
    assert _graphs(rejected_full) == 2  # proposed_graph, base_graph

    # Revert: without graphs by default, today's payload with include_graphs.
    fourth = await project("fourth")
    for template in ("fast_track", "deep_audit"):
        resp = await client.put(
            f"/api/projects/{fourth}/workflow", json={"template_id": template}
        )
        assert resp.status_code == 200, resp.text
    deep, fast = await _rest(client, f"/api/projects/{fourth}/workflow/versions")
    reverted = await _tool(
        client, "conclave_revert_workflow", {"project_id": fourth, "version_id": fast["id"]}
    )
    version = await latest_version(fourth)
    assert version["source"] == "revert"
    assert reverted == {
        "changed": True,
        "reverted_to": fast["id"],
        "version": _strip(version, "graph_after", "graph_before"),
        "is_custom": True,
    }
    reverted_full = await _tool(
        client,
        "conclave_revert_workflow",
        {"project_id": fourth, "version_id": deep["id"], "include_graphs": True},
    )
    assert reverted_full == {
        "changed": True,
        "reverted_to": deep["id"],
        "version": await latest_version(fourth),
        "workflow": (await _rest(client, f"/api/projects/{fourth}/workflow"))["workflow"],
        "is_custom": True,
    }
    assert _graphs(reverted_full) == 3  # workflow, graph_after, graph_before


async def test_upsert_agent_for_an_unknown_project_is_a_tool_error(
    client: httpx.AsyncClient, db: Database
) -> None:
    """A persona scoped to an unknown project is a handled tool error, like the REST 404, not
    the FOREIGN KEY failure surfacing as an internal error."""
    text = await _tool_error(
        client,
        "conclave_upsert_agent",
        {"name": "scoped", "persona_md": "# Scoped", "project_id": "nope"},
    )
    assert text.startswith("Error:") and "project not found" in text, text
    assert await db.fetchval("SELECT COUNT(*) FROM agents WHERE name = 'scoped'") == 0


async def _call_raw(
    client: httpx.AsyncClient, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Like :func:`_call`, with the body written by ``json.dumps``, which spells Infinity
    (httpx's ``json=`` refuses it; the server's JSON parser accepts it)."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    resp = await client.post(
        "/mcp", content=json.dumps(body), headers={"Content-Type": "application/json"}
    )
    assert resp.status_code == 200, resp.text
    result: dict[str, Any] = resp.json()["result"]
    return result


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("conclave_get_task_events", {"after_id": 10**30}),
        ("conclave_list_projects", {"limit": float("inf")}),
        ("conclave_get_task_diff", {"max_bytes": float("-inf")}),
        ("conclave_launch_swarm", {"prompt": "split it", "concurrency": float("inf")}),
        (
            "conclave_workflow_upsert_edge",
            {"source": "developer", "target": "tester", "max_iterations": float("inf")},
        ),
        ("conclave_create_planning_session", {"prompt": "plan it", "max_rounds": 10**30}),
        ("conclave_create_planning_session", {"prompt": "plan it", "max_rounds": 0}),
    ],
)
async def test_out_of_range_integer_arguments_are_tool_errors_not_internal_errors(
    client: httpx.AsyncClient, db: Database, tmp_path: Path, name: str, arguments: dict[str, Any]
) -> None:
    """An integer argument SQLite or int() cannot hold (beyond 64 bits, or Infinity, which
    the JSON parser accepts) is a handled tool error; before the fix it surfaced as
    "Internal error: OverflowError". A planning session needs 1 to 50 rounds, like over REST."""
    project = await repo.create_project(db, name="p", path=str(tmp_path), default_branch="main")
    task = await repo.create_task(db, project_id=project.id, request="r")
    result = await _call_raw(
        client, name, {"project_id": project.id, "task_id": task.id, **arguments}
    )
    text = result["content"][0]["text"]
    assert result["isError"] is True, text
    assert text.startswith("Error:"), text
    assert await repo.list_planning_sessions(db, project.id) == []


async def test_cancel_tool_never_overwrites_a_task_claimed_after_the_read(
    client: httpx.AsyncClient, db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Like REST: a task a worker claims between the tool's read and its write stays
    in_progress, and the tool does not report it cancelled."""
    from conclave.mcp import tools as tools_module

    project = await repo.create_project(db, name="p", path=str(tmp_path), default_branch="main")
    task = await repo.create_task(
        db, project_id=project.id, request="r", state=TaskState.approved
    )
    real_require_task = tools_module._require_task
    claimed: list[Any] = []

    async def read_then_claim(daemon: Daemon, task_id: str) -> Any:
        found = await real_require_task(daemon, task_id)
        if not claimed:  # the worker claims the task right after the tool read it
            claimed.append(await repo.claim_next_approved(db, project.id))
        return found

    monkeypatch.setattr(tools_module, "_require_task", read_then_claim)
    answer = await _tool(client, "conclave_cancel_task", {"task_id": task.id})

    assert claimed and claimed[0] is not None and claimed[0].id == task.id
    assert answer == {"cancelled": False, "task_id": task.id}
    after = await repo.get_task(db, task.id)
    assert after is not None and after.state == TaskState.in_progress


async def test_create_task_with_an_unknown_parent_is_a_tool_error(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    """A child of a task that does not exist is refused: claim_next_approved only claims a
    child whose parent is done, so it would have waited forever without an error."""
    project = await repo.create_project(db, name="p", path=str(tmp_path), default_branch="main")
    text = await _tool_error(
        client,
        "conclave_create_task",
        {"project_id": project.id, "request": "child work", "parent_task_id": "nope"},
    )
    assert text.startswith("Error:") and "nope" in text, text
    assert await repo.list_tasks(db, project.id) == []


async def test_list_bug_candidates_needs_human_only_is_bounded(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    """needs_human_only returns at most 100 candidates, newest first, like the unfiltered
    list (W15: every list result is bounded)."""
    project = await repo.create_project(db, name="p", path=str(tmp_path), default_branch="main")
    ids: list[str] = []
    for idx in range(101):
        cand = await repo.create_bug_candidate(
            db, project_id=project.id, fingerprint=f"fp-{idx}", claim=f"bug {idx}"
        )
        await repo.transition_bug_status(db, cand.id, BugStatus.declined_needs_human)
        await db.execute(  # distinct times, so "newest first" has one answer
            "UPDATE bug_candidates SET discovered_at = ? WHERE id = ?",
            (f"2026-01-01T00:{idx // 60:02d}:{idx % 60:02d}+00:00", cand.id),
        )
        ids.append(cand.id)

    rows = await _tool(
        client,
        "conclave_list_bug_candidates",
        {"project_id": project.id, "needs_human_only": True},
    )
    assert [c["id"] for c in rows] == ids[::-1][:100]
