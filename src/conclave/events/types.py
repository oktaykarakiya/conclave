"""Typed event vocabulary.

Events are the single stream behind the live log, the UI feed, notifications, and
the audit trail. ``EventType`` is a ``StrEnum`` so values pass anywhere a ``str`` is
expected, but :meth:`EventBus.emit` also accepts raw strings for forward flexibility.
"""

from __future__ import annotations

from enum import StrEnum


class EventType(StrEnum):
    # task lifecycle
    task_created = "task.created"
    task_approved = "task.approved"
    task_started = "task.started"
    task_cancelled = "task.cancelled"
    task_committed = "task.committed"
    task_merged = "task.merged"
    task_deleted = "task.deleted"
    task_done = "task.done"
    task_failed = "task.failed"
    # planning
    plan_level = "plan.level_selected"
    plan_artifact = "plan.artifact"
    # agents
    agent_dispatched = "agent.dispatched"
    agent_output = "agent.output_chunk"
    agent_result = "agent.result"
    verdict = "agent.verdict"
    grounding_warning = "grounding.warning"
    pipeline_derived = "pipeline.derived"
    # attempts / gate
    attempt_started = "attempt.started"
    attempt_failed = "attempt.failed"
    baseline_snapshot = "baseline.snapshot"
    # repo intelligence
    onboarding_started = "onboarding.started"
    onboarding_complete = "onboarding.complete"
    onboarding_ai_started = "onboarding.ai_started"
    onboarding_ai_complete = "onboarding.ai_complete"
    # bug fixer (Phase 2)
    bug_discovered = "bug.discovered"
    bug_reproduced = "bug.reproduced"
    bug_dismissed = "bug.dismissed"
    bug_declined = "bug.declined"
    consensus_round = "consensus.round"
    # agent-ception planning sessions
    planning_session_created = "planning.session_created"
    planning_session_started = "planning.session_started"
    planning_agent_turn = "planning.agent_turn"
    planning_human_interject = "planning.human_interject"
    planning_task_proposed = "planning.task_proposed"
    planning_task_refined = "planning.task_refined"
    planning_session_stable = "planning.session_stable"
    planning_tasks_approved = "planning.tasks_approved"
    planning_session_completed = "planning.session_completed"
    planning_session_cancelled = "planning.session_cancelled"
    planning_error = "planning.error"
    # misc
    postmortem_draft = "postmortem.draft"
    usage_recorded = "usage.recorded"
    operator_steer = "operator.steer"
    log = "log"
