"""Declarative node-edge workflow graph models and built-in workflow templates."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from .models import AgentsPolicy


class NodeKind(StrEnum):
    """Execution role of a node in the workflow graph."""

    planner = "planner"
    developer = "developer"
    reviewer = "reviewer"
    gate = "gate"
    merge = "merge"
    custom = "custom"


class EdgeCondition(StrEnum):
    """Activation condition for traversing a directed edge."""

    always = "always"
    on_pass = "on_pass"
    on_fail = "on_fail"
    on_diff_trigger = "on_diff_trigger"


class OutputContract(StrEnum):
    """Expected output format from a node execution."""

    text = "text"
    json = "json"
    verdict = "verdict"
    plan = "plan"
    diff = "diff"


class WorkflowNode(BaseModel):
    """A single stage or agent invocation in a workflow graph."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    label: str = Field(default="", max_length=120)
    kind: NodeKind
    agent: str | None = Field(
        default=None,
        description="Agent persona name from DB (for planner/developer/reviewer/custom).",
    )
    output_contract: OutputContract = OutputContract.text
    prompt_template: str | None = Field(
        default=None,
        description="Optional custom prompt instructions injected into node dispatch.",
    )
    triggers: list[str] = Field(
        default_factory=list,
        description=(
            "Diff triggers (structural or keyword) when this node is conditionally activated."
        ),
    )
    optional: bool = Field(
        default=False,
        description=(
            "If True, node only runs when its diff triggers match or use_planner is active."
        ),
    )
    enabled: bool = Field(default=True)
    position_x: float = Field(default=0.0)
    position_y: float = Field(default=0.0)
    model: str | None = Field(
        default=None,
        description="Optional per-node model override (provider/model).",
    )
    effort: str | None = Field(
        default=None,
        description="Optional per-node reasoning effort/variant override.",
    )
    cli_agent: str | None = Field(
        default=None,
        description="Optional per-node CLI --agent override.",
    )
    skills: list[str] = Field(
        default_factory=list,
        description="Per-node skills injected into dispatch.",
    )
    thinking: bool | None = Field(
        default=None,
        description="Optional per-node thinking toggle.",
    )
    compact_before_start: bool | None = Field(
        default=None,
        description=(
            "Optional per-node compaction of recoverable bulk (diffs, gate/test output). "
            "True or False overrides the agent setting; None inherits it."
        ),
    )
    pause_after: bool = Field(
        default=False,
        description=(
            "If True, pause the task at waiting_for_approval after this node finishes "
            "and wait for operator/MCP approval before advancing."
        ),
    )
    await_human_approval: bool = Field(
        default=False,
        description="Alias for pause_after: wait for human approval after this node finishes.",
    )

    @model_validator(mode="after")
    def _sync_pause_flags(self) -> Self:
        if self.pause_after or self.await_human_approval:
            self.pause_after = True
            self.await_human_approval = True
        return self


class WorkflowEdge(BaseModel):
    """A directed transition between two workflow nodes."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    source: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=64)
    condition: EdgeCondition = EdgeCondition.always
    max_iterations: int = Field(
        default=3,
        ge=1,
        le=50,
        description="Bound on retry loopback edges (on_fail).",
    )
    label: str = Field(default="", max_length=80)
    require_human: bool = Field(
        default=False,
        description=(
            "If True, pause on this connector for Human-in-the-Loop approval "
            "before advancing to the target node."
        ),
    )


class WorkflowGraph(BaseModel):
    """Declarative DAG of workflow nodes with bounded retry loopback edges."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1)
    name: str = Field(default="Standard Conclave", max_length=120)
    description: str = Field(
        default="Plan -> Develop -> Grounded Multi-Agent Review -> Green Gate -> Merge",
        max_length=500,
    )
    template_id: str | None = Field(default="standard")
    parallel_reviewers: bool = Field(default=False)
    nodes: list[WorkflowNode] = Field(default_factory=list)
    edges: list[WorkflowEdge] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_topology(self) -> Self:
        node_ids: set[str] = set()
        for node in self.nodes:
            if node.id in node_ids:
                raise ValueError(f"duplicate node id: {node.id!r}")
            node_ids.add(node.id)

        adj: dict[str, list[str]] = {nid: [] for nid in node_ids}
        for edge in self.edges:
            if edge.source not in node_ids:
                raise ValueError(f"edge {edge.id!r} references unknown source node {edge.source!r}")
            if edge.target not in node_ids:
                raise ValueError(f"edge {edge.id!r} references unknown target node {edge.target!r}")
            if edge.source == edge.target:
                raise ValueError(f"edge {edge.id!r} is a self-loop on node {edge.source!r}")
            if edge.condition != EdgeCondition.on_fail:
                adj[edge.source].append(edge.target)

        # 3-color DFS cycle detection (0=unvisited, 1=visiting, 2=visited) on forward edges
        color: dict[str, int] = dict.fromkeys(node_ids, 0)

        def _visit(nid: str) -> None:
            color[nid] = 1
            for nxt in adj[nid]:
                if color[nxt] == 1:
                    raise ValueError("workflow graph contains a forward cycle")
                if color[nxt] == 0:
                    _visit(nxt)
            color[nid] = 2

        for nid in node_ids:
            if color[nid] == 0:
                _visit(nid)

        return self


