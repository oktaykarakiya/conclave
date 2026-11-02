"""Longitudinal closed-loop workflow evolution engine.

Inspects historical reviewer verdicts, gate outcomes, and task completion streaks to compute
how a project's :class:`WorkflowGraph` should grow or shrink. ``experimental.workflow_evolution``
decides what happens with that change: ``suggest`` (default) stores a proposal for a person to
approve or reject, ``auto`` applies it, ``off`` skips the post-task evolution. Every applied
change is recorded as a workflow version, so it can be reverted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..config import (
    ConclaveConfig,
    EdgeCondition,
    NodeKind,
    OutputContract,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    build_default_workflow,
    resolve_project_config,
    save_project_workflow,
)
from ..config.graph import _agent_label
from ..config.models import _DEFAULT_CONDITIONAL
from ..db import (
    CheckpointStatus,
    Database,
    Project,
    TaskState,
    VerdictRow,
    WorkflowConflictError,
    WorkflowProposal,
    WorkflowProposalState,
    WorkflowVersion,
    WorkflowVersionSource,
)
from ..db import repositories as repo
from ..events import EventBus, EventType

logger = logging.getLogger("conclave.engine.workflow_evolver")

# Longest reason a workflow version or proposal decision accepts from an operator.
REASON_MAX_CHARS = 2000

_NON_CORE_REVIEWERS: tuple[str, ...] = (
    "architect",
    "performance",
    "risk",
    "legal",
    "devops",
)

_GROWTH_SPECIALISTS: tuple[str, ...] = (
    "tester",
    "security",
    "architect",
    "risk",
    "performance",
)


def _is_grounded_catch(verdict_row: VerdictRow) -> bool:
    """Return ``True`` when *verdict_row* is a grounded ``fail`` or ``block`` catch."""
    if verdict_row.verdict not in ("fail", "block"):
        return False
    if "DOWNGRADED" in (verdict_row.reason or ""):
        return False
    return verdict_row.grounded_count > 0 or verdict_row.source in (
        "json",
        "string",
        "operator",
    )


def _ensure_reviewer_node(
    nodes: list[WorkflowNode],
    edges: list[WorkflowEdge],
    agent_name: str,
    *,
    optional: bool,
    dev_id: str = "developer",
    gate_id: str = "gate",
) -> bool:
    """Ensure reviewer *agent_name* exists in *nodes*/*edges* with the requested optionality.

    Returns ``True`` if the graph was mutated.
    """
    mutated = False
    existing = next(
        (n for n in nodes if n.id == agent_name or n.agent == agent_name),
        None,
    )
    target_id = existing.id if existing is not None else agent_name
    default_triggers = list(_DEFAULT_CONDITIONAL.get(agent_name, ["complexity_medium_plus"]))

    if existing is not None:
        if not existing.enabled:
            existing.enabled = True
            mutated = True
        if existing.optional != optional:
            existing.optional = optional
            mutated = True
        if optional and not existing.triggers:
            existing.triggers = default_triggers
            mutated = True
    else:
        nodes.append(
            WorkflowNode(
                id=agent_name,
                label=_agent_label(agent_name),
                kind=NodeKind.reviewer,
                agent=agent_name,
                output_contract=OutputContract.verdict,
                optional=optional,
                enabled=True,
                triggers=default_triggers if optional else [],
                position_x=560.0,
                position_y=60.0 + len(nodes) * 85.0,
            )
        )
        mutated = True

    target_cond = EdgeCondition.on_diff_trigger if optional else EdgeCondition.always
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
            if dev_edge.condition != target_cond:
                dev_edge.condition = target_cond
                dev_edge.label = "diff trigger" if optional else ""
                mutated = True
        else:
            edges.append(
                WorkflowEdge(
                    id=f"e-{dev_id}-{target_id}",
                    source=dev_id,
                    target=target_id,
                    condition=target_cond,
                    label="diff trigger" if optional else "",
                )
            )
            mutated = True

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
            mutated = True

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
            mutated = True

    return mutated


@dataclass(frozen=True)
class WorkflowEvolution:
    """The change evolution would make to a project's workflow, computed without writing."""

    base: WorkflowGraph  # the active graph the change was computed from
    evolved: WorkflowGraph
    had_custom_workflow: bool
    mutated: bool  # whether the evolved graph differs from the base
    action: str  # "grow", "shrink" or "none"
    clean_streak: int
    promoted_agents: list[str]
    demoted_agents: list[str]
    catching_reviewers: list[str]
    task_failed: bool
    gate_failed_after_review: bool


def _active_graph(config: ConclaveConfig) -> WorkflowGraph:
    """The graph a project runs: its workflow, else the one derived from its agents policy."""
    return config.workflow or build_default_workflow(
        config.agents,
        parallel_reviewers=config.execution.parallel_reviewers,
    )


async def compute_workflow_evolution(
    db: Database,
    project_id: str,
    config: ConclaveConfig,
    *,
    task_id: str | None = None,
    gate_failed_after_review: bool = False,
    task_failed: bool = False,
) -> WorkflowEvolution:
    """Compute how *project_id*'s workflow would evolve from outcomes and verdicts (reads only).

    * **Grow**: On ``gate_failed_after_review``, ``task_failed``, or grounded ``fail``/``block``
      catches by conditional/specialist reviewers, ensures ``tester`` and relevant specialists
      (``architect``, ``security``, ``risk``, ``performance``) are present and enabled, promotes
      catching reviewers to mandatory (``optional=False``, ``EdgeCondition.always``), and bumps
      ``max_iterations`` (up to 6) on ``on_fail`` loopback edges.
    * **Shrink**: On a clean streak of ``>= 3`` consecutive ``done`` tasks where non-core
      reviewers (``architect``, ``performance``, ``risk``, ``legal``, ``devops``) had ``0``
      grounded ``fail``/``block`` verdicts (or only ungrounded ``unknown`` demotions), demotes
      those non-core reviewers to ``optional=True`` (``EdgeCondition.on_diff_trigger``).
    """
    had_custom_workflow = config.workflow is not None
    base_graph = _active_graph(config).model_copy(deep=True)
    work_graph = base_graph.model_copy(deep=True)
    nodes = list(work_graph.nodes)
    edges = list(work_graph.edges)

    dev_node = next((n for n in nodes if n.kind == NodeKind.developer), None)
    dev_id = dev_node.id if dev_node is not None else "developer"
    gate_node = next((n for n in nodes if n.kind == NodeKind.gate), None)
    gate_id = gate_node.id if gate_node is not None else "gate"

    # Inspect target task if provided
    if task_id is not None:
        target_task = await repo.get_task(db, task_id)
        if target_task is not None and target_task.state == TaskState.failed:
            task_failed = True
        cps = await repo.list_task_checkpoints(db, task_id)
        review_passed_attempts = {
            cp.attempt
            for cp in cps
            if cp.node_id == "review" and cp.status == CheckpointStatus.completed
        }
        gate_failed_attempts = {
            cp.attempt
            for cp in cps
            if cp.node_id == "gate" and cp.status == CheckpointStatus.failed
        }
        if review_passed_attempts & gate_failed_attempts:
            gate_failed_after_review = True

    all_tasks = await repo.list_tasks(db, project_id, limit=50)
    terminal_tasks = [
        t for t in all_tasks if t.state in (TaskState.done, TaskState.failed)
    ]

    # Compute consecutive clean 'done' streak from most recent terminal task
    clean_done_tasks = []
    for t in terminal_tasks:
        if t.state == TaskState.done:
            clean_done_tasks.append(t)
        else:
            break
    clean_streak = len(clean_done_tasks)

    # Collect grounded catches on task_id or most recent task(s)
    catching_agents: set[str] = set()
    tasks_to_inspect = []
    if task_id is not None:
        t_obj = await repo.get_task(db, task_id)
        if t_obj is not None:
            tasks_to_inspect.append(t_obj)
    elif terminal_tasks:
        tasks_to_inspect.append(terminal_tasks[0])

    for t in tasks_to_inspect:
        v_rows = await repo.list_verdicts(db, t.id, limit=200)
        for v in v_rows:
            if _is_grounded_catch(v):
                catching_agents.add(v.agent)

    # If clean_streak < 3 and no explicit task_id catches, also check if any recent failed task
    # exists when task_id was not passed.
    if task_id is None and terminal_tasks and terminal_tasks[0].state == TaskState.failed:
        task_failed = True

    mutated = False
    action = "none"
    promoted_agents: list[str] = []
    demoted_agents: list[str] = []

    should_grow = (
        gate_failed_after_review or task_failed or bool(catching_agents)
    )

    if should_grow:
        action = "grow"
        if gate_failed_after_review or task_failed:
            # Ensure planner is present and enabled
            planner_node = next((n for n in nodes if n.kind == NodeKind.planner), None)
            if planner_node is not None:
                if not planner_node.enabled:
                    planner_node.enabled = True
                    mutated = True
                if task_failed and planner_node.optional:
                    planner_node.optional = False
                    mutated = True
            elif any(n.id == dev_id for n in nodes):
                nodes.insert(
                    0,
                    WorkflowNode(
                        id="planner",
                        label="Planner",
                        kind=NodeKind.planner,
                        agent="planner",
                        output_contract=OutputContract.plan,
                        optional=not task_failed,
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
                mutated = True

            # Ensure tester + specialist reviewers are present and mandatory
            for spec in _GROWTH_SPECIALISTS:
                make_optional = spec in ("risk", "performance") and not task_failed
                if _ensure_reviewer_node(
                    nodes,
                    edges,
                    spec,
                    optional=make_optional,
                    dev_id=dev_id,
                    gate_id=gate_id,
                ):
                    mutated = True
                if not make_optional and spec not in promoted_agents:
                    promoted_agents.append(spec)

        # Promote any reviewer that recorded grounded catches to mandatory (optional=False)
        for ag in sorted(catching_agents):
            if _ensure_reviewer_node(
                nodes,
                edges,
                ag,
                optional=False,
                dev_id=dev_id,
                gate_id=gate_id,
            ):
                mutated = True
            if ag not in promoted_agents:
                promoted_agents.append(ag)

        # Bump max_iterations (up to 6) on retry loopback edges
        for edge in edges:
            if edge.condition == EdgeCondition.on_fail and edge.max_iterations < 6:
                edge.max_iterations = min(6, max(edge.max_iterations + 1, 4))
                mutated = True

    elif clean_streak >= 3:
        # Inspect verdicts across the clean streak tasks
        streak_catches: dict[str, int] = dict.fromkeys(_NON_CORE_REVIEWERS, 0)
        for done_task in clean_done_tasks[:5]:
            verdicts = await repo.list_verdicts(db, done_task.id, limit=200)
            for v in verdicts:
                if v.agent in streak_catches and _is_grounded_catch(v):
                    streak_catches[v.agent] += 1

        for non_core, catch_count in streak_catches.items():
            if catch_count > 0:
                continue
            existing_node = next(
                (n for n in nodes if n.id == non_core or n.agent == non_core),
                None,
            )
            if existing_node is None:
                continue
            incoming_dev_edges = [
                e
                for e in edges
                if e.source == dev_id
                and e.target == existing_node.id
                and e.condition != EdgeCondition.on_fail
            ]
            was_mandatory = not existing_node.optional or any(
                e.condition == EdgeCondition.always for e in incoming_dev_edges
            )
            if _ensure_reviewer_node(
                nodes,
                edges,
                non_core,
                optional=True,
                dev_id=dev_id,
                gate_id=gate_id,
            ):
                mutated = True
            if was_mandatory or not had_custom_workflow:
                demoted_agents.append(non_core)

        if demoted_agents:
            action = "shrink"
            if not had_custom_workflow:
                mutated = True

    new_version = base_graph.version + 1 if mutated else base_graph.version
    evolved_graph = WorkflowGraph(
        version=new_version,
        name=base_graph.name,
        description=base_graph.description,
        template_id=base_graph.template_id,
        parallel_reviewers=base_graph.parallel_reviewers or action == "grow",
        nodes=nodes,
        edges=edges,
    )
    return WorkflowEvolution(
        base=base_graph,
        evolved=evolved_graph,
        had_custom_workflow=had_custom_workflow,
        mutated=mutated,
        action=action,
        clean_streak=clean_streak,
        promoted_agents=promoted_agents,
        demoted_agents=demoted_agents,
        catching_reviewers=sorted(catching_agents),
        task_failed=task_failed,
        gate_failed_after_review=gate_failed_after_review,
    )


# Node fields a change summary names when they change (positions are layout, not behaviour).
_DESCRIBED_NODE_FIELDS: tuple[str, ...] = (
    "label",
    "kind",
    "agent",
    "output_contract",
    "prompt_template",
    "triggers",
    "model",
    "effort",
    "cli_agent",
    "skills",
    "thinking",
    "compact_before_start",
    "pause_after",
)


def _node_mode(node: WorkflowNode) -> str:
    return "optional, diff-triggered" if node.optional else "mandatory"


def _edge_text(edge: WorkflowEdge) -> str:
    return f"{edge.source} -> {edge.target} ({edge.condition.value})"


def describe_workflow_changes(before: WorkflowGraph, after: WorkflowGraph) -> list[str]:
    """Readable lines for what turning *before* into *after* changes, in a stable order:
    nodes in graph order (added or changed, then removed), then edges likewise, then
    graph-wide flags. Node positions and the graph version are not reported."""
    changes: list[str] = []
    old_nodes = {n.id: n for n in before.nodes}
    new_ids = {n.id for n in after.nodes}
    for node in after.nodes:
        old_node = old_nodes.get(node.id)
        if old_node is None:
            changes.append(f"add {node.kind.value} node {node.id} ({_node_mode(node)})")
            continue
        if old_node.enabled != node.enabled:
            changes.append(f"{'enable' if node.enabled else 'disable'} node {node.id}")
        if old_node.optional != node.optional:
            changes.append(f"make {node.id} {_node_mode(node)}")
        fields = [f for f in _DESCRIBED_NODE_FIELDS if getattr(old_node, f) != getattr(node, f)]
        if fields:
            changes.append(f"change {node.id}: {', '.join(fields)}")
    changes.extend(f"remove node {n.id}" for n in before.nodes if n.id not in new_ids)

    old_edges = {e.id: e for e in before.edges}
    new_edge_ids = {e.id for e in after.edges}
    for edge in after.edges:
        old_edge = old_edges.get(edge.id)
        if old_edge is None:
            changes.append(f"add edge {_edge_text(edge)}")
            continue
        if (old_edge.source, old_edge.target) != (edge.source, edge.target):
            changes.append(f"reroute edge {_edge_text(old_edge)} to {_edge_text(edge)}")
        elif old_edge.condition != edge.condition:
            changes.append(
                f"edge {edge.source} -> {edge.target}: "
                f"{old_edge.condition.value} -> {edge.condition.value}"
            )
        if old_edge.max_iterations != edge.max_iterations:
            changes.append(
                f"edge {_edge_text(edge)}: max_iterations "
                f"{old_edge.max_iterations} -> {edge.max_iterations}"
            )
        if old_edge.require_human != edge.require_human:
            changes.append(
                f"edge {_edge_text(edge)}: "
                f"{'require' if edge.require_human else 'no longer require'} human approval"
            )
    changes.extend(
        f"remove edge {_edge_text(e)}" for e in before.edges if e.id not in new_edge_ids
    )

    if before.parallel_reviewers != after.parallel_reviewers:
        changes.append(
            f"{'enable' if after.parallel_reviewers else 'disable'} parallel reviewers"
        )
    return changes


def _evolution_summary(evolution: WorkflowEvolution, task_id: str | None) -> str:
    """One readable sentence on why evolution changes the workflow."""
    if evolution.action == "grow":
        causes: list[str] = []
        if evolution.task_failed:
            causes.append(f"task {task_id} failed" if task_id else "the latest task failed")
        if evolution.gate_failed_after_review:
            causes.append("the gate failed after the review passed")
        if evolution.catching_reviewers:
            causes.append("grounded catches by " + ", ".join(evolution.catching_reviewers))
        text = "Grow: " + "; ".join(causes)
        if evolution.promoted_agents:
            text += ". Mandatory reviewers: " + ", ".join(evolution.promoted_agents)
        return text + "."
    if evolution.action == "shrink":
        return (
            f"Shrink: {evolution.clean_streak} clean done tasks in a row without a grounded "
            f"catch by {', '.join(evolution.demoted_agents)}, so they become optional "
            "(diff-triggered)."
        )
    return f"Rewire reviewers after {evolution.clean_streak} clean done tasks in a row."


def _evolution_changes(evolution: WorkflowEvolution) -> list[str]:
    changes = describe_workflow_changes(evolution.base, evolution.evolved)
    if not evolution.had_custom_workflow:
        changes.insert(0, "pin the derived pipeline as this project's own workflow")
    return changes


def _reason(given: str | None, default: str) -> str:
    """The operator's reason verbatim when given, else *default*."""
    return given if given and given.strip() else default


def _workflow_state(config: ConclaveConfig) -> dict[str, Any]:
    return {
        "workflow": _active_graph(config).model_dump(mode="json"),
        "is_custom": config.workflow is not None,
    }


async def _emit_workflow_changed(
    db: Database, bus: EventBus, project_id: str, state: dict[str, Any]
) -> None:
    await bus.emit(type=EventType.workflow_updated, project_id=project_id, payload=state)
    project = await repo.get_project(db, project_id)
    if project is not None:
        await bus.emit(
            type=EventType.project_updated,
            project_id=project_id,
            payload={"project": project.model_dump(mode="json")},
        )


async def _compare_and_apply(
    db: Database,
    project_id: str,
    graph: dict[str, Any],
    *,
    base: dict[str, Any],
    source: WorkflowVersionSource,
    reason: str,
    approves_proposal: str | None = None,
    decision_reason: str | None = None,
) -> tuple[WorkflowVersion | None, ConclaveConfig]:
    """Apply *graph* only while the live workflow still equals *base*, the graph the change
    was computed from; otherwise raise :class:`WorkflowConflictError` and change nothing."""
    project = await repo.get_project(db, project_id)
    if project is None:
        raise ValueError(f"project not found: {project_id}")
    live = _active_graph(await resolve_project_config(db, project)).model_dump(mode="json")
    if live != base:
        raise WorkflowConflictError(
            "the project's workflow changed since this change was computed; run workflow "
            "evolution again (conclave_evolve_workflow or POST "
            f"/api/projects/{project_id}/workflow/evolve) for an up-to-date proposal"
        )
    resolved, version = await save_project_workflow(
        db,
        project,
        graph,
        source=source,
        reason=reason,
        approves_proposal=approves_proposal,
        decision_reason=decision_reason,
    )
    return version, resolved


async def evolve_project_workflow(
    db: Database,
    bus: EventBus,
    project_id: str,
    *,
    task_id: str | None = None,
    gate_failed_after_review: bool = False,
    task_failed: bool = False,
    automatic: bool = False,
    apply: bool = False,
    reason: str | None = None,
) -> dict[str, Any]:
    """Evolve *project_id*'s workflow as ``experimental.workflow_evolution`` says.

    *automatic* marks the post-task hook, which in ``off`` mode returns before computing
    anything. Otherwise the change is computed (:func:`compute_workflow_evolution`) and, when
    it would change the graph:

    * *apply* (an operator decision) or ``auto`` mode applies it as a workflow version with
      source ``operator`` / ``evolver`` and emits ``workflow.evolved`` + ``workflow.updated``;
    * ``suggest`` stores a pending proposal, unless an identical one is pending already, and
      emits ``workflow.evolution_proposed`` when it stored a new one;
    * a manual call in ``off`` mode only returns the preview.

    The result keeps the computed ``workflow``; ``evolved`` is true when the live workflow
    changed, ``proposal`` / ``version`` hold what was stored. *reason* is recorded on the
    version, or kept as the proposal's ``note``. A conflicting concurrent edit raises
    :class:`WorkflowConflictError`.
    """
    project = await repo.get_project(db, project_id)
    if project is None:
        raise ValueError(f"project not found: {project_id}")
    config = await resolve_project_config(db, project)
    mode = config.experimental.workflow_evolution
    result: dict[str, Any] = {
        "evolved": False,
        "mode": mode,
        "project_id": project_id,
        "task_id": task_id,
        "action": "none",
        "proposal": None,
        "version": None,
    }
    if automatic and mode == "off":
        return result

    evolution = await compute_workflow_evolution(
        db,
        project_id,
        config,
        task_id=task_id,
        gate_failed_after_review=gate_failed_after_review,
        task_failed=task_failed,
    )
    wf_dump = evolution.evolved.model_dump(mode="json")
    result.update(
        action=evolution.action,
        clean_streak=evolution.clean_streak,
        promoted_agents=evolution.promoted_agents,
        demoted_agents=evolution.demoted_agents,
        workflow=wf_dump,
    )
    if not evolution.mutated:
        return result
    summary = _evolution_summary(evolution, task_id)

    if apply or mode == "auto":
        version, _ = await _compare_and_apply(
            db,
            project_id,
            wf_dump,
            base=evolution.base.model_dump(mode="json"),
            source=WorkflowVersionSource.operator if apply else WorkflowVersionSource.evolver,
            reason=_reason(reason, summary),
        )
        evolution_payload: dict[str, Any] = {
            "project_id": project_id,
            "task_id": task_id,
            "action": evolution.action,
            "clean_streak": evolution.clean_streak,
            "promoted_agents": evolution.promoted_agents,
            "demoted_agents": evolution.demoted_agents,
            "workflow": wf_dump,
            "version_id": version.id if version is not None else None,
        }
        await bus.emit(
            type=EventType.workflow_evolved,
            project_id=project_id,
            task_id=task_id,
            payload=evolution_payload,
        )
        await bus.emit(
            type=EventType.workflow_updated,
            project_id=project_id,
            task_id=task_id,
            payload={"workflow": wf_dump, "is_custom": True},
        )
        result.update(
            evolved=version is not None,
            version=version.model_dump(mode="json") if version is not None else None,
        )
        return result

    if mode == "suggest":
        proposal_reason: dict[str, Any] = {
            "action": evolution.action,
            "summary": summary,
            "changes": _evolution_changes(evolution),
            "promoted_agents": evolution.promoted_agents,
            "demoted_agents": evolution.demoted_agents,
            "task_id": task_id,
            "catching_reviewers": evolution.catching_reviewers,
            "task_failed": evolution.task_failed,
            "gate_failed_after_review": evolution.gate_failed_after_review,
            "clean_streak": evolution.clean_streak,
        }
        if reason and reason.strip():
            proposal_reason["note"] = reason
        proposal, created = await repo.add_workflow_proposal(
            db,
            project_id=project_id,
            reason=proposal_reason,
            proposed_graph=wf_dump,
            base_graph=evolution.base.model_dump(mode="json"),
        )
        if created:
            await bus.emit(
                type=EventType.workflow_evolution_proposed,
                project_id=project_id,
                task_id=task_id,
                payload={
                    "proposal_id": proposal.id,
                    "action": evolution.action,
                    "summary": summary,
                    "changes": proposal_reason["changes"],
                },
            )
        result["proposal"] = proposal.model_dump(mode="json")
    return result


async def revert_workflow(
    db: Database,
    bus: EventBus,
    project: Project,
    version: WorkflowVersion,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    """Restore the graph *version* recorded after its change (``None``: the derived pipeline)
    as a new ``revert`` version; history is never deleted. Restoring the graph the project
    already has records nothing and returns ``changed: false``."""
    resolved, new_version = await save_project_workflow(
        db,
        project,
        version.graph_after,
        source=WorkflowVersionSource.revert,
        reason=_reason(reason, f"revert to version {version.id}"),
    )
    state = _workflow_state(resolved)
    if new_version is not None:
        await _emit_workflow_changed(db, bus, project.id, state)
    return {
        "changed": new_version is not None,
        "reverted_to": version.id,
        "version": new_version.model_dump(mode="json") if new_version is not None else None,
        **state,
    }


async def decide_workflow_proposal(
    db: Database,
    bus: EventBus,
    project: Project,
    proposal: WorkflowProposal,
    *,
    approve: bool,
    reason: str | None = None,
) -> dict[str, Any]:
    """Approve or reject a pending proposal; *reason* is recorded as the decision reason.

    Approving applies the proposed graph as a ``proposal`` version and marks the proposal
    approved in the same transaction, only while the live workflow still equals the graph the
    proposal was computed from. Rejecting leaves the workflow as it is. A proposal that is no
    longer pending, or a live workflow that changed, raises :class:`WorkflowConflictError`.
    """
    if proposal.state is not WorkflowProposalState.pending:
        raise WorkflowConflictError(
            f"workflow proposal {proposal.id} is already {proposal.state.value}"
        )
    decision_reason = reason if reason and reason.strip() else None
    if not approve:
        if not await repo.reject_workflow_proposal(
            db, proposal.id, decision_reason=decision_reason
        ):
            raise WorkflowConflictError(f"workflow proposal {proposal.id} is no longer pending")
        rejected = await repo.get_workflow_proposal(db, proposal.id)
        return {
            "decision": "reject",
            "proposal": rejected.model_dump(mode="json") if rejected is not None else None,
            "version": None,
        }

    summary = str(proposal.reason.get("summary") or "")
    version, resolved = await _compare_and_apply(
        db,
        project.id,
        proposal.proposed_graph,
        base=proposal.base_graph,
        source=WorkflowVersionSource.proposal,
        reason=_reason(reason, f"approved proposal {proposal.id}: {summary}".rstrip(": ")),
        approves_proposal=proposal.id,
        decision_reason=decision_reason,
    )
    state = _workflow_state(resolved)
    await _emit_workflow_changed(db, bus, project.id, state)
    approved = await repo.get_workflow_proposal(db, proposal.id)
    return {
        "decision": "approve",
        "proposal": approved.model_dump(mode="json") if approved is not None else None,
        "version": version.model_dump(mode="json") if version is not None else None,
        **state,
    }
