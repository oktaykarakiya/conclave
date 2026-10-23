"""Integration tests for dynamic workflow graphs, step-level checkpoints, and idempotency."""

from __future__ import annotations

from pathlib import Path

import httpx
from fake_provider import FakeProvider
from httpx import ASGITransport

from conclave.bootstrap import seed_global_defaults
from conclave.db import CheckpointStatus, Database, TaskState
from conclave.db import repositories as repo
from conclave.engine import Orchestrator
from conclave.engine.gitio import run_git
from conclave.events import EventBus
from conclave.runtime import Daemon
from conclave.web import create_app


async def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    (path / "README.md").write_text("# demo\n", encoding="utf-8")
    await run_git(path, "add", "README.md")
    await run_git(path, "commit", "-m", "initial")


async def test_workflow_api_templates_custom_graph_and_agent_delete(
    db: Database, tmp_path: Path
) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    await seed_global_defaults(db)

    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    transport = ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        proj_res = await client.post(
            "/api/projects",
            json={"name": "demo", "path": str(repo_path), "default_branch": "main"},
        )
        assert proj_res.status_code == 200
        pid = proj_res.json()["id"]

        # 1. GET workflow returns standard graph + templates
        wf_res = await client.get(f"/api/projects/{pid}/workflow")
        assert wf_res.status_code == 200
        wf_data = wf_res.json()
        assert wf_data["is_custom"] is False
        assert "fast_track" in wf_data["templates"]
        assert "tdd_loop" in wf_data["templates"]

        # 2. Apply preset template 'fast_track'
        put_tpl = await client.put(
            f"/api/projects/{pid}/workflow",
            json={"template_id": "fast_track"},
        )
        assert put_tpl.status_code == 200
        assert put_tpl.json()["is_custom"] is True
        assert put_tpl.json()["workflow"]["template_id"] == "fast_track"

        # 3. Create custom agent persona, then delete it, and test default reset
        up_res = await client.put(
            "/api/agents/custom-auditor",
            json={"role": "conditional", "persona_md": "# Custom Auditor\nAudit everything."},
        )
        assert up_res.status_code == 200
        assert up_res.json()["name"] == "custom-auditor"

        del_res = await client.delete("/api/agents/custom-auditor")
        assert del_res.status_code == 200
        assert del_res.json() == {"deleted": "custom-auditor"}

        # Deleting a built-in global persona resets it to default
        reset_res = await client.delete("/api/agents/reviewer")
        assert reset_res.status_code == 200
        assert reset_res.json() == {"reset_to_default": "reviewer"}


async def test_idempotency_key_middleware_replays_and_guards_conflicts(
    db: Database, tmp_path: Path
) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    await seed_global_defaults(db)

    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    transport = ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        proj_res = await client.post(
            "/api/projects",
            json={"name": "demo", "path": str(repo_path), "default_branch": "main"},
        )
        pid = proj_res.json()["id"]

        # Create a task with an Idempotency-Key
        headers = {"Idempotency-Key": "idem-task-create-001"}
        r1 = await client.post(
            f"/api/projects/{pid}/tasks",
            json={"request": "implement feature", "title": "Feat"},
            headers=headers,
        )
        assert r1.status_code == 200
        assert r1.headers.get("x-idempotency-replayed") is None

        # Replay identical request -> returns exact same task ID without creating a duplicate
        r2 = await client.post(
            f"/api/projects/{pid}/tasks",
            json={"request": "implement feature", "title": "Feat"},
            headers=headers,
        )
        assert r2.status_code == 200
        assert r2.headers.get("x-idempotency-replayed") == "true"
        assert r2.json()["id"] == r1.json()["id"]

        tasks = (await client.get(f"/api/projects/{pid}/tasks")).json()
        assert len(tasks) == 1

        # Reusing the same Idempotency-Key on a different path -> 409 Conflict
        r_conflict = await client.post(
            f"/api/projects/{pid}/pause",
            headers=headers,
        )
        assert r_conflict.status_code == 409