_AGENT_LABELS: dict[str, str] = {
    "planner": "Planner",
    "developer": "Developer",
    "tester": "Tester",
    "security": "Security Auditor",
    "reviewer": "Senior Reviewer",
    "architect": "Architect",
    "legal": "Legal & Compliance",
    "risk": "Risk Analyst",
    "performance": "Performance Engineer",
    "devops": "DevOps Engineer",
    "test-architect": "Test Architect",
    "pm": "Product Manager",
    "architect-as-planner": "Architect (Planner)",
}


def _agent_label(name: str) -> str:
    return _AGENT_LABELS.get(name, name.replace("-", " ").replace("_", " ").title())


def build_default_workflow(
    policy: AgentsPolicy | None = None,
    *,
    parallel_reviewers: bool = False,
) -> WorkflowGraph:
    """Construct the canonical Conclave workflow graph from an :class:`AgentsPolicy`."""
    if policy is None:
        from .models import AgentsPolicy as _AgentsPolicy

        policy = _AgentsPolicy()

    nodes: list[WorkflowNode] = [
        WorkflowNode(
            id="planner",
            label="Planner",
            kind=NodeKind.planner,
            agent="planner",
            output_contract=OutputContract.plan,
            optional=True,
            position_x=80.0,
            position_y=220.0,
        ),
        WorkflowNode(
            id="developer",
            label="Developer",
            kind=NodeKind.developer,
            agent="developer",
            output_contract=OutputContract.diff,
            position_x=300.0,
            position_y=220.0,
        ),
    ]

    edges: list[WorkflowEdge] = [
        WorkflowEdge(
            id="e-planner-developer",
            source="planner",
            target="developer",
            condition=EdgeCondition.always,
        ),
    ]

    seen_reviewers: set[str] = {"planner", "developer", "gate", "merge"}
    reviewer_idx = 0

    for name in policy.mandatory:
        if name in seen_reviewers:
            continue
        seen_reviewers.add(name)
        y_pos = 60.0 + reviewer_idx * 90.0
        reviewer_idx += 1
        nodes.append(
            WorkflowNode(
                id=name,
                label=_agent_label(name),
                kind=NodeKind.reviewer,
                agent=name,
                output_contract=OutputContract.verdict,
                optional=False,
                position_x=560.0,
                position_y=y_pos,
            )
        )
        edges.extend(
            [
                WorkflowEdge(
                    id=f"e-developer-{name}",
                    source="developer",
                    target=name,
                    condition=EdgeCondition.always,
                ),
                WorkflowEdge(
                    id=f"e-{name}-gate",
                    source=name,
                    target="gate",
                    condition=EdgeCondition.on_pass,
                ),
                WorkflowEdge(
                    id=f"e-{name}-developer-fail",
                    source=name,
                    target="developer",
                    condition=EdgeCondition.on_fail,
                    label="retry",
                ),
            ]
        )

    for name, cond in policy.conditional.items():
        if name in seen_reviewers:
            continue
        seen_reviewers.add(name)
        y_pos = 60.0 + reviewer_idx * 90.0
        reviewer_idx += 1
        nodes.append(
            WorkflowNode(
                id=name,
                label=_agent_label(name),
                kind=NodeKind.reviewer,
                agent=name,
                output_contract=OutputContract.verdict,
                optional=True,
                triggers=list(cond.triggers),
                position_x=560.0,
                position_y=y_pos,
            )
        )
        edges.extend(
            [
                WorkflowEdge(
                    id=f"e-developer-{name}",
                    source="developer",
                    target=name,
                    condition=EdgeCondition.on_diff_trigger,
                    label="diff trigger",
                ),
                WorkflowEdge(
                    id=f"e-{name}-gate",
                    source=name,
                    target="gate",
                    condition=EdgeCondition.on_pass,
                ),
                WorkflowEdge(
                    id=f"e-{name}-developer-fail",
                    source=name,
                    target="developer",
                    condition=EdgeCondition.on_fail,
                    label="retry",
                ),
            ]
        )

    nodes.extend(
        [
            WorkflowNode(
                id="gate",
                label="Green Gate",
                kind=NodeKind.gate,
                position_x=840.0,
                position_y=220.0,
            ),
            WorkflowNode(
                id="merge",
                label="Commit & Merge",
                kind=NodeKind.merge,
                position_x=1080.0,
                position_y=220.0,
            ),
        ]
    )

    edges.extend(
        [
            WorkflowEdge(
                id="e-gate-merge",
                source="gate",
                target="merge",
                condition=EdgeCondition.on_pass,
            ),
            WorkflowEdge(
                id="e-gate-developer-fail",
                source="gate",
                target="developer",
                condition=EdgeCondition.on_fail,
                label="fix tests",
            ),
        ]
    )

    return WorkflowGraph(
        name="Standard Conclave",
        description="Plan -> Develop -> Grounded Multi-Agent Review -> Green Gate -> Merge",
        template_id="standard",
        parallel_reviewers=parallel_reviewers,
        nodes=nodes,
        edges=edges,
    )


