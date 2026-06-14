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
    task_done = "task.done"
    task_failed = "task.failed"
    # planning
    plan_level = "plan.level_selected"
    plan_artifact = "plan.artifact"
    plan_decomposition_complete = "plan.decomposition_complete"
    plan_decomposition_fallback = "plan.decomposition_fallback"
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
    # bug fixer (Phase 2)
    bug_discovered = "bug.discovered"
    bug_reproduced = "bug.reproduced"
    bug_dismissed = "bug.dismissed"
    bug_declined = "bug.declined"
    consensus_round = "consensus.round"
    # misc
    postmortem_draft = "postmortem.draft"
    usage_recorded = "usage.recorded"
    operator_steer = "operator.steer"
    log = "log"
