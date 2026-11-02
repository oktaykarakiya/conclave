"""Workflow versions and evolver proposals over REST, MCP and the orchestrator.

Every change to a project's workflow records a version (source, reason, the graph after and
before it) that can be reverted and is never deleted. The evolver only proposes changes unless
``experimental.workflow_evolution`` is ``auto``, and a proposal applies only while the workflow
is still the one it was computed from. Deterministic: the FakeProvider, no LLM.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fake_provider import FakeProvider
from httpx import ASGITransport

from conclave.config import (
    GLOBAL_DEFAULTS_PRESET,
    save_project_config,
    workflow_templates,
)
from conclave.db import Database, Project, StoreItemKind, TaskState, WorkflowVersionSource
from conclave.db import repositories as repo
from conclave.engine import Orchestrator, run_git
from conclave.events import EventBus
from conclave.mcp.server import handle_mcp_message
from conclave.mcp.tools import MCP_TOOLS
from conclave.runtime import Daemon
from conclave.web import create_app

REASON = "pin the reviewers we trust"


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


async def _mcp(daemon: Daemon, name: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
    """Call an MCP tool; ``(is_error, text)`` on a tool error, else ``(False, payload)``."""
    response = await handle_mcp_message(
        daemon,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response is not None
    result = response["result"]
    text = result["content"][0]["text"]
    return (True, text) if result["isError"] else (False, json.loads(text))


async def _project(db: Database, tmp_path: Path, name: str = "p") -> Project:
    """A project on the derived pipeline (no workflow of its own)."""
    return await repo.create_project(
        db, name=name, path=str(tmp_path / name), default_branch="main"
    )


def _template(template_id: str) -> dict[str, Any]:
    return workflow_templates()[template_id].model_dump(mode="json")


async def _live(client: httpx.AsyncClient, pid: str) -> dict[str, Any]:
    resp = await client.get(f"/api/projects/{pid}/workflow")
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


async def _versions(client: httpx.AsyncClient, pid: str, **params: Any) -> list[dict[str, Any]]:
    resp = await client.get(f"/api/projects/{pid}/workflow/versions", params=params)
    assert resp.status_code == 200, resp.text
    body: list[dict[str, Any]] = resp.json()
    return body


async def _proposals(
    client: httpx.AsyncClient, pid: str, **params: Any
) -> list[dict[str, Any]]:
    resp = await client.get(f"/api/projects/{pid}/workflow/proposals", params=params)
    assert resp.status_code == 200, resp.text
    body: list[dict[str, Any]] = resp.json()
    return body


async def _propose(client: httpx.AsyncClient, pid: str) -> dict[str, Any]:
    """A pending grow proposal from a manual evolve in the default suggest mode."""
    resp = await client.post(f"/api/projects/{pid}/workflow/evolve", json={"task_failed": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "suggest" and body["evolved"] is False
    proposal: dict[str, Any] = body["proposal"]
    assert proposal["state"] == "pending"
    return proposal


async def _decide(
    surface: str,
    client: httpx.AsyncClient,
    daemon: Daemon,
    pid: str,
    proposal_id: str,
    decision: str,
    reason: str | None = None,
) -> tuple[bool, Any]:
    """Approve or reject over *surface*; ``(refused, payload)`` where refused is 409/isError."""
    if surface == "rest":
        resp = await client.post(
            f"/api/projects/{pid}/workflow/proposals/{proposal_id}/{decision}",
            json={"reason": reason} if reason else None,
        )
        assert resp.status_code in (200, 409), resp.text
        return resp.status_code == 409, resp.json()
    args = {"project_id": pid, "proposal_id": proposal_id, "decision": decision}
    if reason:
        args["reason"] = reason
    return await _mcp(daemon, "conclave_decide_workflow_proposal", args)


# --- every mutation path records a version -------------------------------------

Mutation = Callable[[httpx.AsyncClient, Daemon, str], Awaitable[None]]


def _rest(method: str, path: str, body: dict[str, Any]) -> Mutation:
    async def call(client: httpx.AsyncClient, _daemon: Daemon, pid: str) -> None:
        resp = await client.request(method, f"/api/projects/{pid}{path}", json=body)
        assert resp.status_code == 200, resp.text

    return call


def _tool(name: str, args: dict[str, Any]) -> Mutation:
    async def call(_client: httpx.AsyncClient, daemon: Daemon, pid: str) -> None:
        is_error, payload = await _mcp(daemon, name, {"project_id": pid, **args})
        assert not is_error, payload

    return call


MUTATIONS = [
    pytest.param(
        _rest("PUT", "/workflow", {"workflow": _template("fast_track"), "reason": REASON}),
        "operator",
        id="rest-put-graph",
    ),
    pytest.param(
        _rest("PUT", "/workflow", {"template_id": "tdd_loop", "reason": REASON}),
        "template",
        id="rest-put-template",
    ),
    pytest.param(
        _rest(
            "PATCH", "/config", {"config": {"workflow": _template("deep_audit")}, "reason": REASON}
        ),
        "operator",
        id="rest-patch-config",
    ),
    pytest.param(
        _rest("POST", "/workflow/evolve", {"task_failed": True, "apply": True, "reason": REASON}),
        "operator",
        id="rest-evolve-apply",
    ),
    pytest.param(
        _tool("conclave_set_workflow", {"workflow": _template("fast_track"), "reason": REASON}),
        "operator",
        id="mcp-set-workflow",
    ),
    pytest.param(
        _tool("conclave_apply_workflow_template", {"template_id": "deep_audit", "reason": REASON}),
        "template",
        id="mcp-apply-template",
    ),
    pytest.param(
        _tool(
            "conclave_workflow_upsert_node",
            {"id": "custom-sec", "kind": "reviewer", "agent": "security", "reason": REASON},
        ),
        "operator",
        id="mcp-upsert-node",
    ),
    pytest.param(
        _tool("conclave_workflow_remove_node", {"node_id": "legal", "reason": REASON}),
        "operator",
        id="mcp-remove-node",
    ),
    pytest.param(
        _tool(
            "conclave_workflow_upsert_edge",
            {"source": "planner", "target": "tester", "condition": "always", "reason": REASON},
        ),
        "operator",
        id="mcp-upsert-edge",
    ),
    pytest.param(
        _tool(
            "conclave_workflow_remove_edge",
            {"source": "developer", "target": "legal", "reason": REASON},
        ),
        "operator",
        id="mcp-remove-edge",
    ),
    pytest.param(
        _tool("conclave_evolve_workflow", {"task_failed": True, "apply": True, "reason": REASON}),
        "operator",
        id="mcp-evolve-apply",
    ),
    pytest.param(
        _tool(
            "conclave_patch_config",
            {"config": {"workflow": _template("fast_track")}, "reason": REASON},
        ),
        "operator",
        id="mcp-patch-config",
    ),
]


@pytest.mark.parametrize(("mutate", "source"), MUTATIONS)
async def test_every_workflow_mutation_path_records_one_version(
    db: Database,
    tmp_path: Path,
    daemon: Daemon,
    client: httpx.AsyncClient,
    mutate: Mutation,
    source: str,
) -> None:
    project = await _project(db, tmp_path)
    assert await _versions(client, project.id) == []

    await mutate(client, daemon, project.id)

    versions = await _versions(client, project.id)
    assert len(versions) == 1
    version = versions[0]
    assert (version["source"], version["reason"]) == (source, REASON)
    assert version["project_id"] == project.id and version["created_at"]
    assert version["graph_before"] is None  # it started on the derived pipeline
    live = await _live(client, project.id)
    assert live["is_custom"] is True
    assert version["graph_after"] == live["workflow"]


async def test_writes_that_leave_the_workflow_alone_record_no_version(
    db: Database, tmp_path: Path, daemon: Daemon, client: httpx.AsyncClient
) -> None:
    project = await _project(db, tmp_path)
    for _ in range(2):  # the same graph again is no change
        resp = await client.put(
            f"/api/projects/{project.id}/workflow", json={"workflow": _template("fast_track")}
        )
        assert resp.status_code == 200, resp.text
    resp = await client.patch(
        f"/api/projects/{project.id}/config",
        json={"config": {"execution": {"review_rounds_max": 7}}},
    )
    assert resp.status_code == 200, resp.text
    for name, args in (
        ("conclave_patch_config", {"review_rounds_max": 9}),
        ("conclave_set_workflow", {"workflow": _template("fast_track")}),
    ):
        is_error, payload = await _mcp(daemon, name, {"project_id": project.id, **args})
        assert not is_error, payload

    versions = await _versions(client, project.id)
    assert [(v["source"], v["reason"]) for v in versions] == [("operator", "workflow saved")]


async def test_a_store_install_and_a_global_apply_to_all_are_versioned_too(
    db: Database, tmp_path: Path, daemon: Daemon, client: httpx.AsyncClient
) -> None:
    project = await _project(db, tmp_path)
    resp = await client.post(
        "/api/store",
        json={"kind": "workflow", "name": "team-flow", "payload": _template("tdd_loop")},
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/api/projects/{project.id}/store/install", json={"item_id": resp.json()["id"]}
    )
    assert resp.status_code == 200, resp.text
    resp = await client.patch(
        "/api/config/global",
        json={
            "config": {"workflow": _template("deep_audit")},
            "apply_to_all_projects": True,
            "reason": "one audit pipeline everywhere",
        },
    )
    assert resp.status_code == 200, resp.text

    versions = await _versions(client, project.id)
    assert [(v["source"], v["reason"]) for v in versions] == [
        ("operator", "one audit pipeline everywhere"),
        ("template", "Store workflow installed: team-flow"),
    ]
    assert versions[0]["graph_before"] == versions[1]["graph_after"]
    assert versions[0]["graph_after"] == (await _live(client, project.id))["workflow"]


async def test_a_write_that_repairs_an_invalid_stored_workflow_still_works(
    db: Database, tmp_path: Path
) -> None:
    """The workflow before a write is read from the workflow layers alone, as stored."""
    broken = {"nodes": [{"id": "x"}]}  # a node without a kind does not validate
    await db.execute(
        "INSERT INTO projects(id, name, path, default_branch, mode, config_json, created_at) "
        "VALUES ('legacy', 'legacy', '/tmp/legacy', 'main', 'task_queue', ?, '2026-01-01')",
        (json.dumps({"workflow": broken}),),
    )
    project = await repo.get_project(db, "legacy")
    assert project is not None

    resolved = await save_project_config(db, project, unset=["workflow"])

    assert resolved.workflow is None
    versions = await repo.list_workflow_versions(db, "legacy")
    assert [(v.graph_before, v.graph_after) for v in versions] == [(broken, None)]
    assert versions[0].source is WorkflowVersionSource.operator and versions[0].reason


# --- proposals: approve, reject, conflicts --------------------------------------


@pytest.mark.parametrize("surface", ["rest", "mcp"])
async def test_approving_a_proposal_applies_it_as_a_proposal_version(
    surface: str, db: Database, tmp_path: Path, daemon: Daemon, client: httpx.AsyncClient
) -> None:
    project = await _project(db, tmp_path)
    before = await _live(client, project.id)
    proposal = await _propose(client, project.id)
    assert await _live(client, project.id) == before  # suggesting never touches the workflow
    assert proposal["base_graph"] == before["workflow"]
    assert proposal["reason"]["action"] == "grow" and proposal["reason"]["changes"]

    refused, result = await _decide(
        surface, client, daemon, project.id, proposal["id"], "approve", REASON
    )

    assert not refused, result
    live = await _live(client, project.id)
    assert live["is_custom"] is True and live["workflow"] == proposal["proposed_graph"]
    assert result["workflow"] == live["workflow"]
    versions = await _versions(client, project.id)
    assert [(v["source"], v["reason"]) for v in versions] == [("proposal", REASON)]
    assert versions[0]["graph_after"] == proposal["proposed_graph"]
    assert versions[0]["graph_before"] is None
    (approved,) = await _proposals(client, project.id, state="approved")
    assert approved["id"] == proposal["id"]
    assert approved["version_id"] == versions[0]["id"] == result["version"]["id"]
    assert approved["decision_reason"] == REASON and approved["decided_at"]

    for decision in ("approve", "reject"):  # a decided proposal stays decided
        refused, message = await _decide(
            surface, client, daemon, project.id, proposal["id"], decision
        )
        assert refused and "already approved" in json.dumps(message)
    assert len(await _versions(client, project.id)) == 1


@pytest.mark.parametrize("surface", ["rest", "mcp"])
async def test_rejecting_a_proposal_records_the_reason_and_leaves_the_workflow(
    surface: str, db: Database, tmp_path: Path, daemon: Daemon, client: httpx.AsyncClient
) -> None:
    project = await _project(db, tmp_path)
    before = await _live(client, project.id)
    proposal = await _propose(client, project.id)

    refused, result = await _decide(
        surface, client, daemon, project.id, proposal["id"], "reject", "too many reviewers"
    )

    assert not refused, result
    assert await _live(client, project.id) == before
    assert await _versions(client, project.id) == []
    assert await _proposals(client, project.id, state="pending") == []
    (rejected,) = await _proposals(client, project.id, state="rejected")
    assert rejected["id"] == proposal["id"]
    assert rejected["decision_reason"] == "too many reviewers"
    assert rejected["version_id"] is None
    refused, _ = await _decide(surface, client, daemon, project.id, proposal["id"], "approve")
    assert refused


@pytest.mark.parametrize("surface", ["rest", "mcp"])
async def test_approving_after_the_workflow_changed_is_refused(
    surface: str, db: Database, tmp_path: Path, daemon: Daemon, client: httpx.AsyncClient
) -> None:
    project = await _project(db, tmp_path)
    proposal = await _propose(client, project.id)
    resp = await client.put(
        f"/api/projects/{project.id}/workflow", json={"template_id": "fast_track"}
    )
    assert resp.status_code == 200, resp.text
    live = await _live(client, project.id)

    refused, message = await _decide(surface, client, daemon, project.id, proposal["id"], "approve")

    assert refused and "evolution again" in json.dumps(message)
    assert await _live(client, project.id) == live
    assert [p["id"] for p in await _proposals(client, project.id, state="pending")] == [
        proposal["id"]
    ]
    assert [v["source"] for v in await _versions(client, project.id)] == ["template"]


# --- revert -------------------------------------------------------------------


@pytest.mark.parametrize("surface", ["rest", "mcp"])
async def test_revert_restores_an_earlier_graph_including_the_derived_pipeline(
    surface: str, db: Database, tmp_path: Path, daemon: Daemon, client: httpx.AsyncClient
) -> None:
    project = await _project(db, tmp_path)
    pid = project.id
    for body in (
        {"workflow": _template("fast_track")},
        {"template_id": "standard"},  # back to the derived pipeline: a version holding null
        {"workflow": _template("deep_audit")},
    ):
        resp = await client.put(f"/api/projects/{pid}/workflow", json=body)
        assert resp.status_code == 200, resp.text
    deep, derived, fast = await _versions(client, pid)  # newest first
    assert derived["graph_after"] is None and fast["graph_after"] == _template("fast_track")

    async def revert(version_id: str, reason: str | None = None) -> dict[str, Any]:
        if surface == "rest":
            resp = await client.post(
                f"/api/projects/{pid}/workflow/versions/{version_id}/revert",
                json={"reason": reason} if reason else None,
            )
            assert resp.status_code == 200, resp.text
            body: dict[str, Any] = resp.json()
            return body
        args = {"project_id": pid, "version_id": version_id}
        if reason:
            args["reason"] = reason
        is_error, payload = await _mcp(daemon, "conclave_revert_workflow", args)
        assert not is_error, payload
        result: dict[str, Any] = payload
        return result

    result = await revert(derived["id"], "back to the policy")
    assert result["changed"] is True and result["is_custom"] is False
    assert (await _live(client, pid))["is_custom"] is False
    versions = await _versions(client, pid)
    assert len(versions) == 4
    assert (versions[0]["source"], versions[0]["reason"]) == ("revert", "back to the policy")
    assert versions[0]["graph_after"] is None
    assert versions[0]["graph_before"] == deep["graph_after"]

    result = await revert(fast["id"])
    assert result["changed"] is True
    assert (await _live(client, pid))["workflow"] == _template("fast_track")
    versions = await _versions(client, pid)
    assert len(versions) == 5
    assert versions[0]["reason"] == f"revert to version {fast['id']}"

    result = await revert(fast["id"])  # it is the current graph already
    assert result["changed"] is False and result["version"] is None
    assert len(await _versions(client, pid)) == 5  # history only grows


# --- listing, bounds and bad input ----------------------------------------------


async def test_lists_are_newest_first_bounded_and_filterable(
    db: Database, tmp_path: Path, daemon: Daemon, client: httpx.AsyncClient
) -> None:
    project = await _project(db, tmp_path)
    pid = project.id
    for template in ("fast_track", "tdd_loop", "deep_audit"):
        resp = await client.put(f"/api/projects/{pid}/workflow", json={"template_id": template})
        assert resp.status_code == 200, resp.text
    versions = await _versions(client, pid)
    assert [v["reason"] for v in versions] == [
        "template applied: deep_audit",
        "template applied: tdd_loop",
        "template applied: fast_track",
    ]
    assert [v["id"] for v in await _versions(client, pid, limit=2)] == [
        v["id"] for v in versions[:2]
    ]
    for bad in (0, 501):
        for path in ("versions", "proposals"):
            resp = await client.get(f"/api/projects/{pid}/workflow/{path}", params={"limit": bad})
            assert resp.status_code == 422, (path, bad)
    is_error, payload = await _mcp(
        daemon, "conclave_list_workflow_versions", {"project_id": pid, "limit": 0}
    )
    assert not is_error and [v["id"] for v in payload] == [versions[0]["id"]]
    is_error, payload = await _mcp(
        daemon, "conclave_list_workflow_versions", {"project_id": pid, "limit": 10_000}
    )
    assert not is_error and [v["id"] for v in payload] == [v["id"] for v in versions]

    other = await _project(db, tmp_path, "other")
    first = await _propose(client, other.id)
    refused, _ = await _decide("rest", client, daemon, other.id, first["id"], "reject")
    assert not refused
    second = await _propose(client, other.id)  # the same change again, now that none is pending
    assert second["id"] != first["id"]
    assert [p["id"] for p in await _proposals(client, other.id)] == [second["id"], first["id"]]
    assert [p["id"] for p in await _proposals(client, other.id, state="pending")] == [
        second["id"]
    ]
    resp = await client.get(f"/api/projects/{other.id}/workflow/proposals", params={"state": "x"})
    assert resp.status_code == 422
    is_error, payload = await _mcp(
        daemon, "conclave_list_workflow_proposals", {"project_id": other.id, "state": "rejected"}
    )
    assert not is_error and [p["id"] for p in payload] == [first["id"]]
    is_error, message = await _mcp(
        daemon, "conclave_list_workflow_proposals", {"project_id": other.id, "state": "x"}
    )
    assert is_error and "invalid state" in message


async def test_bad_ids_and_bad_input_are_4xx_or_tool_errors(
    db: Database, tmp_path: Path, daemon: Daemon, client: httpx.AsyncClient
) -> None:
    project = await _project(db, tmp_path, "mine")
    other = await _project(db, tmp_path, "other")
    pid = project.id
    resp = await client.put(
        f"/api/projects/{other.id}/workflow", json={"template_id": "fast_track"}
    )
    assert resp.status_code == 200, resp.text
    (foreign_version,) = await _versions(client, other.id)
    foreign_proposal = await _propose(client, other.id)
    foreign_task = await repo.create_task(db, project_id=other.id, request="theirs")
    too_long = "x" * 2001

    for method, url in (
        ("GET", "/api/projects/nope/workflow/versions"),
        ("GET", "/api/projects/nope/workflow/proposals"),
        ("POST", f"/api/projects/nope/workflow/versions/{foreign_version['id']}/revert"),
        ("POST", f"/api/projects/nope/workflow/proposals/{foreign_proposal['id']}/approve"),
        ("POST", f"/api/projects/nope/workflow/proposals/{foreign_proposal['id']}/reject"),
        ("POST", "/api/projects/nope/workflow/evolve"),
    ):
        assert (await client.request(method, url)).status_code == 404, url
    for version_id in ("nope", foreign_version["id"]):
        resp = await client.post(f"/api/projects/{pid}/workflow/versions/{version_id}/revert")
        assert resp.status_code == 404
    for proposal_id in ("nope", foreign_proposal["id"]):
        for decision in ("approve", "reject"):
            resp = await client.post(
                f"/api/projects/{pid}/workflow/proposals/{proposal_id}/{decision}"
            )
            assert resp.status_code == 404
    for task_id in ("nope", foreign_task.id):
        resp = await client.post(
            f"/api/projects/{pid}/workflow/evolve", json={"task_id": task_id}
        )
        assert resp.status_code == 404
    for method, url, body in (
        ("PUT", f"/api/projects/{pid}/workflow", {"template_id": "fast_track", "reason": too_long}),
        ("PATCH", f"/api/projects/{pid}/config", {"config": {}, "reason": too_long}),
        ("POST", f"/api/projects/{pid}/workflow/evolve", {"apply": True, "reason": too_long}),
        (
            "POST",
            f"/api/projects/{other.id}/workflow/proposals/{foreign_proposal['id']}/reject",
            {"reason": too_long},
        ),
        (
            "POST",
            f"/api/projects/{other.id}/workflow/versions/{foreign_version['id']}/revert",
            {"reason": too_long},
        ),
    ):
        resp = await client.request(method, url, json=body)
        assert resp.status_code == 422, url

    for name, args in (
        ("conclave_list_workflow_versions", {"project_id": "nope"}),
        ("conclave_list_workflow_proposals", {"project_id": "nope"}),
        ("conclave_revert_workflow", {"project_id": pid}),
        ("conclave_revert_workflow", {"project_id": pid, "version_id": "nope"}),
        ("conclave_revert_workflow", {"project_id": pid, "version_id": foreign_version["id"]}),
        (
            "conclave_decide_workflow_proposal",
            {"project_id": pid, "proposal_id": "nope", "decision": "approve"},
        ),
        (
            "conclave_decide_workflow_proposal",
            {"project_id": pid, "proposal_id": foreign_proposal["id"], "decision": "reject"},
        ),
        (
            "conclave_decide_workflow_proposal",
            {"project_id": other.id, "proposal_id": foreign_proposal["id"], "decision": "maybe"},
        ),
        ("conclave_evolve_workflow", {"project_id": pid, "task_id": "nope"}),
        ("conclave_evolve_workflow", {"project_id": pid, "task_id": foreign_task.id}),
        (
            "conclave_set_workflow",
            {"project_id": pid, "workflow": _template("fast_track"), "reason": too_long},
        ),
        (
            "conclave_decide_workflow_proposal",
            {
                "project_id": other.id,
                "proposal_id": foreign_proposal["id"],
                "decision": "reject",
                "reason": too_long,
            },
        ),
    ):
        is_error, message = await _mcp(daemon, name, args)
        assert is_error, (name, args)
        assert message.startswith("Error:"), message  # a handled tool error, not a crash

    assert await _versions(client, pid) == []  # no refused call changed anything
    assert [p["state"] for p in await _proposals(client, other.id)] == ["pending"]
    assert len(await _versions(client, other.id)) == 1


# --- the post-task hook, end to end ---------------------------------------------


async def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    await run_git(path, "config", "user.email", "test@example.com")
    await run_git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# test repo\n", encoding="utf-8")
    await run_git(path, "add", "-A")
    await run_git(path, "commit", "-m", "initial commit")


@pytest.mark.parametrize("mode", ["off", "suggest", "auto"])
async def test_a_failed_task_follows_the_evolution_mode(
    mode: str, db: Database, tmp_path: Path
) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    config: dict[str, Any] = {
        "execution": {"baseline_test_command": "test -f FEATURE.txt"},
        "agent_overrides": {"developer": {"max_retries": 1}},
    }
    if mode != "suggest":  # suggest is the default
        config["experimental"] = {"workflow_evolution": mode}
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main", config=config
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )
    # The developer never writes the file, so the gate stays red and the task fails.
    orchestrator = Orchestrator(
        db, EventBus(db), FakeProvider(developer_writes=False), tmp_path / "home"
    )
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is False

    stored = await repo.get_project(db, project.id)
    assert stored is not None
    events = await repo.list_events(db, project_id=project.id, limit=10_000)
    workflow_events = [e.type for e in events if e.type.startswith("workflow.")]
    proposals = await repo.list_workflow_proposals(db, project.id)
    versions = await repo.list_workflow_versions(db, project.id)
    if mode == "off":
        assert workflow_events == [] and proposals == [] and versions == []
        assert "workflow" not in stored.config
    elif mode == "suggest":
        assert workflow_events == ["workflow.evolution_proposed"]
        assert [(p.state.value, p.reason["task_id"]) for p in proposals] == [
            ("pending", task.id)
        ]
        assert proposals[0].reason["action"] == "grow" and proposals[0].reason["summary"]
        assert versions == []
        assert "workflow" not in stored.config
    else:
        assert workflow_events == ["workflow.evolved", "workflow.updated"]
        assert proposals == []
        assert [v.source.value for v in versions] == ["evolver"]
        assert versions[0].graph_before is None
        assert stored.config["workflow"] == versions[0].graph_after


# --- the setting and the tool schemas -------------------------------------------


async def test_the_evolution_setting_defaults_to_suggest_and_is_settable(
    db: Database, tmp_path: Path, daemon: Daemon, client: httpx.AsyncClient
) -> None:
    schema = (await client.get("/api/config/schema")).json()
    prop = schema["$defs"]["ExperimentalSettings"]["properties"]["workflow_evolution"]
    assert prop["enum"] == ["off", "suggest", "auto"] and prop["default"] == "suggest"

    project = await _project(db, tmp_path)
    url = f"/api/projects/{project.id}/config"

    async def mode() -> str:
        value: str = (await client.get(url)).json()["experimental"]["workflow_evolution"]
        return value

    assert await mode() == "suggest"
    resp = await client.patch(url, json={"config": {"experimental": {"workflow_evolution": "off"}}})
    assert resp.status_code == 200, resp.text
    assert await mode() == "off"
    resp = await client.patch(url, json={"config": {"experimental": {"workflow_evolution": "x"}}})
    assert resp.status_code == 422
    is_error, _ = await _mcp(
        daemon, "conclave_patch_config", {"project_id": project.id, "workflow_evolution": "auto"}
    )
    assert not is_error and await mode() == "auto"
    is_error, _ = await _mcp(
        daemon,
        "conclave_patch_config",
        {"project_id": project.id, "config": {"experimental": {"workflow_evolution": "suggest"}}},
    )
    assert not is_error and await mode() == "suggest"
    assert "experimental" not in ((await repo.get_project(db, project.id)) or project).config
    is_error, _ = await _mcp(
        daemon, "conclave_patch_config", {"project_id": project.id, "workflow_evolution": "x"}
    )
    assert is_error and await mode() == "suggest"

    await repo.upsert_store_item(  # a global value reaches the project
        db,
        kind=StoreItemKind.config_preset,
        name=GLOBAL_DEFAULTS_PRESET,
        payload={"experimental": {"workflow_evolution": "off"}},
    )
    assert await mode() == "off"


def test_mcp_exposes_the_history_tools_and_their_enums() -> None:
    by_name = {t["name"]: t["inputSchema"] for t in MCP_TOOLS}
    for name in (
        "conclave_list_workflow_versions",
        "conclave_revert_workflow",
        "conclave_list_workflow_proposals",
        "conclave_decide_workflow_proposal",
    ):
        assert name in by_name, name
        assert by_name[name]["required"][0] == "project_id"
    decide = by_name["conclave_decide_workflow_proposal"]
    assert decide["properties"]["decision"]["enum"] == ["approve", "reject"]
    assert decide["required"] == ["project_id", "proposal_id", "decision"]
    assert by_name["conclave_list_workflow_proposals"]["properties"]["state"]["enum"] == [
        "pending",
        "approved",
        "rejected",
    ]
    assert by_name["conclave_revert_workflow"]["required"] == ["project_id", "version_id"]
    assert by_name["conclave_evolve_workflow"]["properties"]["apply"]["type"] == "boolean"
    patch = by_name["conclave_patch_config"]["properties"]
    assert patch["workflow_evolution"]["enum"] == ["off", "suggest", "auto"]
    for name in (
        "conclave_patch_config",
        "conclave_set_workflow",
        "conclave_apply_workflow_template",
        "conclave_workflow_upsert_node",
        "conclave_workflow_remove_node",
        "conclave_workflow_upsert_edge",
        "conclave_workflow_remove_edge",
        "conclave_evolve_workflow",
        "conclave_revert_workflow",
        "conclave_decide_workflow_proposal",
    ):
        assert by_name[name]["properties"]["reason"]["type"] == "string", name
