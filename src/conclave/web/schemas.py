"""Request/response models for the web API."""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProjectCreate(BaseModel):
    name: str
    path: str
    default_branch: str = "main"


class ConfigPatch(BaseModel):
    config: dict[str, Any]


class TaskCreate(BaseModel):
    request: str
    title: str = ""
    use_planner: bool | None = None
    auto_approve: bool = False


class ProfileInput(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    name: str
    project_id: str | None = None
    arg_mode: str = "inherit"
    base_url: str | None = None
    model: str | None = None
    subagent_model: str | None = None
    effort: str | None = None
    auth_token: str | None = None  # write-only; stored as a secret, never returned
    extra_env: dict[str, str] = Field(default_factory=dict)


class SecretInput(BaseModel):
    name: str
    value: str


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


class PaginationParams(BaseModel):
    """Shared pagination model for list endpoints (DoS hardening — WEB-1).

    Every unbounded list endpoint accepts these query params; the repo layer
    translates them into ``LIMIT ? OFFSET ?`` clauses. When omitted, the
    defaults cap the response so a single request can never balloon memory.
    """

    limit: int = Field(50, ge=1, le=500, description="Max items per page")
    offset: int = Field(0, ge=0, description="Items to skip")
