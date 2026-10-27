"""Workflow graph resolution and node selection for the orchestrator."""

from __future__ import annotations

from ..config import (
    ConclaveConfig,
    EdgeCondition,
    NodeKind,
    OutputContract,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    build_default_workflow,
)
from ..config.graph import _agent_label
from .pipeline import _triggers_match, get_agent_pipeline

_HIGH_RISK_TRIGGERS: list[str] = [
    "auth_change",
    "db_change",
    "db_schema",
    "migration",
    "new_dependency",
    "payment_change",
    "user_data_change",
    "infra_change",
    "deploy_config",
    "dockerfile",
    "api_change",
    "frontend_bundle",
]

_DOMAIN_SPECIALIST_TRIGGERS: dict[str, list[str]] = {
    "security": [
        "auth_change",
        "payment_change",
        "user_data_change",
        "env_var",
        "third_party_api",
    ],
    "architect": [
        "new_files",
        "files_gt_5",
        "complexity_medium_plus",
        "api_change",
        "db_schema",
        "db_change",
        "new_dependency",
    ],
    "performance": [
        "db_change",
        "db_schema",
        "api_change",
        "loop_operation",
        "frontend_bundle",
    ],
    "risk": [
        "files_gt_5",
        "complexity_medium_plus",
        "db_change",
        "auth_change",
        "payment_change",
        "infra_change",
    ],
    "devops": [
        "dockerfile",
        "env_var",
        "new_service",
        "deploy_config",
        "migration",
        "infra_change",
    ],
}


def resolve_workflow(config: ConclaveConfig) -> WorkflowGraph:
    """Return ``config.workflow`` if set, otherwise build the default workflow graph."""
    if config.workflow is not None:
        return config.workflow
    return build_default_workflow(
        config.agents,
        parallel_reviewers=config.execution.parallel_reviewers,
    )


def _diff_metrics(diff: str) -> tuple[str, int, int, bool, dict[str, bool]]:
    diff_lower = diff.lower()
    lines = diff.split("\n")
    file_count = sum(1 for line in lines if line.startswith("diff --git"))
    changed_lines = sum(
        1
        for line in lines
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )
    has_new_files = any("new file mode" in line for line in lines)
    structural: dict[str, bool] = {
        "new_files": has_new_files,
        "files_gt_5": file_count > 5,
        "complexity_medium_plus": file_count > 3,
    }
    return diff_lower, file_count, changed_lines, has_new_files, structural


def _ensure_reviewer_in_graph(
    nodes: list[WorkflowNode],
    edges: list[WorkflowEdge],
    agent_name: str,
    *,
    dev_id: str = "developer",
    gate_id: str = "gate",
    triggers: list[str] | None = None,
) -> None:
    existing = next(
        (n for n in nodes if n.id == agent_name or n.agent == agent_name),
        None,
    )
    target_id = existing.id if existing is not None else agent_name
    if existing is not None:
        existing.enabled = True
        existing.optional = False
    else:
        nodes.append(
            WorkflowNode(
                id=agent_name,
                label=_agent_label(agent_name),
                kind=NodeKind.reviewer,
                agent=agent_name,
                output_contract=OutputContract.verdict,
                optional=False,
                enabled=True,
                triggers=list(triggers or _DOMAIN_SPECIALIST_TRIGGERS.get(agent_name, [])),
                position_x=560.0,
                position_y=60.0 + len(nodes) * 80.0,
            )
        )

    dev_exists = any(n.id == dev_id for n in nodes)
    gate_exists = any(n.id == gate_id for n in nodes)
    if dev_exists:
        dev_edge = next(
            (
                e
                for e in edges
                if e.source == dev_id
                and e.target == target_id
                and e.condition != EdgeCondition.on_fail
            ),
            None,
        )
        if dev_edge is not None:
            dev_edge.condition = EdgeCondition.always
        else:
            edges.append(
                WorkflowEdge(
                    id=f"e-{dev_id}-{target_id}",
                    source=dev_id,
                    target=target_id,
                    condition=EdgeCondition.always,
                )
            )
        fail_edge = next(
            (
                e
                for e in edges
                if e.source == target_id
                and e.target == dev_id
                and e.condition == EdgeCondition.on_fail
            ),
            None,
        )
        if fail_edge is None:
            edges.append(
                WorkflowEdge(
                    id=f"e-{target_id}-{dev_id}-fail",
                    source=target_id,
                    target=dev_id,
                    condition=EdgeCondition.on_fail,
                    label="retry",
                )
            )
    if gate_exists:
        gate_edge = next(
            (
                e
                for e in edges
                if e.source == target_id
                and e.target == gate_id
                and e.condition != EdgeCondition.on_fail
            ),
            None,
        )
        if gate_edge is None:
            edges.append(
                WorkflowEdge(
                    id=f"e-{target_id}-{gate_id}",
                    source=target_id,
                    target=gate_id,
                    condition=EdgeCondition.on_pass,
                )
            )


