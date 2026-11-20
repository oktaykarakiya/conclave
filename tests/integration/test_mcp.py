"""Integration tests for Conclave's MCP server (/mcp JSON-RPC 2.0 and /api/mcp/info)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fake_provider import FakeProvider
from httpx import ASGITransport

from conclave.bootstrap import seed_global_defaults
from conclave.db import Database, TaskState
from conclave.db import repositories as repo
from conclave.engine.gitio import run_git
from conclave.mcp import mcp_connect_snippets
from conclave.runtime import Daemon
from conclave.web import create_app
from waits import wait_until, within


async def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    (path / "README.md").write_text("# mcp-demo\n", encoding="utf-8")
    await run_git(path, "add", "README.md")
    await run_git(path, "commit", "-m", "initial")


async def test_mcp_initialize_tools_and_full_workflow_control(
    db: Database, tmp_path: Path
) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    await seed_global_defaults(db)

    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    transport = ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. GET /api/mcp/info returns connection snippets and tool list
        info_res = await client.get("/api/mcp/info")
        assert info_res.status_code == 200
        info = info_res.json()
        assert info["tool_count"] >= 28
        assert "conclave_workflow_upsert_node" in info["tools"]
        assert "mcp" in info["opencode_remote_json"]

        # 2. JSON-RPC initialize
        init_res = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            },
        )
        assert init_res.status_code == 200
        init_body = init_res.json()
        assert init_body["result"]["serverInfo"]["name"] == "conclave-mcp"

        # 3. Notification returns 202 with empty body
        notif_res = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert notif_res.status_code == 202

        # 4. tools/list returns all tools
        list_res = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        )
        tools = list_res.json()["result"]["tools"]
        assert len(tools) >= 28

        # 5. Attach project via MCP tool call
        attach_res = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "conclave_attach_project",
                    "arguments": {
                        "name": "mcp-proj",
                        "path": str(repo_path),
                        "default_branch": "main",
                    },
                },
            },
        )
        attach_payload = json.loads(
            attach_res.json()["result"]["content"][0]["text"]
        )
        pid = attach_payload["id"]

        # 6. Create custom agent persona + add node + connect edge on the workflow diagram via MCP
        await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "conclave_upsert_agent",
                    "arguments": {
                        "name": "mcp-auditor",
                        "role": "conditional",
                        "persona_md": "# MCP Auditor\nCheck everything.",
                    },
                },
            },
        )

        node_res = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "conclave_workflow_upsert_node",
                    "arguments": {
                        "project_id": pid,
                        "id": "mcp-auditor",
                        "kind": "reviewer",
                        "label": "MCP Auditor",
                        "agent": "mcp-auditor",
                    },
                },
            },
        )
        assert node_res.json()["result"]["isError"] is False

        edge_res = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "conclave_workflow_upsert_edge",
                    "arguments": {
                        "project_id": pid,
                        "source": "developer",
                        "target": "mcp-auditor",
                        "condition": "always",
                    },
                },
            },
        )
        wf_after = json.loads(edge_res.json()["result"]["content"][0]["text"])
        node_ids = {n["id"] for n in wf_after["workflow"]["nodes"]}
        assert "mcp-auditor" in node_ids

        # 7. Create a task via MCP and read resources/list + resources/read
        task_res = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "conclave_create_task",
                    "arguments": {
                        "project_id": pid,
                        "title": "MCP Task",
                        "request": "Add feature via MCP",
                        "auto_approve": False,
                    },
                },
            },
        )
        created_task = json.loads(task_res.json()["result"]["content"][0]["text"])
        assert created_task["state"] == "inbox"

        res_read = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 8,
                "method": "resources/read",
                "params": {"uri": f"conclave://projects/{pid}"},
            },
        )
        resource_data = json.loads(
            res_read.json()["result"]["contents"][0]["text"]
        )
        assert resource_data["project"]["id"] == pid
        assert len(resource_data["tasks"]) == 1


async def test_global_store_and_parallel_swarm_execution(
    db: Database, tmp_path: Path
) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    await seed_global_defaults(db)

    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    transport = ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Global Store is seeded with built-in workflows, agents, and config presets
        store_res = await client.get("/api/store")
        assert store_res.status_code == 200
        store_items = store_res.json()
        kinds = {i["kind"] for i in store_items}
        assert {"workflow", "agent", "config_preset"} <= kinds

        # 2. Publish a custom agent and config preset to the Global Store (with 0 repos attached)
        pub_agent = await client.post(
            "/api/store",
            json={
                "kind": "agent",
                "name": "migration-specialist",
                "description": "Specialist for large-scale DB and API migrations",
                "tags": ["migration", "swarm"],
                "payload": {
                    "role": "conditional",
                    "persona_md": "# Migration Specialist\nVerify backwards compatibility.",
                },
            },
        )
        assert pub_agent.status_code == 200
        custom_item_id = pub_agent.json()["id"]

        # 3. Attach a repo selecting a workflow blueprint + swarm config preset from the Store
        proj_res = await client.post(
            "/api/projects",
            json={
                "name": "swarm-repo",
                "path": str(repo_path),
                "default_branch": "main",
                "workflow_template": "fast_track",
                "config_preset": "high-throughput-swarm",
            },
        )
        assert proj_res.status_code == 200
        pid = proj_res.json()["id"]

        cfg_res = await client.get(f"/api/projects/{pid}/config")
        assert cfg_res.json()["execution"]["max_parallel_tasks"] == 4
        assert cfg_res.json()["workflow"]["template_id"] == "fast_track"

        # 4. Install the custom store agent onto the repo
        inst_res = await client.post(
            f"/api/projects/{pid}/store/install",
            json={"item_id": custom_item_id},
        )
        assert inst_res.status_code == 200
        assert inst_res.json()["installed"] == "migration-specialist"

        # 5. Launch a 3-unit Swarm via MCP conclave_launch_swarm
        swarm_mcp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 99,
                "method": "tools/call",
                "params": {
                    "name": "conclave_launch_swarm",
                    "arguments": {
                        "project_id": pid,
                        "concurrency": 3,
                        "units": [
                            {"title": "Module A", "request": "Refactor module A"},
                            {"title": "Module B", "request": "Refactor module B"},
                            {"title": "Module C", "request": "Refactor module C"},
                        ],
                    },
                },
            },
        )
        swarm_data = json.loads(swarm_mcp.json()["result"]["content"][0]["text"])
        swarm_id = swarm_data["swarm_id"]
        assert swarm_data["count"] == 3

        # 6. Verify claim_next_approved with max_parallel=3 claims all 3 concurrently!
        c1 = await repo.claim_next_approved(db, pid, max_parallel=3)
        c2 = await repo.claim_next_approved(db, pid, max_parallel=3)
        c3 = await repo.claim_next_approved(db, pid, max_parallel=3)
        c4 = await repo.claim_next_approved(db, pid, max_parallel=3)
        assert c1 is not None and c2 is not None and c3 is not None
        assert c4 is None  # capped at 3 concurrent in_progress tasks
        assert {c1.state, c2.state, c3.state} == {TaskState.in_progress}

        # 7. Check swarm status endpoint
        status_res = await client.get(f"/api/projects/{pid}/swarms/{swarm_id}")
        assert status_res.status_code == 200
        assert status_res.json()["counts"]["in_progress"] == 3

        # 8. Delete custom store item
        del_store = await client.delete(f"/api/store/{custom_item_id}")
        assert del_store.status_code == 200


async def test_observability_compaction_and_agent_config(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conclave.observability import export_interaction_logs_to_parquet

    # Both engines "installed", whatever this host has: the built-in model list offers only
    # the models of available engines.
    monkeypatch.setenv("CONCLAVE_OPENCODE_BIN", sys.executable)
    monkeypatch.setenv("CONCLAVE_CLAUDE_BIN", sys.executable)

    repo_path = tmp_path / "obs-repo"
    await _init_repo(repo_path)
    await seed_global_defaults(db)

    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    transport = ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Verify /api/models returns DeepSeek V4.1 Flash as default
        models_res = await client.get("/api/models")
        assert models_res.status_code == 200
        models_data = models_res.json()
        assert models_data["default_model"] == "deepseek/deepseek-flash"
        assert "tdd-strict" in models_data["skill_templates"]
        assert "claude-code/opus" in models_data["models"]
        assert models_data["engines"]["default"] == "opencode"
        assert set(models_data["engines"]["available"]) == {"opencode", "claude"}

        # 2. Attach project via MCP (this also logs an MCP interaction automatically)
        attach_mcp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 101,
                "method": "tools/call",
                "params": {
                    "name": "conclave_attach_project",
                    "arguments": {
                        "name": "obs-repo",
                        "path": str(repo_path),
                        "default_branch": "main",
                    },
                },
            },
        )
        pid = json.loads(attach_mcp.json()["result"]["content"][0]["text"])["id"]

        # 3. Configure developer agent via MCP conclave_configure_agent
        cfg_mcp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 102,
                "method": "tools/call",
                "params": {
                    "name": "conclave_configure_agent",
                    "arguments": {
                        "project_id": pid,
                        "agent": "developer",
                        "model": "google/gemini-2.5-flash-lite",
                        "effort": "low",
                        "cli_agent": "build",
                        "skills": ["tdd-strict", "minimal-diff"],
                        "thinking": False,
                        "compact_before_start": True,
                    },
                },
            },
        )
        cfg_out = json.loads(cfg_mcp.json()["result"]["content"][0]["text"])
        assert cfg_out["agent"] == "developer"
        assert cfg_out["settings"]["model"] == "google/gemini-2.5-flash-lite"
        assert cfg_out["settings"]["cli_agent"] == "build"
        assert cfg_out["settings"]["skills"] == ["tdd-strict", "minimal-diff"]

        # 4. Create a task with a large request and compact its context via MCP & REST
        long_req = "Implement feature X.\n" + ("Detailed context line for compaction.\n" * 300)
        task_res = await client.post(
            f"/api/projects/{pid}/tasks",
            json={"title": "Big Task", "request": long_req, "auto_approve": False},
        )
        tid = task_res.json()["id"]

        compact_res = await client.post(f"/api/tasks/{tid}/compact")
        assert compact_res.status_code == 200
        assert compact_res.json()["compacted"] is True

        compact_mcp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 103,
                "method": "tools/call",
                "params": {
                    "name": "conclave_compact_session",
                    "arguments": {"task_id": tid},
                },
            },
        )
        assert json.loads(compact_mcp.json()["result"]["content"][0]["text"])["compacted"] is True

        # 5. Verify interactions were logged automatically and queryable via REST & SQL
        stats_res = await client.get("/api/observability/stats")
        assert stats_res.status_code == 200
        assert stats_res.json()["total_sessions"] >= 3

        sessions_res = await client.get("/api/observability/sessions")
        assert sessions_res.status_code == 200
        sessions = sessions_res.json()
        assert len(sessions) >= 3
        sid = sessions[0]["session_id"]

        detail_res = await client.get(f"/api/observability/sessions/{sid}")
        assert detail_res.status_code == 200
        assert detail_res.json()["session_id"] == sid
        assert len(detail_res.json()["messages"]) >= 1

        sql_res = await client.post(
            "/api/observability/query",
            json={"sql": "SELECT session_id, agent, source FROM interaction_logs LIMIT 10"},
        )
        assert sql_res.status_code == 200
        assert sql_res.json()["row_count"] >= 3

        # Reject mutating SQL
        bad_sql = await client.post(
            "/api/observability/query",
            json={"sql": "DELETE FROM interaction_logs"},
        )
        assert bad_sql.status_code == 400

        # 6. Export the archive and verify the file is created with rows, its suffix naming
        # its format (.parquet with pyarrow, else .jsonl)
        pq_info = await export_interaction_logs_to_parquet(
            db, target_path=tmp_path / "INTERACTION_LOGS.parquet"
        )
        pq_path = Path(pq_info["path"])
        assert pq_info["ok"] is True
        assert pq_info["rows"] >= 3
        assert pq_path.is_file()
        assert pq_path.parent == tmp_path and pq_path.suffix == f".{pq_info['format']}"
        assert pq_info["size_bytes"] > 0


async def test_mcp_attach_hyphenated_store_keys_refuses_reattach_and_global_settings(
    db: Database, tmp_path: Path
) -> None:
    repo_path = tmp_path / "acme-repo"
    await _init_repo(repo_path)
    await seed_global_defaults(db)

    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    transport = ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Publish config preset with double-escaped hyphenated keys in agent_overrides
        pub_mcp = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 201,
                "method": "tools/call",
                "params": {
                    "name": "conclave_store_publish",
                    "arguments": {
                        "kind": "config_preset",
                        "name": "acme-quality-argon",
                        "description": "Argon quality preset",
                        "payload": {
                            "agent_defaults": {"model": "google/gemini-2.5-flash-lite"},
                            "agent_overrides": {
                                '"architect-as-planner"': {"model": "google/gemini-2.5-pro"},
                                '\\"test-architect\\"': {"model": "google/gemini-2.5-flash"},
                                "'repo-analyst'": {"timeout_minutes": 45},
                            },
                        },
                    },
                },
            },
        )
        pub_item = json.loads(pub_mcp.json()["result"]["content"][0]["text"])
        ov_keys = set(pub_item["payload"]["agent_overrides"].keys())
        assert ov_keys == {"architect-as-planner", "test-architect", "repo-analyst"}

        # 2. Attach the project with the preset on 'staging'
        a1 = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 202,
                "method": "tools/call",
                "params": {
                    "name": "conclave_attach_project",
                    "arguments": {
                        "name": "acme",
                        "path": str(repo_path),
                        "default_branch": "staging",
                        "config_preset": "acme-quality-argon",
                    },
                },
            },
        )
        p1 = json.loads(a1.json()["result"]["content"][0]["text"])
        pid = p1["id"]
        assert p1["default_branch"] == "staging"
        # The branch lives in default_branch, which the project inherits as its target_branch.
        assert "target_branch" not in p1["config"].get("execution", {})
        p1_effective = (await client.get(f"/api/projects/{pid}/config")).json()
        assert p1_effective["execution"]["target_branch"] == "staging"
        assert "architect-as-planner" in p1["config"]["agent_overrides"]

        # Attaching the same path again is refused, names the project and changes nothing.
        row = dict(await db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,)))
        a2 = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 203,
                "method": "tools/call",
                "params": {
                    "name": "conclave_attach_project",
                    "arguments": {
                        "name": "acme-updated",
                        "path": str(repo_path),
                        "default_branch": "main",
                    },
                },
            },
        )
        refused = a2.json()["result"]
        assert refused["isError"] is True
        assert pid in refused["content"][0]["text"]
        assert dict(await db.fetchone("SELECT * FROM projects WHERE id = ?", (pid,))) == row

        # 3. Update settings globally via MCP (without project_id); apply_to_all_projects clears
        # the patched keys from every project's delta so each one inherits the new value.
        global_patch = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 204,
                "method": "tools/call",
                "params": {
                    "name": "conclave_patch_config",
                    "arguments": {
                        "target_branch": "release",
                        "parallel_reviewers": True,
                        "max_parallel_tasks": 6,
                        "apply_to_all_projects": True,
                    },
                },
            },
        )
        g_cfg = json.loads(global_patch.json()["result"]["content"][0]["text"])
        assert g_cfg["execution"]["target_branch"] == "release"
        assert g_cfg["execution"]["parallel_reviewers"] is True
        assert g_cfg["execution"]["max_parallel_tasks"] == 6

        proj_after = (await client.get(f"/api/projects/{pid}")).json()
        assert proj_after["default_branch"] == "release"
        assert "target_branch" not in proj_after["config"].get("execution", {})
        effective = (await client.get(f"/api/projects/{pid}/config")).json()
        assert effective["execution"]["target_branch"] == "release"
        assert effective["execution"]["max_parallel_tasks"] == 6



async def test_get_mcp_is_405_while_info_and_post_are_unchanged(
    db: Database, tmp_path: Path
) -> None:
    """GET /mcp would open the optional server-to-client stream, which Conclave does not offer:
    405 with Allow: POST whatever the Accept header (a 200 made clients reopen it every
    second). GET /api/mcp/info and POST /mcp answer as before."""
    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    # The SPA mount at "/" is what answers an unrouted GET /mcp, so it must be present here.
    assert any(getattr(route, "name", None) == "spa" for route in app.routes)
    transport = ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for headers in (
            {},
            {"Accept": "text/event-stream"},
            {"Accept": "application/json, text/event-stream"},
        ):
            res = await client.get("/mcp", headers=headers)
            assert res.status_code == 405, headers
            assert "POST" in [m.strip() for m in res.headers["allow"].split(",")], headers
            assert "text/event-stream" not in res.headers.get("content-type", "")

        info = await client.get("/api/mcp/info")
        assert info.status_code == 200
        assert info.json() == mcp_connect_snippets("http://test")

        ping = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert ping.status_code == 200
        assert ping.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}


# --- operator SQL: POST /api/observability/query and conclave_query_interactions --------------

_SECRET_VALUE = "sk-never-returned"
_RUNAWAY_SQL = (
    "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT max(x) FROM c"
)


async def _store_secret(db: Database) -> None:
    await db.execute(
        "INSERT INTO secrets(id, name, value, created_at) VALUES (?, ?, ?, ?)",
        ("sec-1", "deepseek-key", _SECRET_VALUE, "2026-11-18T00:00:00+00:00"),
    )


async def _query_rest_and_mcp(
    db: Database, tmp_path: Path, sql: str
) -> tuple[httpx.Response, dict[str, object]]:
    """The REST answer to *sql*, and the MCP tool's ``result``."""
    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        rest = await c.post("/api/observability/query", json={"sql": sql})
        mcp = await c.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "conclave_query_interactions", "arguments": {"sql": sql}},
            },
        )
    result: dict[str, object] = mcp.json()["result"]
    return rest, result


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM secrets",
        "SELECT count(*) FROM secrets",
        "SELECT session_id FROM interaction_logs WHERE session_id IN (SELECT value FROM secrets)",
        "WITH s AS (SELECT * FROM secrets) SELECT name, value FROM s",
    ],
    ids=["direct", "count", "subquery", "with"],
)
async def test_operator_sql_cannot_read_the_secrets_table(
    db: Database, tmp_path: Path, sql: str
) -> None:
    await _store_secret(db)

    with pytest.raises(ValueError, match="cannot read the secrets table"):
        await repo.query_interaction_logs_readonly(db, sql)
    rest, mcp = await _query_rest_and_mcp(db, tmp_path, sql)

    assert rest.status_code == 400
    assert "cannot read the secrets table" in rest.json()["detail"]
    assert mcp["isError"] is True
    assert "cannot read the secrets table" in json.dumps(mcp["content"])
    assert _SECRET_VALUE not in rest.text + json.dumps(mcp)