def _build_fast_track() -> WorkflowGraph:
    nodes = [
        WorkflowNode(
            id="developer",
            label="Developer",
            kind=NodeKind.developer,
            agent="developer",
            output_contract=OutputContract.diff,
            position_x=160.0,
            position_y=220.0,
        ),
        WorkflowNode(
            id="reviewer",
            label="Senior Reviewer",
            kind=NodeKind.reviewer,
            agent="reviewer",
            output_contract=OutputContract.verdict,
            position_x=440.0,
            position_y=220.0,
        ),
        WorkflowNode(
            id="gate",
            label="Green Gate",
            kind=NodeKind.gate,
            position_x=720.0,
            position_y=220.0,
        ),
        WorkflowNode(
            id="merge",
            label="Commit & Merge",
            kind=NodeKind.merge,
            position_x=980.0,
            position_y=220.0,
        ),
    ]
    edges = [
        WorkflowEdge(
            id="e-developer-reviewer",
            source="developer",
            target="reviewer",
            condition=EdgeCondition.always,
        ),
        WorkflowEdge(
            id="e-reviewer-gate",
            source="reviewer",
            target="gate",
            condition=EdgeCondition.on_pass,
        ),
        WorkflowEdge(
            id="e-reviewer-developer-fail",
            source="reviewer",
            target="developer",
            condition=EdgeCondition.on_fail,
            label="retry",
        ),
        WorkflowEdge(
            id="e-gate-merge",
            source="gate",
            target="merge",
            condition=EdgeCondition.on_pass,
        ),
        WorkflowEdge(
            id="e-gate-developer-fail",
            source="gate",
            target="developer",
            condition=EdgeCondition.on_fail,
            label="fix tests",
        ),
    ]
    return WorkflowGraph(
        name="Fast Track",
        description="Developer -> Senior Reviewer -> Green Gate -> Merge (minimal latency)",
        template_id="fast_track",
        parallel_reviewers=False,
        nodes=nodes,
        edges=edges,
    )


def _build_tdd_loop() -> WorkflowGraph:
    nodes = [
        WorkflowNode(
            id="test-architect",
            label="Test Architect",
            kind=NodeKind.planner,
            agent="test-architect",
            output_contract=OutputContract.plan,
            optional=False,
            position_x=80.0,
            position_y=220.0,
        ),
        WorkflowNode(
            id="developer",
            label="Developer",
            kind=NodeKind.developer,
            agent="developer",
            output_contract=OutputContract.diff,
            position_x=320.0,
            position_y=220.0,
        ),
        WorkflowNode(
            id="tester",
            label="Tester",
            kind=NodeKind.reviewer,
            agent="tester",
            output_contract=OutputContract.verdict,
            position_x=560.0,
            position_y=160.0,
        ),
        WorkflowNode(
            id="reviewer",
            label="Senior Reviewer",
            kind=NodeKind.reviewer,
            agent="reviewer",
            output_contract=OutputContract.verdict,
            position_x=560.0,
            position_y=280.0,
        ),
        WorkflowNode(
            id="gate",
            label="Green Gate",
            kind=NodeKind.gate,
            position_x=840.0,
            position_y=220.0,
        ),
        WorkflowNode(
            id="merge",
            label="Commit & Merge",
            kind=NodeKind.merge,
            position_x=1080.0,
            position_y=220.0,
        ),
    ]
    edges = [
        WorkflowEdge(
            id="e-test-architect-developer",
            source="test-architect",
            target="developer",
            condition=EdgeCondition.always,
        ),
        WorkflowEdge(
            id="e-developer-tester",
            source="developer",
            target="tester",
            condition=EdgeCondition.always,
        ),
        WorkflowEdge(
            id="e-developer-reviewer",
            source="developer",
            target="reviewer",
            condition=EdgeCondition.always,
        ),
        WorkflowEdge(
            id="e-tester-gate",
            source="tester",
            target="gate",
            condition=EdgeCondition.on_pass,
        ),
        WorkflowEdge(
            id="e-tester-developer-fail",
            source="tester",
            target="developer",
            condition=EdgeCondition.on_fail,
            label="retry",
        ),
        WorkflowEdge(
            id="e-reviewer-gate",
            source="reviewer",
            target="gate",
            condition=EdgeCondition.on_pass,
        ),
        WorkflowEdge(
            id="e-reviewer-developer-fail",
            source="reviewer",
            target="developer",
            condition=EdgeCondition.on_fail,
            label="retry",
        ),
        WorkflowEdge(
            id="e-gate-merge",
            source="gate",
            target="merge",
            condition=EdgeCondition.on_pass,
        ),
        WorkflowEdge(
            id="e-gate-developer-fail",
            source="gate",
            target="developer",
            condition=EdgeCondition.on_fail,
            label="fix tests",
        ),
    ]
    return WorkflowGraph(
        name="TDD Loop",
        description=(
            "Test Architect -> Developer -> Tester + Reviewer (parallel) -> Green Gate -> Merge"
        ),
        template_id="tdd_loop",
        parallel_reviewers=True,
        nodes=nodes,
        edges=edges,
    )