def scale_workflow_for_task(
    request: str, diff: str, config: ConclaveConfig
) -> WorkflowGraph:
    """Elastically shrink or grow a task's workflow graph based on request & diff scope.

    * **Shrink** on trivial scope (request ``<= 80`` chars, single-file diff with ``<= 25``
      changed lines, and no high-risk triggers): prunes non-core reviewers down to the
      core ``reviewer`` node and disables optional planner nodes.
    * **Grow** on complex/high-risk scope (``files_gt_5``, ``new_files``, or domain triggers
      like ``db_change``, ``auth_change``, ``api_change``, ``new_dependency``,
      ``frontend_bundle``, ``deploy_config``): dynamically activates/grafts the matching
      specialist reviewer nodes and planner node even if omitted or disabled in the base
      workflow graph.
    """
    base = resolve_workflow(config).model_copy(deep=True)
    nodes = list(base.nodes)
    edges = list(base.edges)

    req_clean = (request or "").strip()
    req_lower = req_clean.lower()
    diff_lower, file_count, changed_lines, has_new_files, structural = _diff_metrics(diff)
    combined_text = f"{diff_lower}\n{req_lower}"

    high_risk_matched = (
        has_new_files
        or file_count > 5
        or _triggers_match(_HIGH_RISK_TRIGGERS, structural, combined_text)
    )
    is_trivial = (
        len(req_clean) <= 80
        and file_count <= 1
        and changed_lines <= 25
        and not high_risk_matched
    )

    dev_node = next((n for n in nodes if n.kind == NodeKind.developer), None)
    dev_id = dev_node.id if dev_node is not None else "developer"
    gate_node = next((n for n in nodes if n.kind == NodeKind.gate), None)
    gate_id = gate_node.id if gate_node is not None else "gate"

    if is_trivial:
        # Shrink: disable optional planner nodes and keep only the core 'reviewer' node.
        for n in nodes:
            if n.kind == NodeKind.planner and n.optional:
                n.enabled = False
            elif n.kind in (NodeKind.reviewer, NodeKind.custom):
                is_core = n.id == "reviewer" or n.agent == "reviewer"
                n.enabled = is_core

        if not any(
            n.enabled and n.kind in (NodeKind.reviewer, NodeKind.custom) for n in nodes
        ):
            _ensure_reviewer_in_graph(
                nodes, edges, "reviewer", dev_id=dev_id, gate_id=gate_id
            )

        return WorkflowGraph(
            version=base.version,
            name=base.name,
            description=base.description,
            template_id=base.template_id,
            parallel_reviewers=base.parallel_reviewers,
            nodes=nodes,
            edges=edges,
        )

    # Check if complex or domain-triggered (Grow)
    matched_specialists: list[str] = []
    for spec_agent, spec_triggers in _DOMAIN_SPECIALIST_TRIGGERS.items():
        if _triggers_match(spec_triggers, structural, combined_text):
            matched_specialists.append(spec_agent)

    is_complex = (
        high_risk_matched
        or file_count > 3
        or changed_lines > 80
        or len(req_clean) >= 250
        or bool(matched_specialists)
    )

    if is_complex:
        # Ensure planner is present and enabled on complex/high-risk tasks
        planner_node = next((n for n in nodes if n.kind == NodeKind.planner), None)
        if planner_node is not None:
            planner_node.enabled = True
            if file_count > 5 or len(req_clean) >= 250 or len(matched_specialists) >= 2:
                planner_node.optional = False
        elif any(n.id == dev_id for n in nodes):
            nodes.insert(
                0,
                WorkflowNode(
                    id="planner",
                    label="Planner",
                    kind=NodeKind.planner,
                    agent="planner",
                    output_contract=OutputContract.plan,
                    optional=False,
                    enabled=True,
                    position_x=80.0,
                    position_y=220.0,
                ),
            )
            edges.insert(
                0,
                WorkflowEdge(
                    id=f"e-planner-{dev_id}",
                    source="planner",
                    target=dev_id,
                    condition=EdgeCondition.always,
                ),
            )

        # Ensure core 'reviewer' and 'tester' are active on complex changes
        _ensure_reviewer_in_graph(nodes, edges, "reviewer", dev_id=dev_id, gate_id=gate_id)
        _ensure_reviewer_in_graph(nodes, edges, "tester", dev_id=dev_id, gate_id=gate_id)

        for spec in matched_specialists:
            _ensure_reviewer_in_graph(nodes, edges, spec, dev_id=dev_id, gate_id=gate_id)

    return WorkflowGraph(
        version=base.version,
        name=base.name,
        description=base.description,
        template_id=base.template_id,
        parallel_reviewers=base.parallel_reviewers or len(matched_specialists) >= 2,
        nodes=nodes,
        edges=edges,
    )



