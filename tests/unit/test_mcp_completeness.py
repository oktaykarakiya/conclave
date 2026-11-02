"""Unit tests for MCP tool completeness, null-safety, bug fixes, and web API parity."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from conclave.config import resolve_agent, resolve_project_config
from conclave.db import CheckpointStatus, Database, StoreItemKind, TaskState
from conclave.db import repositories as repo
from conclave.events import EventType
from conclave.mcp.server import handle_mcp_message
from conclave.mcp.tools import MCP_TOOLS, _opt_bool, _opt_int, _opt_str, call_mcp_tool
from conclave.providers import AgentResult, OnChunk, Provider, ResolvedProfile
from conclave.runtime import Daemon


class _StubProvider(Provider):
    name = "stub"

    async def run_agent(
        self,
        *,
        profile: ResolvedProfile,
        prompt: str,
        timeout_seconds: int,
        cwd: Path | None = None,
        on_chunk: OnChunk | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AgentResult:
        return AgentResult(ok=True, text='```json\n{"verdict": "pass", "reason": "ok"}\n```')


def test_null_safe_helpers_and_tool_schemas() -> None:
    """_opt_str, _opt_int, _opt_bool handle None and MCP_TOOLS expose all parity schemas."""
    args: dict[str, Any] = {
        "title": None,
        "limit": None,
        "default_branch": None,
        "auto_approve": None,
        "flag_false": "false",
        "flag_true": "true",
    }
    assert _opt_str(args, "title", "") == ""
    assert _opt_str(args, "default_branch", "main") == "main"
    assert _opt_int(args, "limit", 50) == 50
    assert _opt_bool(args, "auto_approve", True) is True
    assert _opt_bool(args, "flag_false", True) is False
    assert _opt_bool(args, "flag_true", False) is True

    by_name = {t["name"]: t for t in MCP_TOOLS}
    assert "conclave_set_hitl_checkpoint" in by_name
    assert "conclave_get_reload_status" in by_name
    assert "conclave_get_observability_stats" in by_name

    create_props = by_name["conclave_create_task"]["inputSchema"]["properties"]
    assert "parent_task_id" in create_props

    patch_props = by_name["conclave_patch_config"]["inputSchema"]["properties"]
    for expected_field in (
        "name",
        "review_rounds_max",
        "stop_on_failure",
        "setup_timeout_seconds",
        "retention_events_max",
        "structured_verdicts",
        "persistent_baseline_cache",
        "auto_planner_char_threshold",
        "cross_attempt_memory_entries",
        "context",
        "planning",
        "agents",
        "bug_fixer",
        "notifications",
        "protected",
        "agent_defaults",
        "agent_overrides",
        "workflow",
        "allow_no_test_command",
        "test_integrity",
    ):
        assert expected_field in patch_props, f"missing {expected_field} in conclave_patch_config"
    assert patch_props["allow_no_test_command"]["type"] == "boolean"
    assert patch_props["test_integrity"]["enum"] == ["block", "warn", "off"]
    assert patch_props["unset"] == {
        "type": "array",
        "items": {"type": "string"},
        "description": patch_props["unset"]["description"],
    }
    assert patch_props["apply_to_all_projects"]["default"] is False

    get_props = by_name["conclave_get_config"]["inputSchema"]["properties"]
    assert get_props["include_layers"]["type"] == "boolean"
    assert get_props["include_layers"]["default"] is False


async def test_patch_config_round_trips_the_gate_integrity_fields(
    db: Database, tmp_path: Path
) -> None:
    """Both fields are settable via ``config.execution`` and via the explicit parameters."""
    daemon = Daemon(db, tmp_path / "home", _StubProvider(), workers_enabled=False)
    git_repo = tmp_path / "repo"
    (git_repo / ".git").mkdir(parents=True)
    pid = (
        await call_mcp_tool(daemon, "conclave_attach_project", {"name": "p", "path": str(git_repo)})
    )["id"]

    before = await call_mcp_tool(daemon, "conclave_get_config", {"project_id": pid})
    assert before["execution"]["allow_no_test_command"] is False
    assert before["execution"]["test_integrity"] == "block"

    await call_mcp_tool(
        daemon,
        "conclave_patch_config",
        {"project_id": pid, "config": {"execution": {"allow_no_test_command": True,
                                                    "test_integrity": "warn"}}},
    )
    via_config = await call_mcp_tool(daemon, "conclave_get_config", {"project_id": pid})
    assert via_config["execution"]["allow_no_test_command"] is True
    assert via_config["execution"]["test_integrity"] == "warn"

    await call_mcp_tool(
        daemon,
        "conclave_patch_config",
        {"project_id": pid, "allow_no_test_command": False, "test_integrity": "off"},
    )
    via_params = await call_mcp_tool(daemon, "conclave_get_config", {"project_id": pid})
    assert via_params["execution"]["allow_no_test_command"] is False
    assert via_params["execution"]["test_integrity"] == "off"


async def test_patch_config_rejects_an_unknown_test_integrity_mode(
    db: Database, tmp_path: Path
) -> None:
    daemon = Daemon(db, tmp_path / "home", _StubProvider(), workers_enabled=False)
    git_repo = tmp_path / "repo"
    (git_repo / ".git").mkdir(parents=True)
    pid = (
        await call_mcp_tool(daemon, "conclave_attach_project", {"name": "p", "path": str(git_repo)})
    )["id"]

    response = await handle_mcp_message(
        daemon,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "conclave_patch_config",
                "arguments": {"project_id": pid, "test_integrity": "strict"},
            },
        },
    )
    assert response is not None and response["result"]["isError"] is True
    config = await call_mcp_tool(daemon, "conclave_get_config", {"project_id": pid})
    assert config["execution"]["test_integrity"] == "block"  # the bad patch was not stored


async def test_mcp_null_arguments_do_not_produce_none_strings_or_type_error(
    db: Database, tmp_path: Path
) -> None:
    """Passing explicit JSON null values to MCP tools falls back to defaults cleanly."""
    daemon = Daemon(db, tmp_path / "home", _StubProvider(), workers_enabled=False)
    git_repo = tmp_path / "repo"
    (git_repo / ".git").mkdir(parents=True)

    proj = await call_mcp_tool(
        daemon,
        "conclave_attach_project",
        {
            "name": "demo",
            "path": str(git_repo),
            "default_branch": None,
            "config_preset": None,
            "workflow_template": None,
        },
    )
    pid = proj["id"]
    assert proj["default_branch"] == "main"

    listed_projects = await call_mcp_tool(
        daemon, "conclave_list_projects", {"limit": None}
    )
    assert len(listed_projects) == 1

    created = await call_mcp_tool(
        daemon,
        "conclave_create_task",
        {
            "project_id": pid,
            "request": "Implement feature",
            "title": None,
            "auto_approve": None,
            "parent_task_id": None,
            "source_branch": None,
        },
    )
    assert created["title"] == ""
    assert created["state"] == "approved"
    assert created["parent_task_id"] is None
    assert created["source_branch"] is None

    tasks = await call_mcp_tool(
        daemon,
        "conclave_list_tasks",
        {"project_id": pid, "state": None, "limit": None},
    )
    assert len(tasks) == 1

    global_cfg = await call_mcp_tool(
        daemon, "conclave_get_config", {"project_id": None}
    )
    assert "execution" in global_cfg


async def test_parity_tools_hitl_reload_status_and_observability_stats(
    db: Database, tmp_path: Path
) -> None:
    """Verify conclave_set_hitl_checkpoint, conclave_get_reload_status, and observability stats."""
    daemon = Daemon(db, tmp_path / "home", _StubProvider(), workers_enabled=False)
    project = await repo.create_project(
        db, name="p1", path=str(tmp_path), default_branch="main"
    )
    task = await repo.create_task(
        db, project_id=project.id, request="Test HITL", state=TaskState.in_progress
    )

    # 1. conclave_set_hitl_checkpoint arms and disarms target_id
    armed_res = await call_mcp_tool(
        daemon,
        "conclave_set_hitl_checkpoint",
        {"task_id": task.id, "target_id": "gate", "enabled": True},
    )
    assert armed_res["task_id"] == task.id
    assert armed_res["target_id"] == "gate"
    assert armed_res["enabled"] is True
    assert "gate" in armed_res["armed"]

    disarmed_res = await call_mcp_tool(
        daemon,
        "conclave_set_hitl_checkpoint",
        {"task_id": task.id, "target_id": "gate", "enabled": False},
    )
    assert disarmed_res["enabled"] is False
    assert "gate" not in disarmed_res["armed"]

    # 2. conclave_get_reload_status returns status without triggering reload
    reload_st = await call_mcp_tool(daemon, "conclave_get_reload_status", {})
    assert reload_st["reload_count"] == 0
    assert reload_st["quiescent"] is True
    assert reload_st["discoverable_modules"] > 0

    # 3. conclave_get_observability_stats returns stats and optional parquet export
    await repo.record_interaction_log(
        db,
        session_id="sess-obs-1",
        title="Test Session",
        model="google/gemini-4-argon",
        agent="developer",
        cost=0.12,
        tokens_input=100,
        tokens_output=50,
        project_id=project.id,
        task_id=task.id,
    )
    stats = await call_mcp_tool(
        daemon,
        "conclave_get_observability_stats",
        {"project_id": project.id, "export_parquet": True},
    )
    assert stats["total_sessions"] == 1
    assert stats["total_input_tokens"] == 100
    assert "parquet" in stats
    assert stats["parquet"]["ok"] is True


async def test_configure_agent_global_preserves_project_overrides(
    db: Database, tmp_path: Path
) -> None:
    """Global conclave_configure_agent reaches every project without clobbering its overrides.

    The configured fields are cleared from each project's delta (nothing is copied in), so the
    project inherits the new global value while its own execution and agent_overrides stay.
    """
    daemon = Daemon(db, tmp_path / "home", _StubProvider(), workers_enabled=False)

    # Seed global-defaults with an existing execution setting and reviewer override
    await repo.upsert_store_item(
        db,
        kind=StoreItemKind.config_preset,
        name="global-defaults",
        payload={
            "execution": {"target_branch": "main", "max_parallel_tasks": 1},
            "agent_overrides": {"reviewer": {"model": "global/reviewer-old"}},
        },
    )

    # Create project with custom execution and developer overrides
    project = await repo.create_project(
        db,
        name="custom-proj",
        path=str(tmp_path),
        default_branch="staging",
        config={
            "execution": {"target_branch": "staging", "max_parallel_tasks": 4},
            "agent_overrides": {
                "developer": {"model": "project/custom-dev", "timeout_minutes": 90},
                "reviewer": {"model": "project/custom-reviewer"},
            },
        },
    )

    # Update global agent_defaults via conclave_configure_agent
    res = await call_mcp_tool(
        daemon,
        "conclave_configure_agent",
        {
            "project_id": "global",
            "agent": "default",
            "model": "google/gemini-4-argon",
            "effort": "high",
        },
    )
    assert res["project_id"] == "global"
    assert res["agent_defaults"]["model"] == "google/gemini-4-argon"

    updated_proj = await repo.get_project(db, project.id)
    assert updated_proj is not None
    # Project's custom execution and agent_overrides must be untouched! (Its target_branch
    # equals its default_branch, which it inherits as its branch, so the delta drops it.)
    assert updated_proj.default_branch == "staging"
    assert updated_proj.config["execution"]["max_parallel_tasks"] == 4
    assert updated_proj.config["agent_overrides"]["developer"]["model"] == "project/custom-dev"
    assert updated_proj.config["agent_overrides"]["reviewer"]["model"] == "project/custom-reviewer"
    # ...and the new default is inherited, not frozen into the project.
    assert "agent_defaults" not in updated_proj.config
    effective = await resolve_project_config(db, updated_proj)
    assert effective.execution.target_branch == "staging"  # not the global "main"
    assert effective.agent_defaults.model == "google/gemini-4-argon"
    assert resolve_agent(effective, "tester").model == "google/gemini-4-argon"
    assert resolve_agent(effective, "tester").effort == "high"
    assert resolve_agent(effective, "reviewer").model == "project/custom-reviewer"

    events = await repo.list_events(db, project_id=project.id)
    assert any(e.type == EventType.project_updated for e in events)


async def test_complete_node_wildcard_resolves_target_node_in_daemon(
    db: Database, tmp_path: Path
) -> None:
    """conclave_complete_node with node_id='*' passes the resolved target_node to resolve_node."""
    daemon = Daemon(db, tmp_path / "home", _StubProvider(), workers_enabled=False)
    project = await repo.create_project(
        db, name="p-node", path=str(tmp_path), default_branch="main"
    )
    task = await repo.create_task(
        db,
        project_id=project.id,
        request="Pause at gate",
        state=TaskState.waiting_for_approval,
    )
    await repo.save_task_checkpoint(
        db,
        task_id=task.id,
        node_id="gate",
        attempt=1,
        status=CheckpointStatus.waiting_for_approval,
    )

    resolved_calls: list[tuple[str, str]] = []
    orig_resolve = daemon.resolve_node

    def _spy_resolve(task_id: str, node_id: str, **kwargs: Any) -> bool:
        resolved_calls.append((task_id, node_id))
        return orig_resolve(task_id, node_id, **kwargs)

    daemon.resolve_node = _spy_resolve  # type: ignore[method-assign]

    res = await call_mcp_tool(
        daemon,
        "conclave_complete_node",
        {"task_id": task.id, "node_id": "*", "status": "completed", "message": "LGTM"},
    )
    assert res["node_id"] == "gate"
    assert resolved_calls == [(task.id, "gate")]


async def test_approve_task_requeues_cancelled_and_blocked_and_emits_project_id(
    db: Database, tmp_path: Path
) -> None:
    """conclave_approve_task re-queues cancelled and blocked tasks and emits project_id."""
    daemon = Daemon(db, tmp_path / "home", _StubProvider(), workers_enabled=False)
    project = await repo.create_project(
        db, name="p-app", path=str(tmp_path), default_branch="main"
    )
    t_cancelled = await repo.create_task(
        db, project_id=project.id, request="cancelled task", state=TaskState.cancelled
    )
    t_blocked = await repo.create_task(
        db, project_id=project.id, request="blocked task", state=TaskState.blocked
    )

    res1 = await call_mcp_tool(
        daemon, "conclave_approve_task", {"task_id": t_cancelled.id}
    )
    assert res1 == {"approved": t_cancelled.id}
    after1 = await repo.get_task(db, t_cancelled.id)
    assert after1 is not None and after1.state is TaskState.approved

    res2 = await call_mcp_tool(
        daemon, "conclave_approve_task", {"task_id": t_blocked.id}
    )
    assert res2 == {"approved": t_blocked.id}
    after2 = await repo.get_task(db, t_blocked.id)
    assert after2 is not None and after2.state is TaskState.approved

    events = await repo.list_events(db, project_id=project.id)
    approved_events = [e for e in events if e.type == EventType.task_approved]
    assert len(approved_events) == 2
    assert {e.task_id for e in approved_events} == {t_cancelled.id, t_blocked.id}
    assert all(e.project_id == project.id for e in approved_events)


async def test_delete_agent_cleans_up_global_store_item_and_create_task_parent_id(
    db: Database, tmp_path: Path
) -> None:
    """conclave_delete_agent removes the Global Store item; conclave_create_task sets parent."""
    daemon = Daemon(db, tmp_path / "home", _StubProvider(), workers_enabled=False)
    project = await repo.create_project(
        db, name="p-tree", path=str(tmp_path), default_branch="main"
    )

    # 1. Upsert global custom agent -> verify store item created -> delete -> verify store item gone
    await call_mcp_tool(
        daemon,
        "conclave_upsert_agent",
        {
            "name": "custom-sql-auditor",
            "role": "conditional",
            "persona_md": "# SQL Auditor\nCheck queries.",
        },
    )
    store_before = await repo.get_store_item_by_name(
        db, StoreItemKind.agent, "custom-sql-auditor"
    )
    assert store_before is not None

    del_res = await call_mcp_tool(
        daemon, "conclave_delete_agent", {"name": "custom-sql-auditor"}
    )
    assert del_res == {"deleted": "custom-sql-auditor"}
    store_after = await repo.get_store_item_by_name(
        db, StoreItemKind.agent, "custom-sql-auditor"
    )
    assert store_after is None

    # 2. Create parent and child task via conclave_create_task with parent_task_id
    parent = await call_mcp_tool(
        daemon,
        "conclave_create_task",
        {
            "project_id": project.id,
            "title": "Parent Epic",
            "request": "Build parent module",
            "auto_approve": False,
        },
    )
    child = await call_mcp_tool(
        daemon,
        "conclave_create_task",
        {
            "project_id": project.id,
            "title": "Child Subtask",
            "request": "Build child submodule",
            "parent_task_id": parent["id"],
            "auto_approve": False,
        },
    )
    assert child["parent_task_id"] == parent["id"]
    children = await repo.get_child_tasks(db, parent["id"])
    assert [c.id for c in children] == [child["id"]]
