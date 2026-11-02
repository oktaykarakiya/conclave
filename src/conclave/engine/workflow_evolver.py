"""Longitudinal closed-loop workflow evolution engine.

Inspects historical reviewer verdicts, gate outcomes, and task completion streaks to
automatically grow or shrink a project's persisted :class:`WorkflowGraph` over time.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import (
    EdgeCondition,
    NodeKind,
    OutputContract,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    build_default_workflow,
    resolve_project_config,
    save_project_config,
)
from ..config.graph import _agent_label
from ..config.models import _DEFAULT_CONDITIONAL
from ..db import CheckpointStatus, Database, TaskState, VerdictRow
from ..db import repositories as repo
from ..events import EventBus, EventType

logger = logging.getLogger("conclave.engine.workflow_evolver")

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


async def evolve_project_workflow(
    db: Database,
    bus: EventBus,
    project_id: str,
    *,
    task_id: str | None = None,
    gate_failed_after_review: bool = False,
    task_failed: bool = False,
) -> dict[str, Any]:
    """Longitudinally evolve *project_id*'s workflow graph from outcomes and verdicts.

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
    project = await repo.get_project(db, project_id)
    if project is None:
        raise ValueError(f"project not found: {project_id}")

    config = await resolve_project_config(db, project)
    had_custom_workflow = config.workflow is not None
    base_graph = (
        config.workflow.model_copy(deep=True)
        if config.workflow is not None
        else build_default_workflow(
            config.agents,
            parallel_reviewers=config.execution.parallel_reviewers,
        )
    )
    nodes = list(base_graph.nodes)
    edges = list(base_graph.edges)

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

    if mutated:
        wf_dump = evolved_graph.model_dump(mode="json")
        await save_project_config(db, project, {"workflow": wf_dump})
        evolution_payload: dict[str, Any] = {
            "project_id": project_id,
            "task_id": task_id,
            "action": action,
            "clean_streak": clean_streak,
            "promoted_agents": promoted_agents,
            "demoted_agents": demoted_agents,
            "workflow": wf_dump,
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
        return {
            "evolved": True,
            **evolution_payload,
        }

    return {
        "evolved": False,
        "project_id": project_id,
        "task_id": task_id,
        "action": action,
        "clean_streak": clean_streak,
        "promoted_agents": promoted_agents,
        "demoted_agents": demoted_agents,
        "workflow": evolved_graph.model_dump(mode="json"),
    }
