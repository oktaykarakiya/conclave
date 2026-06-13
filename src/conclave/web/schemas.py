"""Request/response models for the web API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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


class AgentUpsert(BaseModel):
    role: str = "conditional"
    persona_md: str
    project_id: str | None = None
