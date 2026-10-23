"""Workflow graph resolution and node selection for the orchestrator."""

from __future__ import annotations

from ..config import (
    ConclaveConfig,
    EdgeCondition,
    NodeKind,
    OutputContract,
    WorkflowGraph,
    WorkflowNode,
    build_default_workflow,
)
from .pipeline import _triggers_match, get_agent_pipeline


def resolve_workflow(config: ConclaveConfig) -> WorkflowGraph:
    """Return ``config.workflow`` if set, otherwise build the default workflow graph."""
    if config.workflow is not None:
        return config.workflow
    return build_default_workflow(
        config.agents,
        parallel_reviewers=config.execution.parallel_reviewers,
    )


def derive_review_nodes(diff: str, config: ConclaveConfig) -> list[WorkflowNode]:
    """Derive the ordered list of review :class:`WorkflowNode` instances for ``diff``."""
    if config.workflow is None:
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

    workflow = config.workflow
    diff_lower = diff.lower()
    lines = diff.split("\n")
    file_count = sum(1 for line in lines if line.startswith("diff --git"))
    has_new_files = any("new file mode" in line for line in lines)

    structural: dict[str, bool] = {
        "new_files": has_new_files,
        "files_gt_5": file_count > 5,
        "complexity_medium_plus": file_count > 3,
    }

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


def derive_planner_nodes(config: ConclaveConfig) -> list[WorkflowNode]:
    """Return all enabled planner (or pre-developer custom) nodes in the resolved workflow."""
    workflow = resolve_workflow(config)
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

