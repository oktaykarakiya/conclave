"""Integration tests for Conclave's MCP server (/mcp JSON-RPC 2.0 and /api/mcp/info)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest
from fake_provider import FakeProvider
from httpx import ASGITransport

from conclave.bootstrap import seed_global_defaults
from conclave.db import Database, TaskState
from conclave.db import repositories as repo
from conclave.engine.gitio import run_git
from conclave.runtime import Daemon
from conclave.web import create_app


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

        # 6. Export to Parquet and verify file is created with rows
        pq_path = tmp_path / "INTERACTION_LOGS.parquet"
        pq_info = await export_interaction_logs_to_parquet(db, target_path=pq_path)
        assert pq_info["ok"] is True
        assert pq_info["rows"] >= 3
        assert pq_path.is_file()
        assert pq_info["size_bytes"] > 0


async def test_mcp_inplace_attach_hyphenated_store_keys_and_global_settings(
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

        # 2. Attach project on 'main', then re-attach in-place to update default_branch
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
                        "default_branch": "main",
                    },
                },
            },
        )
        p1 = json.loads(a1.json()["result"]["content"][0]["text"])
        pid = p1["id"]
        assert p1["default_branch"] == "main"

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
                        "default_branch": "staging",
                        "config_preset": "acme-quality-argon",
                    },
                },
            },
        )
        p2 = json.loads(a2.json()["result"]["content"][0]["text"])
        assert p2["id"] == pid  # updated in-place without creating duplicate
        assert p2["default_branch"] == "staging"
        # The branch lives in default_branch, which the project inherits as its target_branch.
        assert "target_branch" not in p2["config"].get("execution", {})
        p2_effective = (await client.get(f"/api/projects/{pid}/config")).json()
        assert p2_effective["execution"]["target_branch"] == "staging"
        assert "architect-as-planner" in p2["config"]["agent_overrides"]

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

