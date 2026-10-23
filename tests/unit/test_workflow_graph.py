"""Unit tests for declarative WorkflowGraph validation, templates, and graph_executor."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conclave.config import (
    ConclaveConfig,
    EdgeCondition,
    NodeKind,
    OutputContract,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    build_default_workflow,
    load_project_config,
    workflow_templates,
)
from conclave.engine import (
    derive_planner_nodes,
    derive_review_nodes,
    is_parallel_review,
    resolve_workflow,
)


def test_default_workflow_and_templates_are_valid() -> None:
    default = build_default_workflow()
    node_ids = {n.id for n in default.nodes}
    assert {"planner", "developer", "tester", "security", "reviewer", "gate", "merge"} <= node_ids

    templates = workflow_templates()
    assert set(templates.keys()) == {"standard", "fast_track", "tdd_loop", "deep_audit"}
    assert templates["fast_track"].parallel_reviewers is False
    assert templates["tdd_loop"].parallel_reviewers is True
    assert templates["deep_audit"].parallel_reviewers is True


def test_workflow_rejects_duplicate_node_ids() -> None:
    with pytest.raises(ValidationError, match="duplicate node id"):
        WorkflowGraph(
            nodes=[
                WorkflowNode(id="dev", kind=NodeKind.developer),
                WorkflowNode(id="dev", kind=NodeKind.reviewer),
            ],
            edges=[],
        )


def test_workflow_rejects_unknown_edge_endpoints_and_self_loops() -> None:
    with pytest.raises(ValidationError, match="unknown target node"):
        WorkflowGraph(
            nodes=[WorkflowNode(id="dev", kind=NodeKind.developer)],
            edges=[WorkflowEdge(id="e1", source="dev", target="missing")],
        )

    with pytest.raises(ValidationError, match="self-loop"):
        WorkflowGraph(
            nodes=[WorkflowNode(id="dev", kind=NodeKind.developer)],
            edges=[WorkflowEdge(id="e1", source="dev", target="dev")],
        )


def test_workflow_rejects_forward_cycle_but_allows_on_fail_loopback() -> None:
    # Forward cycle (always/on_pass) is rejected
    with pytest.raises(ValidationError, match="forward cycle"):
        WorkflowGraph(
            nodes=[
                WorkflowNode(id="a", kind=NodeKind.developer),
                WorkflowNode(id="b", kind=NodeKind.reviewer),
            ],
            edges=[
                WorkflowEdge(id="e1", source="a", target="b", condition=EdgeCondition.always),
                WorkflowEdge(id="e2", source="b", target="a", condition=EdgeCondition.on_pass),
            ],
        )

    # Retry loopback with on_fail is valid
    graph = WorkflowGraph(
        nodes=[
            WorkflowNode(id="a", kind=NodeKind.developer),
            WorkflowNode(id="b", kind=NodeKind.reviewer),
        ],
        edges=[
            WorkflowEdge(id="e1", source="a", target="b", condition=EdgeCondition.always),
            WorkflowEdge(id="e2", source="b", target="a", condition=EdgeCondition.on_fail),
        ],
    )
    assert len(graph.edges) == 2


def test_graph_executor_derives_custom_and_conditional_nodes() -> None:
    custom_graph = WorkflowGraph(
        name="Custom",
        parallel_reviewers=True,
        nodes=[
            WorkflowNode(
                id="custom-plan",
                label="Custom Planner",
                kind=NodeKind.planner,
                agent="planner",
                output_contract=OutputContract.plan,
            ),
            WorkflowNode(
                id="dev",
                label="Dev",
                kind=NodeKind.developer,
                agent="developer",
                output_contract=OutputContract.diff,
            ),
            WorkflowNode(
                id="mandatory-rev",
                label="Mandatory Reviewer",
                kind=NodeKind.reviewer,
                agent="reviewer",
                output_contract=OutputContract.verdict,
                optional=False,
            ),
            WorkflowNode(
                id="db-specialist",
                label="DB Specialist",
                kind=NodeKind.custom,
                agent="architect",
                output_contract=OutputContract.verdict,
                optional=True,
                triggers=["db_schema"],
            ),
            WorkflowNode(
                id="disabled-rev",
                label="Disabled",
                kind=NodeKind.reviewer,
                agent="tester",
                enabled=False,
            ),
        ],
        edges=[
            WorkflowEdge(id="e1", source="custom-plan", target="dev"),
            WorkflowEdge(id="e2", source="dev", target="mandatory-rev"),
            WorkflowEdge(
                id="e3",
                source="dev",
                target="db-specialist",
                condition=EdgeCondition.on_diff_trigger,
            ),
        ],
    )
    cfg = load_project_config({"workflow": custom_graph.model_dump(mode="json")})
    assert resolve_workflow(cfg).name == "Custom"
    assert is_parallel_review(cfg) is True

    planners = derive_planner_nodes(cfg)
    assert [n.id for n in planners] == ["custom-plan"]

    # Non-DB diff -> only mandatory-rev
    plain_diff = "diff --git a/README.md b/README.md\n+hello\n"
    rev_plain = derive_review_nodes(plain_diff, cfg)
    assert [n.id for n in rev_plain] == ["mandatory-rev"]

    # DB schema diff -> mandatory-rev + db-specialist
    db_diff = "diff --git a/schema.sql b/schema.sql\n+CREATE TABLE t (id INT);\n"
    rev_db = derive_review_nodes(db_diff, cfg)
    assert [n.id for n in rev_db] == ["mandatory-rev", "db-specialist"]

    # Fallback when workflow is None
    default_cfg = ConclaveConfig()
    assert is_parallel_review(default_cfg) is False
    rev_default = derive_review_nodes(plain_diff, default_cfg)
    assert [n.id for n in rev_default] == ["tester", "security", "reviewer"]


def test_workflow_node_pause_after_and_edge_require_human_sync() -> None:
    n1 = WorkflowNode(id="planner", kind=NodeKind.planner, pause_after=True)
    assert n1.pause_after is True
    assert n1.await_human_approval is True

    n2 = WorkflowNode(id="dev", kind=NodeKind.developer, await_human_approval=True)
    assert n2.pause_after is True
    assert n2.await_human_approval is True

    edge = WorkflowEdge(id="e1", source="planner", target="dev", require_human=True)
    assert edge.require_human is True


def test_hyphenated_agent_override_keys_are_unquoted() -> None:
    from conclave.config import resolve_agent

    cfg = load_project_config(
        {
            "agent_overrides": {
                '"architect-as-planner"': {"model": "google/gemini-2.5-pro", "effort": "high"},
                '\\"test-architect\\"': {"model": "google/gemini-2.5-flash"},
                "'repo-analyst'": {"timeout_minutes": 45},
            }
        }
    )
    assert "architect-as-planner" in cfg.agent_overrides
    assert "test-architect" in cfg.agent_overrides
    assert "repo-analyst" in cfg.agent_overrides
    assert '"architect-as-planner"' not in cfg.agent_overrides

    resolved_arch = resolve_agent(cfg, "architect-as-planner")
    assert resolved_arch.model == "google/gemini-2.5-pro"
    assert str(resolved_arch.effort) == "high"
    resolved_test = resolve_agent(cfg, "test-architect")
    assert resolved_test.model == "google/gemini-2.5-flash"


def test_is_pass_through_review_for_pr_review_workflows() -> None:
    from conclave.engine.graph_executor import is_pass_through_review

    # Default workflow has a developer node and on_fail retry -> False
    assert is_pass_through_review(ConclaveConfig()) is False

    # PR review workflow without developer node -> True
    pr_wf = WorkflowGraph(
        nodes=[
            WorkflowNode(id="planner", kind=NodeKind.planner),
            WorkflowNode(id="reviewer", kind=NodeKind.reviewer, agent="reviewer"),
            WorkflowNode(id="gate", kind=NodeKind.gate),
            WorkflowNode(id="submitter", kind=NodeKind.custom, agent="submitter"),
        ],
        edges=[
            WorkflowEdge(
                id="e1", source="planner", target="reviewer", condition=EdgeCondition.always
            ),
            WorkflowEdge(
                id="e2", source="reviewer", target="gate", condition=EdgeCondition.always
            ),
            WorkflowEdge(
                id="e3",
                source="gate",
                target="submitter",
                condition=EdgeCondition.always,
                require_human=True,
            ),
        ],
    )
    assert is_pass_through_review(ConclaveConfig(workflow=pr_wf)) is True

