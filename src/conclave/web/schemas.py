"""Request/response models for the web API."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..config import WorkflowGraph
from ..db import ProjectMode, StoreItemKind


class ProjectCreate(BaseModel):
    name: str
    path: str
    default_branch: str = "main"
    workflow_template: str | None = None
    config_preset: str | None = None
    max_parallel_tasks: int | None = Field(default=None, ge=1, le=16)


class ProjectModeUpdate(BaseModel):
    mode: ProjectMode


class ConfigPatch(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)
    default_branch: str | None = None
    name: str | None = None
    apply_to_all_projects: bool = True


class WorkflowUpdate(BaseModel):
    workflow: WorkflowGraph | None = None
    template_id: str | None = None


class TaskCreate(BaseModel):
    request: str
    title: str = ""
    use_planner: bool | None = None
    auto_approve: bool = False
    source_branch: str | None = None


class SteerInput(BaseModel):
    """Operator guidance injected into an in-progress task's next dispatch."""

    message: str = Field(min_length=1, description="Guidance to inject into the next dispatch")
    node_id: str | None = None
    edge_id: str | None = None


class NodeCompleteInput(BaseModel):
    """Complete, approve, or override a workflow node or HITL connector."""

    status: str = Field(default="completed")
    message: str | None = None
    verdict: str | None = None
    output: str | None = None
    edge_id: str | None = None
    attempt: int | None = None


class HitlToggleInput(BaseModel):
    """Arm or disarm a Human-in-the-Loop pause on a node or edge for a task."""

    target_id: str = Field(min_length=1)
    enabled: bool = True


class QuarantineInput(BaseModel):
    pattern: str
    reason: str
    until: str  # YYYY-MM-DD

    @field_validator("until")
    @classmethod
    def _validate_until_date(cls, v: str) -> str:
        """Reject non-ISO dates so the stored string always parses as YYYY-MM-DD."""
        try:
            date.fromisoformat(v)
        except ValueError as exc:
            raise ValueError(
                f"until must be a valid YYYY-MM-DD date, got: {v!r}"
            ) from exc
        return v


class AgentUpsert(BaseModel):
    role: str = "conditional"
    persona_md: str
    project_id: str | None = None


class PlanningSessionCreate(BaseModel):
    title: str = ""
    prompt: str
    max_rounds: int = 5


class PlanningMessageInput(BaseModel):
    content: str


class StoreItemUpsert(BaseModel):
    kind: StoreItemKind
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    payload: dict[str, Any]


class StoreInstallInput(BaseModel):
    item_id: str | None = None
    kind: StoreItemKind | None = None
    name: str | None = None


class SwarmUnitInput(BaseModel):
    title: str = ""
    request: str = Field(min_length=1)
    use_planner: bool | None = False


class SwarmCreateInput(BaseModel):
    units: list[SwarmUnitInput] = Field(default_factory=list)
    prompt: str | None = None
    concurrency: int = Field(default=4, ge=1, le=16)
    auto_approve: bool = True


class PaginationParams(BaseModel):
    """Shared pagination model for list endpoints (DoS hardening — WEB-1).

    Every unbounded list endpoint accepts these query params; the repo layer
    translates them into ``LIMIT ? OFFSET ?`` clauses. When omitted, the
    defaults cap the response so a single request can never balloon memory.
    """

    limit: int = Field(50, ge=1, le=500, description="Max items per page")
    offset: int = Field(0, ge=0, description="Items to skip")