async def test_step_level_checkpoints_persisted_and_resumed_after_crash(
    db: Database, tmp_path: Path
) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    await seed_global_defaults(db)

    project = await repo.create_project(
        db,
        name="demo",
        path=str(repo_path),
        default_branch="main",
        config={
            "execution": {"target_branch": "main", "parallel_reviewers": True},
        },
    )
    task = await repo.create_task(
        db,
        project_id=project.id,
        request="implement feature with planner",
        use_planner=True,
        state=TaskState.approved,
    )

    # Pre-seed a completed planner checkpoint to simulate a task that crashed after planning
    await repo.save_task_checkpoint(
        db,
        task_id=task.id,
        node_id="planner",
        attempt=0,
        status=CheckpointStatus.completed,
        artifact={"plan_preamble": "\n=== SAVED PLAN ===\n"},
    )

    provider = FakeProvider()
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True

    # Because the planner checkpoint was already completed, the planner LLM was NOT re-dispatched,
    # and the saved plan preamble was injected into the developer prompt!
    assert not any("Produce a structured plan" in p for p in provider.prompts)
    assert any("=== SAVED PLAN ===" in p for p in provider.prompts)

    # Verify all step-level checkpoints were recorded
    checkpoints = await repo.list_task_checkpoints(db, task.id)
    cp_nodes = {cp.node_id for cp in checkpoints}
    assert {"baseline", "planner", "developer", "review", "gate", "merge"} <= cp_nodes


async def test_pause_after_node_and_source_branch_pr_review(
    db: Database, tmp_path: Path
) -> None:
    import asyncio

    repo_path = tmp_path / "pr-repo"
    await _init_repo(repo_path)
    await run_git(repo_path, "config", "user.email", "dev@example.com")
    await run_git(repo_path, "config", "user.name", "Dev")

    # Create a 'staging' branch and a feature PR branch 'feature-login-fix' with an existing commit
    await run_git(repo_path, "checkout", "-b", "staging")
    await run_git(repo_path, "checkout", "-b", "feature-login-fix")
    (repo_path / "pr_feature.py").write_text("def calc():\n    return 42\n", encoding="utf-8")
    await run_git(repo_path, "add", "pr_feature.py")
    await run_git(repo_path, "commit", "-m", "feat: add pr_feature")
    await run_git(repo_path, "checkout", "staging")

    await seed_global_defaults(db)
    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    transport = ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        proj_res = await client.post(
            "/api/projects",
            json={
                "name": "pr-review-proj",
                "path": str(repo_path),
                "default_branch": "staging",
                "workflow_template": "fast_track",
            },
        )
        assert proj_res.status_code == 200
        pid = proj_res.json()["id"]

        # Configure 'reviewer' node with pause_after=True via MCP tool
        await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "conclave_workflow_upsert_node",
                    "arguments": {
                        "project_id": pid,
                        "id": "reviewer",
                        "kind": "reviewer",
                        "agent": "reviewer",
                        "pause_after": True,
                    },
                },
            },
        )

        # Create a PR review task with source_branch='feature-login-fix'
        task_res = await client.post(
            f"/api/projects/{pid}/tasks",
            json={
                "title": "Review PR feature-login-fix -> staging",
                "request": "Review PR changes in pr_feature.py",
                "source_branch": "feature-login-fix",
                "auto_approve": True,
            },
        )
        assert task_res.status_code == 200
        tid = task_res.json()["id"]
        assert task_res.json()["source_branch"] == "feature-login-fix"

        claimed = await repo.claim_next_approved(db, pid)
        assert claimed is not None

        # Run process_task in background; it should pause at waiting_for_approval after 'reviewer'
        run_future = asyncio.create_task(daemon.orchestrator.process_task(claimed))

        for _ in range(100):
            cur = await repo.get_task(db, tid)
            if cur is not None and cur.state == TaskState.waiting_for_approval:
                break
            await asyncio.sleep(0.05)

        paused_task = await repo.get_task(db, tid)
        assert paused_task is not None
        assert paused_task.state == TaskState.waiting_for_approval

        # Verify developer was skipped on attempt 1 (since source_branch already had commits)
        cps_while_paused = await repo.list_task_checkpoints(db, tid)
        dev_cp = next(cp for cp in cps_while_paused if cp.node_id == "developer")
        assert dev_cp.status == CheckpointStatus.skipped
        rev_cp = next(cp for cp in cps_while_paused if cp.node_id == "reviewer")
        assert rev_cp.status == CheckpointStatus.waiting_for_approval

        # Complete/approve the paused node via REST API
        comp_res = await client.post(
            f"/api/tasks/{tid}/nodes/reviewer/complete",
            json={"status": "completed", "verdict": "pass", "message": "LGTM from human"},
        )
        assert comp_res.status_code == 200
        assert comp_res.json()["ok"] is True

        ok = await asyncio.wait_for(run_future, timeout=10.0)
        assert ok is True

        final_task = await repo.get_task(db, tid)
        assert final_task is not None
        assert final_task.state == TaskState.done