def derive_review_nodes(
    diff: str,
    config: ConclaveConfig,
    *,
    request: str | None = None,
) -> list[WorkflowNode]:
    """Derive the ordered list of review :class:`WorkflowNode` instances for ``diff``."""
    diff_lower, file_count, _, has_new_files, structural = _diff_metrics(diff)
    if request is not None:
        workflow = scale_workflow_for_task(request, diff, config)
    elif config.workflow is None:
        pipeline = get_agent_pipeline(diff, config.agents)
        default_graph = build_default_workflow(
            config.agents,
            parallel_reviewers=config.execution.parallel_reviewers,
        )
        by_id = {
            n.id: n for n in default_graph.nodes if n.kind == NodeKind.reviewer
        }
        return [
            by_id.get(name)
            or WorkflowNode(
                id=name,
                label=name,
                kind=NodeKind.reviewer,
                agent=name,
                output_contract=OutputContract.verdict,
            )
            for name in pipeline
        ]
    elif config.workflow.template_id == "fast_track" and (
        has_new_files
        or file_count > 3
        or _triggers_match(_HIGH_RISK_TRIGGERS, structural, diff_lower)
    ):
        workflow = scale_workflow_for_task("", diff, config)
    else:
        workflow = config.workflow

    dev_ids = {n.id for n in workflow.nodes if n.kind == NodeKind.developer}
    gate_ids = {n.id for n in workflow.nodes if n.kind in (NodeKind.gate, NodeKind.merge)}
    selected: list[WorkflowNode] = []

    for node in workflow.nodes:
        if not node.enabled:
            continue
        if node.kind not in (NodeKind.reviewer, NodeKind.custom):
            continue

        has_incoming_from_gate = any(
            edge.target == node.id
            and edge.source in gate_ids
            and edge.condition != EdgeCondition.on_fail
            for edge in workflow.edges
        )
        if has_incoming_from_gate:
            continue

        has_incoming_from_dev = any(
            edge.target == node.id
            and edge.source in dev_ids
            and edge.condition != EdgeCondition.on_fail
            for edge in workflow.edges
        )
        has_forward_to_dev = any(
            edge.source == node.id
            and edge.target in dev_ids
            and edge.condition != EdgeCondition.on_fail
            for edge in workflow.edges
        )
        if not (has_incoming_from_dev or not has_forward_to_dev):
            continue

        incoming = [
            edge
            for edge in workflow.edges
            if edge.target == node.id and edge.condition != EdgeCondition.on_fail
        ]
        is_conditional = node.optional or any(
            edge.condition == EdgeCondition.on_diff_trigger for edge in incoming
        )
        if is_conditional:
            if node.triggers and _triggers_match(node.triggers, structural, diff_lower):
                selected.append(node)
        else:
            selected.append(node)

    return selected


