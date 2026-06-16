"""Domain row models for the persistence layer.

These are distinct from :mod:`conclave.config` models — they represent stored
records (projects, tasks, events, …). JSON columns are parsed on load via
``from_row``.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# A single corrupt row must never 500 a list endpoint or stall the worker's claim loop,
# so from_row decoding degrades gracefully and records the corruption here instead of
# raising it up the stack.
logger = logging.getLogger("conclave.db.models")


class ProjectMode(StrEnum):
    task_queue = "task_queue"
    autonomous_bug_fixer = "autonomous_bug_fixer"


class TaskState(StrEnum):
    inbox = "inbox"  # created, awaiting approval
    approved = "approved"  # ready to run
    in_progress = "in_progress"  # claimed by a worker
    done = "done"
    failed = "failed"
    cancelled = "cancelled"
    blocked = "blocked"  # parent task failed, cannot proceed


class TaskOrigin(StrEnum):
    operator = "operator"
    bug_fixer = "bug_fixer"


def _loads(value: Any, fallback: Any) -> Any:
    """Decode a JSON column, tolerating corruption.

    A malformed ``*_json`` cell falls back to its caller-supplied default (and is logged)
    rather than raising JSONDecodeError out of ``from_row`` — one bad row must not take
    down the whole task/project list endpoint or crash the worker.
    """
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        logger.warning("corrupt JSON column, falling back to %r (raw=%r)", fallback, value)
        return fallback


def _enum[E: StrEnum](enum_cls: type[E], value: Any, default: E) -> E:
    """Parse an enum column, tolerating unknown values.

    The mirror of :func:`_loads` for enum columns: an unrecognised stored string (newer
    schema, or genuine corruption) falls back to ``default`` instead of raising ValueError.
    ``default`` is each field's already-declared safe value — notably ``TaskState.inbox``,
    a non-claimable state, so a corrupt task can never be picked up and run.
    """
    try:
        return enum_cls(value)
    except ValueError:
        logger.warning(
            "unknown %s value %r, falling back to %s", enum_cls.__name__, value, default.value
        )
        return default


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    path: str
    default_branch: str
    mode: ProjectMode = ProjectMode.task_queue
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> Project:
        return cls(
            id=row["id"],
            name=row["name"],
            path=row["path"],
            default_branch=row["default_branch"],
            mode=_enum(ProjectMode, row["mode"], ProjectMode.task_queue),
            config=_loads(row["config_json"], {}),
            created_at=row["created_at"],
        )


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    title: str = ""
    request: str
    level: int | None = None
    state: TaskState = TaskState.inbox
    use_planner: bool | None = None
    plan: dict[str, Any] | None = None
    branch: str | None = None
    result_summary: str | None = None
    origin: TaskOrigin = TaskOrigin.operator
    parent_task_id: str | None = None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> Task:
        up = row["use_planner"]
        ptid = row["parent_task_id"] if "parent_task_id" in row.keys() else None
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            request=row["request"],
            level=row["level"],
            state=_enum(TaskState, row["state"], TaskState.inbox),
            use_planner=None if up is None else bool(up),
            plan=_loads(row["plan_json"], None),
            branch=row["branch"],
            result_summary=row["result_summary"],
            origin=_enum(TaskOrigin, row["origin"], TaskOrigin.operator),
            parent_task_id=ptid,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class EventRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    project_id: str | None = None
    task_id: str | None = None
    planning_session_id: str | None = None
    agent: str | None = None
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: str

    @classmethod
    def from_row(cls, row: Any) -> EventRow:
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            planning_session_id=(
                row["planning_session_id"] if "planning_session_id" in row.keys() else None
            ),
            agent=row["agent"],
            type=row["type"],
            payload=_loads(row["payload_json"], {}),
            ts=row["ts"],
        )


class VerdictRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    task_id: str
    attempt: int
    agent: str
    verdict: str
    reason: str = ""
    source: str = "none"
    grounded_count: int = 0
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> VerdictRow:
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            attempt=row["attempt"],
            agent=row["agent"],
            verdict=row["verdict"],
            reason=row["reason"],
            source=row["source"],
            grounded_count=row["grounded_count"],
            evidence=_loads(row["evidence_json"], []),
            created_at=row["created_at"],
        )


class UsageRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str | None = None
    task_id: str | None = None
    agent: str
    model_reported: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    ts: str

    @classmethod
    def from_row(cls, row: Any) -> UsageRow:
        keys = row.keys()
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            agent=row["agent"],
            model_reported=row["model_reported"],
            cost_usd=row["cost_usd"],
            num_turns=row["num_turns"],
            input_tokens=row["input_tokens"] if "input_tokens" in keys else None,
            output_tokens=row["output_tokens"] if "output_tokens" in keys else None,
            cache_read_tokens=row["cache_read_tokens"] if "cache_read_tokens" in keys else None,
            cache_creation_tokens=(
                row["cache_creation_tokens"] if "cache_creation_tokens" in keys else None
            ),
            ts=row["ts"],
        )


class AgentPersona(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str | None = None
    name: str
    role: str
    persona_md: str
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> AgentPersona:
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            role=row["role"],
            persona_md=row["persona_md"],
            created_at=row["created_at"],
        )


class Baseline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    sha: str
    output: str
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> Baseline:
        return cls(
            project_id=row["project_id"],
            sha=row["sha"],
            output=row["output"],
            created_at=row["created_at"],
        )


class QuarantineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    pattern: str
    reason: str
    until: str  # YYYY-MM-DD
    created_by: str = "operator"
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> QuarantineEntry:
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            pattern=row["pattern"],
            reason=row["reason"],
            until=row["until"],
            created_by=row["created_by"],
            created_at=row["created_at"],
        )
