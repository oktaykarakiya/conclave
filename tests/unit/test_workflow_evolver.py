"""Unit tests for runtime elastic workflow scaling and longitudinal workflow evolution."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from conclave.config import (
    ConclaveConfig,
    EdgeCondition,
    NodeKind,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    load_project_config,
    workflow_templates,
)
from conclave.db import CheckpointStatus, Database, Project, Task, TaskState
from conclave.db import repositories as repo
from conclave.engine import (
    derive_planner_nodes,
    derive_review_nodes,
    evolve_project_workflow,
    scale_workflow_for_task,
    workflow_evolver,
)
from conclave.engine.workflow_evolver import describe_workflow_changes
from conclave.events import EventBus, EventType
from conclave.mcp.tools import call_mcp_tool
from conclave.providers import AgentResult, OnChunk, Provider, ResolvedProfile
from conclave.runtime import Daemon
from conclave.web import create_app


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


def test_scale_workflow_for_task_shrinks_trivial_scope() -> None:
    """Short request (<= 80 chars) + small single-file diff (<= 25 changed lines) with no
    high-risk triggers shrinks reviewers to ['reviewer'] and skips optional planner."""
    cfg = ConclaveConfig()
    trivial_request = "Fix typo in README heading"
    trivial_diff = (
        "diff --git a/README.md b/README.md\n"
        "index 1111111..2222222 100644\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1 @@\n"
        "-# Conclve\n"
        "+# Conclave\n"
    )

    scaled = scale_workflow_for_task(trivial_request, trivial_diff, cfg)
    enabled_reviewers = [
        n.id for n in scaled.nodes if n.enabled and n.kind.value == "reviewer"
    ]
    assert enabled_reviewers == ["reviewer"]

    derived_rev = derive_review_nodes(trivial_diff, cfg, request=trivial_request)
    assert [n.id for n in derived_rev] == ["reviewer"]

    derived_plan = derive_planner_nodes(cfg, request=trivial_request, diff=trivial_diff)
    assert derived_plan == []


def test_scale_workflow_for_task_grows_complex_and_high_risk_scope() -> None:
    """Complex/high-risk scope grafts specialist reviewers and planner even onto fast_track."""
    fast_track_wf = workflow_templates()["fast_track"]
    cfg = load_project_config({"workflow": fast_track_wf.model_dump(mode="json")})

    complex_request = (
        "Implement database migration for JWT authentication tokens, update API routes, "
        "and add new dependencies in package.json and Dockerfile."
    )
    complex_diff = (
        "diff --git a/src/db/migration.sql b/src/db/migration.sql\n"
        "new file mode 100644\n"
        "+CREATE TABLE auth_tokens (id TEXT PRIMARY KEY, jwt TEXT);\n"
        "diff --git a/src/api/routes.py b/src/api/routes.py\n"
        "+def login_endpoint(password: str):\n"
        "+    for item in items:\n"
        "+        pass\n"
        "diff --git a/Dockerfile b/Dockerfile\n"
        "+FROM python:3.12\n"
        "diff --git a/package.json b/package.json\n"
        '+{"dependencies": {"vite": "^6.0.0"}}\n'
    )

    scaled = scale_workflow_for_task(complex_request, complex_diff, cfg)
    reviewer_ids = {
        n.id for n in scaled.nodes if n.enabled and n.kind.value == "reviewer"
    }
    assert {
        "reviewer",
        "tester",
        "security",
        "architect",
        "performance",
        "risk",
        "devops",
    } <= reviewer_ids

    derived_rev = derive_review_nodes(complex_diff, cfg, request=complex_request)
    derived_ids = {n.id for n in derived_rev}
    assert {"reviewer", "tester", "security", "architect", "performance", "devops"} <= derived_ids

    derived_plan = derive_planner_nodes(cfg, request=complex_request, diff=complex_diff)
    assert [n.id for n in derived_plan] == ["planner"]


async def test_evolve_project_workflow_grows_on_gate_failure_and_grounded_catches(
    db: Database, tmp_path: Path
) -> None:
    """evolve_project_workflow promotes catching specialists, enables tester/planner, and bumps
    max_iterations when gate fails after review or grounded fail/block verdicts occur."""
    bus = EventBus(db)
    fast_track = workflow_templates()["fast_track"]
    project = await repo.create_project(
        db,
        name="evo-grow",
        path=str(tmp_path),
        default_branch="main",
        config={
            "workflow": fast_track.model_dump(mode="json"),
            "experimental": {"workflow_evolution": "auto"},
        },
    )
    task = await repo.create_task(
        db,
        project_id=project.id,
        title="Risky DB task",
        request="Update schema",
        state=TaskState.failed,
    )
    await repo.save_task_checkpoint(
        db,
        task_id=task.id,
        node_id="review",
        attempt=1,
        status=CheckpointStatus.completed,
    )
    await repo.save_task_checkpoint(
        db,
        task_id=task.id,
        node_id="gate",
        attempt=1,
        status=CheckpointStatus.failed,
    )
    await repo.add_verdict(
        db,
        task_id=task.id,
        attempt=1,
        agent="security",
        verdict="block",
        reason="Hardcoded token in src/auth.py",
        source="json",
        grounded_count=1,
        evidence=[{"file": "src/auth.py", "line": 12}],
    )

    res = await evolve_project_workflow(
        db, bus, project.id, task_id=task.id
    )
    assert res["evolved"] is True
    assert res["action"] == "grow"
    assert "tester" in res["promoted_agents"]
    assert "security" in res["promoted_agents"]
    assert "architect" in res["promoted_agents"]

    updated_proj = await repo.get_project(db, project.id)
    assert updated_proj is not None
    wf = load_project_config(updated_proj.config).workflow
    assert wf is not None
    by_id = {n.id: n for n in wf.nodes}
    assert (
        "tester" in by_id
        and by_id["tester"].enabled is True
        and by_id["tester"].optional is False
    )
    assert (
        "security" in by_id
        and by_id["security"].enabled is True
        and by_id["security"].optional is False
    )
    assert "planner" in by_id and by_id["planner"].enabled is True

    on_fail_edges = [e for e in wf.edges if e.condition == EdgeCondition.on_fail]
    assert on_fail_edges
    assert all(e.max_iterations >= 4 for e in on_fail_edges)

    events = await repo.list_events(db, project_id=project.id, limit=50)
    event_types = {e.type for e in events}
    assert EventType.workflow_evolved in event_types
    assert EventType.workflow_updated in event_types


async def test_evolve_project_workflow_shrinks_after_3_clean_done_tasks(
    db: Database, tmp_path: Path
) -> None:
    """After >= 3 consecutive clean done tasks where non-core reviewers had 0 grounded catches
    (or only ungrounded 'unknown' demotions), evolve_project_workflow shrinks them to
    optional=True with condition=EdgeCondition.on_diff_trigger."""
    bus = EventBus(db)
    deep_audit = workflow_templates()["deep_audit"]
    project = await repo.create_project(
        db,
        name="evo-shrink",
        path=str(tmp_path),
        default_branch="main",
        config={
            "workflow": deep_audit.model_dump(mode="json"),
            "experimental": {"workflow_evolution": "auto"},
        },
    )

    # Create 3 consecutive clean done tasks (including an ungrounded 'unknown' demotion)
    for idx in range(3):
        t = await repo.create_task(
            db,
            project_id=project.id,
            title=f"Clean task {idx}",
            request="Small clean improvement",
            state=TaskState.done,
        )
        await repo.add_verdict(
            db,
            task_id=t.id,
            attempt=1,
            agent="reviewer",
            verdict="pass",
            reason="LGTM",
            source="json",
            grounded_count=1,
        )
        await repo.add_verdict(
            db,
            task_id=t.id,
            attempt=1,
            agent="architect",
            verdict="unknown",
            reason="[DOWNGRADED from fail: ungrounded citation]",
            source="json",
            grounded_count=0,
        )

    res = await evolve_project_workflow(db, bus, project.id)
    assert res["evolved"] is True
    assert res["action"] == "shrink"
    assert res["clean_streak"] >= 3
    assert {"architect", "performance", "risk"} <= set(res["demoted_agents"])

    updated_proj = await repo.get_project(db, project.id)
    assert updated_proj is not None
    wf = load_project_config(updated_proj.config).workflow
    assert wf is not None
    by_id = {n.id: n for n in wf.nodes}
    for non_core in ("architect", "performance", "risk"):
        assert by_id[non_core].optional is True
        dev_edge = next(
            e
            for e in wf.edges
            if e.source == "developer"
            and e.target == non_core
            and e.condition != EdgeCondition.on_fail
        )
        assert dev_edge.condition == EdgeCondition.on_diff_trigger

    # Core 'reviewer' remains mandatory
    assert by_id["reviewer"].optional is False


async def test_workflow_evolve_rest_and_mcp_endpoints(
    db: Database, tmp_path: Path
) -> None:
    """Test POST /api/projects/{id}/workflow/evolve and conclave_evolve_workflow MCP tool."""
    daemon = Daemon(db, tmp_path / "home", _StubProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    transport = ASGITransport(app=app)

    project = await repo.create_project(
        db,
        name="api-evolve",
        path=str(tmp_path),
        default_branch="main",
        config={
            "workflow": workflow_templates()["fast_track"].model_dump(mode="json"),
            "experimental": {"workflow_evolution": "auto"},
        },
    )

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/api/projects/{project.id}/workflow/evolve",
            json={"gate_failed_after_review": True},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["evolved"] is True
        assert data["action"] == "grow"

        # Now create 3 clean done tasks and evolve via MCP tool to shrink
        for idx in range(3):
            await repo.create_task(
                db,
                project_id=project.id,
                title=f"Done {idx}",
                request="clean",
                state=TaskState.done,
            )

        mcp_data = await call_mcp_tool(
            daemon,
            "conclave_evolve_workflow",
            {"project_id": project.id},
        )
        assert mcp_data["evolved"] is True
        assert mcp_data["action"] == "shrink"


# --- evolution modes: off / suggest / auto ------------------------------------


async def _evolution_project(db: Database, tmp_path: Path, mode: str | None) -> Project:
    """A fast_track project; *mode* None keeps the default workflow_evolution."""
    fast_track = workflow_templates()["fast_track"]
    config: dict[str, Any] = {"workflow": fast_track.model_dump(mode="json")}
    if mode is not None:
        config["experimental"] = {"workflow_evolution": mode}
    return await repo.create_project(
        db, name=f"evo-{mode}", path=str(tmp_path), default_branch="main", config=config
    )


async def _growth_task(db: Database, project_id: str) -> Task:
    """A failed task whose gate failed after the review passed, with a grounded security block."""
    task = await repo.create_task(
        db, project_id=project_id, title="Risky", request="Update schema", state=TaskState.failed
    )
    await repo.save_task_checkpoint(
        db, task_id=task.id, node_id="review", attempt=1, status=CheckpointStatus.completed
    )
    await repo.save_task_checkpoint(
        db, task_id=task.id, node_id="gate", attempt=1, status=CheckpointStatus.failed
    )
    await repo.add_verdict(
        db,
        task_id=task.id,
        attempt=1,
        agent="security",
        verdict="block",
        reason="Hardcoded token in src/auth.py",
        source="json",
        grounded_count=1,
        evidence=[{"file": "src/auth.py", "line": 12}],
    )
    return task


async def _workflow_events(db: Database, project_id: str) -> list[str]:
    events = await repo.list_events(db, project_id=project_id, limit=500)
    return [e.type for e in events if e.type.startswith("workflow.")]


async def test_off_mode_post_task_hook_computes_nothing_and_changes_nothing(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _never(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("off mode must not compute an evolution")

    monkeypatch.setattr(workflow_evolver, "compute_workflow_evolution", _never)
    project = await _evolution_project(db, tmp_path, "off")
    task = await _growth_task(db, project.id)

    res = await evolve_project_workflow(
        db, EventBus(db), project.id, task_id=task.id, automatic=True
    )

    assert res["evolved"] is False and res["mode"] == "off"
    assert res["proposal"] is None and res["version"] is None
    assert (await repo.get_project(db, project.id)) == project
    assert await repo.list_workflow_proposals(db, project.id) == []
    assert await repo.list_workflow_versions(db, project.id) == []
    assert await _workflow_events(db, project.id) == []


async def test_suggest_mode_stores_one_proposal_and_leaves_the_workflow(
    db: Database, tmp_path: Path
) -> None:
    bus = EventBus(db)
    project = await _evolution_project(db, tmp_path, None)  # suggest is the default
    task = await _growth_task(db, project.id)

    res = await evolve_project_workflow(db, bus, project.id, task_id=task.id, automatic=True)

    assert res["mode"] == "suggest" and res["evolved"] is False and res["version"] is None
    assert (await repo.get_project(db, project.id)) == project  # the live workflow is untouched
    proposals = await repo.list_workflow_proposals(db, project.id)
    assert len(proposals) == 1
    proposal = proposals[0]
    assert res["proposal"]["id"] == proposal.id
    assert proposal.state.value == "pending"
    assert proposal.reason["action"] == "grow"
    assert proposal.reason["task_id"] == task.id
    assert "security" in proposal.reason["catching_reviewers"]
    assert proposal.reason["task_failed"] is True
    assert proposal.reason["gate_failed_after_review"] is True
    assert proposal.reason["summary"] and proposal.reason["changes"]
    assert proposal.base_graph == workflow_templates()["fast_track"].model_dump(mode="json")
    assert proposal.proposed_graph == res["workflow"]
    assert await _workflow_events(db, project.id) == ["workflow.evolution_proposed"]
    assert await repo.list_workflow_versions(db, project.id) == []

    # The same trigger again neither duplicates the pending proposal nor announces it again.
    again = await evolve_project_workflow(db, bus, project.id, task_id=task.id, automatic=True)
    assert again["proposal"]["id"] == proposal.id
    assert len(await repo.list_workflow_proposals(db, project.id)) == 1
    assert await _workflow_events(db, project.id) == ["workflow.evolution_proposed"]


async def test_auto_mode_applies_the_change_as_an_evolver_version(
    db: Database, tmp_path: Path
) -> None:
    project = await _evolution_project(db, tmp_path, "auto")
    task = await _growth_task(db, project.id)

    res = await evolve_project_workflow(
        db, EventBus(db), project.id, task_id=task.id, automatic=True
    )

    assert res["evolved"] is True and res["action"] == "grow" and res["proposal"] is None
    updated = await repo.get_project(db, project.id)
    assert updated is not None
    live = load_project_config(updated.config).workflow
    assert live is not None and "tester" in {n.id for n in live.nodes}
    versions = await repo.list_workflow_versions(db, project.id)
    assert len(versions) == 1
    version = versions[0]
    assert version.source.value == "evolver"
    assert version.graph_before == workflow_templates()["fast_track"].model_dump(mode="json")
    assert version.graph_after == live.model_dump(mode="json")
    assert version.reason.startswith("Grow:")
    assert res["version"]["id"] == version.id
    assert await _workflow_events(db, project.id) == ["workflow.evolved", "workflow.updated"]
    assert await repo.list_workflow_proposals(db, project.id) == []


async def test_manual_evolve_in_off_mode_previews_and_apply_applies_it(
    db: Database, tmp_path: Path
) -> None:
    bus = EventBus(db)
    project = await _evolution_project(db, tmp_path, "off")
    task = await _growth_task(db, project.id)

    preview = await evolve_project_workflow(db, bus, project.id, task_id=task.id)
    assert preview["evolved"] is False and preview["action"] == "grow"
    assert preview["proposal"] is None and preview["version"] is None
    assert preview["workflow"]["version"] == 2  # what it would become
    assert (await repo.get_project(db, project.id)) == project
    assert await repo.list_workflow_proposals(db, project.id) == []
    assert await _workflow_events(db, project.id) == []

    applied = await evolve_project_workflow(
        db, bus, project.id, task_id=task.id, apply=True, reason="  operator call  "
    )
    assert applied["evolved"] is True
    versions = await repo.list_workflow_versions(db, project.id)
    assert [(v.source.value, v.reason) for v in versions] == [("operator", "  operator call  ")]
    assert versions[0].graph_after == applied["workflow"]


def _node(node_id: str, kind: NodeKind = NodeKind.reviewer, **fields: Any) -> WorkflowNode:
    return WorkflowNode(id=node_id, kind=kind, agent=node_id, **fields)


def test_describe_workflow_changes_lists_each_change_in_a_stable_order() -> None:
    before = WorkflowGraph(
        nodes=[
            _node("developer", NodeKind.developer),
            _node("tester"),
            _node("architect", optional=True),
            _node("legal", enabled=False),
            _node("old"),
        ],
        edges=[
            WorkflowEdge(id="d-t", source="developer", target="tester"),
            WorkflowEdge(
                id="t-d", source="tester", target="developer", condition=EdgeCondition.on_fail
            ),
            WorkflowEdge(
                id="d-a",
                source="developer",
                target="architect",
                condition=EdgeCondition.on_diff_trigger,
            ),
            WorkflowEdge(id="d-o", source="developer", target="old"),
        ],
    )
    after = WorkflowGraph(
        parallel_reviewers=True,
        nodes=[
            _node("developer", NodeKind.developer),
            _node("tester", position_x=999.0),  # layout only: not reported
            _node("architect", triggers=["new_files"]),
            _node("legal"),
            _node("security"),
        ],
        edges=[
            WorkflowEdge(id="d-t", source="developer", target="tester"),
            WorkflowEdge(
                id="t-d",
                source="tester",
                target="developer",
                condition=EdgeCondition.on_fail,
                max_iterations=4,
            ),
            WorkflowEdge(id="d-a", source="developer", target="architect"),
            WorkflowEdge(id="d-s", source="developer", target="security"),
        ],
    )

    expected = [
        "make architect mandatory",
        "change architect: triggers",
        "enable node legal",
        "add reviewer node security (mandatory)",
        "remove node old",
        "edge tester -> developer (on_fail): max_iterations 3 -> 4",
        "edge developer -> architect: on_diff_trigger -> always",
        "add edge developer -> security (always)",
        "remove edge developer -> old (always)",
        "enable parallel reviewers",
    ]
    assert describe_workflow_changes(before, after) == expected
    assert describe_workflow_changes(before, after) == expected  # deterministic
    assert describe_workflow_changes(after, after) == []