def derive_post_gate_nodes(config: ConclaveConfig) -> list[WorkflowNode]:
    """Return enabled custom/stage nodes wired after the green-gate (e.g. 'submitter')."""
    if config.workflow is None:
        return []
    workflow = config.workflow
    gate_ids = {n.id for n in workflow.nodes if n.kind == NodeKind.gate}
    if not gate_ids:
        return []
    selected: list[WorkflowNode] = []
    for node in workflow.nodes:
        if not node.enabled or node.kind in (NodeKind.gate, NodeKind.merge):
            continue
        has_incoming_from_gate = any(
            edge.target == node.id
            and edge.source in gate_ids
            and edge.condition != EdgeCondition.on_fail
            for edge in workflow.edges
        )
        if has_incoming_from_gate:
            selected.append(node)
    return selected


def derive_planner_nodes(
    config: ConclaveConfig,
    *,
    request: str | None = None,
    diff: str = "",
) -> list[WorkflowNode]:
    """Return all enabled planner (or pre-developer custom) nodes in the resolved workflow."""
    workflow = (
        scale_workflow_for_task(request, diff, config)
        if request is not None
        else resolve_workflow(config)
    )
    dev_ids = {n.id for n in workflow.nodes if n.kind == NodeKind.developer}
    selected: list[WorkflowNode] = []

    for node in workflow.nodes:
        if not node.enabled:
            continue
        if node.kind == NodeKind.planner:
            selected.append(node)
        elif node.kind == NodeKind.custom:
            has_forward_to_dev = any(
                edge.source == node.id
                and edge.target in dev_ids
                and edge.condition != EdgeCondition.on_fail
                for edge in workflow.edges
            )
            if has_forward_to_dev:
                selected.append(node)

    return selected


def is_parallel_review(config: ConclaveConfig) -> bool:
    """Return ``True`` when reviewers should be dispatched concurrently."""
    if config.workflow is not None:
        return config.workflow.parallel_reviewers or config.execution.parallel_reviewers
    return config.execution.parallel_reviewers


def is_pass_through_review(config: ConclaveConfig) -> bool:
    """Return ``True`` when reviewer fail verdicts should pass through to gate/submitter.

    In non-developer (PR review / audit) workflows — either when no ``developer`` node
    is enabled in ``config.workflow``, or when reviewer outgoing edges use
    ``condition: "always"`` with no ``on_fail`` edge looping back to a ``developer`` node —
    a reviewer returning ``"verdict": "fail"`` represents an audit finding to be compiled
    into ``REVIEW_VERDICTS_SUMMARY`` for downstream gate/submitter stages rather than
    aborting the attempt to post-mortem.
    """
    if config.workflow is None:
        return False
    workflow = config.workflow
    enabled_dev_ids = {
        n.id for n in workflow.nodes if n.enabled and n.kind == NodeKind.developer
    }
    if not enabled_dev_ids:
        return True

    reviewer_ids = {
        n.id
        for n in workflow.nodes
        if n.enabled and n.kind in (NodeKind.reviewer, NodeKind.custom)
    }
    has_on_fail_to_dev = any(
        e.source in reviewer_ids
        and e.target in enabled_dev_ids
        and e.condition == EdgeCondition.on_fail
        for e in workflow.edges
    )
    has_always_forward = any(
        e.source in reviewer_ids
        and e.target not in enabled_dev_ids
        and e.condition == EdgeCondition.always
        for e in workflow.edges
    )
    return has_always_forward and not has_on_fail_to_dev

