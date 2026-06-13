"""Domain row models for the persistence layer.

These are distinct from :mod:`conclave.config` models — they represent stored
records (projects, tasks, events, …). JSON columns are parsed on load via
``from_row``.
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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


class TaskOrigin(StrEnum):
    operator = "operator"
    bug_fixer = "bug_fixer"


def _loads(value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    return json.loads(value)


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
            mode=ProjectMode(row["mode"]),
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
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> Task:
        up = row["use_planner"]
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            request=row["request"],
            level=row["level"],
            state=TaskState(row["state"]),
            use_planner=None if up is None else bool(up),
            plan=_loads(row["plan_json"], None),
            branch=row["branch"],
            result_summary=row["result_summary"],
            origin=TaskOrigin(row["origin"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class EventRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    project_id: str | None = None
    task_id: str | None = None
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
    ts: str

    @classmethod
    def from_row(cls, row: Any) -> UsageRow:
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            agent=row["agent"],
            model_reported=row["model_reported"],
            cost_usd=row["cost_usd"],
            num_turns=row["num_turns"],
            ts=row["ts"],
        )


class EngineProfileRow(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    id: str
    project_id: str | None = None
    name: str
    arg_mode: str = "inherit"
    base_url: str | None = None
    model: str | None = None
    subagent_model: str | None = None
    effort: str | None = None
    auth_secret_id: str | None = None
    extra_env: dict[str, str] = Field(default_factory=dict)
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> EngineProfileRow:
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            arg_mode=row["arg_mode"],
            base_url=row["base_url"],
            model=row["model"],
            subagent_model=row["subagent_model"],
            effort=row["effort"],
            auth_secret_id=row["auth_secret_id"],
            extra_env=_loads(row["extra_env_json"], {}),
            created_at=row["created_at"],
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


class RepoKnowledgeRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    version: int
    sha: str | None = None
    manifest_fingerprint: str | None = None
    knowledge: dict[str, Any] = Field(default_factory=dict)
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> RepoKnowledgeRow:
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            version=row["version"],
            sha=row["sha"],
            manifest_fingerprint=row["manifest_fingerprint"],
            knowledge=_loads(row["knowledge_json"], {}),
            created_at=row["created_at"],
        )