def _build_deep_audit() -> WorkflowGraph:
    swarm = ["tester", "security", "reviewer", "architect", "performance", "risk"]
    nodes: list[WorkflowNode] = [
        WorkflowNode(
            id="planner",
            label="Planner",
            kind=NodeKind.planner,
            agent="planner",
            output_contract=OutputContract.plan,
            optional=False,
            position_x=80.0,
            position_y=260.0,
        ),
        WorkflowNode(
            id="developer",
            label="Developer",
            kind=NodeKind.developer,
            agent="developer",
            output_contract=OutputContract.diff,
            position_x=300.0,
            position_y=260.0,
        ),
    ]
    edges: list[WorkflowEdge] = [
        WorkflowEdge(
            id="e-planner-developer",
            source="planner",
            target="developer",
            condition=EdgeCondition.always,
        ),
    ]
    for idx, name in enumerate(swarm):
        nodes.append(
            WorkflowNode(
                id=name,
                label=_agent_label(name),
                kind=NodeKind.reviewer,
                agent=name,
                output_contract=OutputContract.verdict,
                optional=False,
                position_x=560.0,
                position_y=60.0 + idx * 85.0,
            )
        )
        edges.extend(
            [
                WorkflowEdge(
                    id=f"e-developer-{name}",
                    source="developer",
                    target=name,
                    condition=EdgeCondition.always,
                ),
                WorkflowEdge(
                    id=f"e-{name}-gate",
                    source=name,
                    target="gate",
                    condition=EdgeCondition.on_pass,
                ),
                WorkflowEdge(
                    id=f"e-{name}-developer-fail",
                    source=name,
                    target="developer",
                    condition=EdgeCondition.on_fail,
                    label="retry",
                ),
            ]
        )
    nodes.extend(
        [
            WorkflowNode(
                id="gate",
                label="Green Gate",
                kind=NodeKind.gate,
                position_x=840.0,
                position_y=260.0,
            ),
            WorkflowNode(
                id="merge",
                label="Commit & Merge",
                kind=NodeKind.merge,
                position_x=1080.0,
                position_y=260.0,
            ),
        ]
    )
    edges.extend(
        [
            WorkflowEdge(
                id="e-gate-merge",
                source="gate",
                target="merge",
                condition=EdgeCondition.on_pass,
            ),
            WorkflowEdge(
                id="e-gate-developer-fail",
                source="gate",
                target="developer",
                condition=EdgeCondition.on_fail,
                label="fix tests",
            ),
        ]
    )
    return WorkflowGraph(
        name="Deep Audit",
        description=(
            "Planner -> Developer -> Parallel Swarm "
            "[Tester, Security, Reviewer, Architect, Performance, Risk] -> Green Gate -> Merge"
        ),
        template_id="deep_audit",
        parallel_reviewers=True,
        nodes=nodes,
        edges=edges,
    )


def workflow_templates(policy: AgentsPolicy | None = None) -> dict[str, WorkflowGraph]:
    """Return the built-in workflow presets keyed by ``template_id``."""
    return {
        "standard": build_default_workflow(policy),
        "fast_track": _build_fast_track(),
        "tdd_loop": _build_tdd_loop(),
        "deep_audit": _build_deep_audit(),
    }