async def test_operator_sql_cannot_read_raw_database_pages(db: Database) -> None:
    # sqlite_dbpage (in SQLite builds that have it) returns every raw page, the secrets
    # table's included: refused as well, or unknown where the build lacks it.
    await _store_secret(db)
    probe = sqlite3.connect(":memory:")
    try:
        probe.execute("SELECT * FROM sqlite_dbpage LIMIT 1")
        has_dbpage = True
    except sqlite3.Error:
        has_dbpage = False
    finally:
        probe.close()

    with pytest.raises(ValueError) as refused:
        await repo.query_interaction_logs_readonly(db, "SELECT data FROM sqlite_dbpage")

    if has_dbpage:
        assert str(refused.value) == "Read-only queries cannot read the sqlite_dbpage table"
    assert _SECRET_VALUE not in str(refused.value)


class _HeldClock:
    """The repositories module's ``time``, whose ``monotonic()`` stands still until the test moves
    it: a query's deadline passes when the test says, however slowly a loaded host runs the
    test meanwhile. Everything else is the real ``time``."""

    def __init__(self) -> None:
        self.now = 0.0
        self.reads = 0  # monotonic() calls: the query's deadline, then one per progress check

    def monotonic(self) -> float:
        self.reads += 1
        return self.now

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


async def test_a_runaway_operator_query_stops_at_its_deadline_and_blocks_nothing(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _HeldClock()
    monkeypatch.setattr(repo, "time", clock)
    monkeypatch.setattr(repo, "_READONLY_QUERY_TIMEOUT_S", 1.0)
    runaway = asyncio.create_task(repo.query_interaction_logs_readonly(db, _RUNAWAY_SQL))
    try:
        # Under way in its worker thread: it read its deadline, and its progress check ran.
        await wait_until(
            lambda: clock.reads >= 2 or runaway.done(), "the runaway query never got under way"
        )

        # Another database call of the daemon completes meanwhile, on the shared connection.
        listed = await within(repo.list_projects(db), "list_projects never completed meanwhile")
        assert listed == []
        assert not runaway.done()
    finally:
        clock.now = 2.0  # past the deadline: the query stops at its next progress check
    with pytest.raises(ValueError, match="took longer than 1 seconds and was stopped"):
        await runaway

    # The real clock again: the REST and MCP answers to a query its deadline stops.
    monkeypatch.setattr(repo, "time", time)
    monkeypatch.setattr(repo, "_READONLY_QUERY_TIMEOUT_S", 0.2)
    rest, mcp = await _query_rest_and_mcp(db, tmp_path, _RUNAWAY_SQL)
    assert rest.status_code == 400 and "took longer than" in rest.json()["detail"]
    assert mcp["isError"] is True and "took longer than" in json.dumps(mcp["content"])


async def test_an_ordinary_operator_query_answers_as_before(db: Database) -> None:
    for session_id, agent, cost in (("s-1", "developer", 0.5), ("s-2", "tester", 0.25)):
        await repo.record_interaction_log(db, session_id=session_id, agent=agent, cost=cost)
    sql = "SELECT session_id, agent, cost FROM interaction_logs ORDER BY session_id"

    answer = await repo.query_interaction_logs_readonly(db, sql, limit=1)

    # What the daemon's shared connection, where the query used to run, reads.
    cur = await db.conn.execute(sql)
    expected = [dict(row) for row in await cur.fetchall()]
    assert answer == {
        "columns": ["session_id", "agent", "cost"],
        "rows": expected[:1],
        "row_count": 1,
    }
    assert expected[0] == {"session_id": "s-1", "agent": "developer", "cost": 0.5}


# --- attaching with a preset or template that does not exist -----------------------------------


@pytest.mark.parametrize("surface", ["rest", "mcp"])
@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("config_preset", "unknown config_preset: 'no-such-preset'"),
        ("workflow_template", "unknown workflow_template: 'no-such-preset'"),
    ],
)
async def test_attaching_with_an_unknown_preset_or_template_is_refused(
    db: Database, tmp_path: Path, surface: str, field: str, message: str
) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    await seed_global_defaults(db)  # the built-in presets and workflows exist; this one not
    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    request = {"name": "t", "path": str(repo_path), field: "no-such-preset"}

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        if surface == "rest":
            res = await c.post("/api/projects", json=request)
            assert res.status_code == 422, res.text
            assert message in res.json()["detail"]
        else:
            res = await c.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "conclave_attach_project", "arguments": request},
                },
            )
            result = res.json()["result"]
            assert result["isError"] is True
            assert message in result["content"][0]["text"]

    assert await repo.list_projects(db) == []  # nothing attached
