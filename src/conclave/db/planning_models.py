"""Domain row models for the planning-session layer.

These mirror the pattern in :mod:`conclave.db.models` — Pydantic ``BaseModel``
with ``from_row()`` classmethods that decode JSON columns and parse enums.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Mirror conclave.db.models: a corrupt planning row degrades to safe defaults (and is
# logged) rather than raising out of from_row and 500-ing the planning endpoints.
logger = logging.getLogger("conclave.db.planning_models")


class PlanningSessionStatus(StrEnum):
    active = "active"
    stable = "stable"       # agents agreed, awaiting human approval
    completed = "completed"  # human approved, tasks created
    cancelled = "cancelled"


class PlanningNodeStatus(StrEnum):
    proposed = "proposed"
    refined = "refined"
    approved = "approved"


def _loads(value: Any, fallback: Any) -> Any:
    """Decode a JSON column, tolerating corruption (mirror of :func:`conclave.db.models._loads`).

    A malformed ``*_json`` cell falls back to its caller-supplied default (and is logged)
    rather than raising JSONDecodeError out of ``from_row``.
    """
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        logger.warning("corrupt JSON column, falling back to %r (raw=%r)", fallback, value)
        return fallback


def _enum[E: StrEnum](enum_cls: type[E], value: Any, default: E) -> E:
    """Parse an enum column, tolerating unknown values (mirror of :func:`conclave.db.models._enum`).

    An unrecognised stored status falls back to ``default`` (the field's declared safe
    value) instead of raising ValueError out of ``from_row``.
    """
    try:
        return enum_cls(value)
    except ValueError:
        logger.warning(
            "unknown %s value %r, falling back to %s", enum_cls.__name__, value, default.value
        )
        return default


class PlanningSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    title: str = ""
    prompt: str
    status: PlanningSessionStatus = PlanningSessionStatus.active
    turn_number: int = 0
    max_rounds: int = 5
    created_at: str
    completed_at: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> PlanningSession:
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            prompt=row["prompt"],
            status=_enum(PlanningSessionStatus, row["status"], PlanningSessionStatus.active),
            turn_number=row["turn_number"],
            max_rounds=row["max_rounds"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
        )


class PlanningMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    session_id: str
    agent: str
    role: str = "agent"  # "agent" | "human"
    content: str
    turn_number: int = 0
    parent_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> PlanningMessage:
        return cls(
            id=row["id"],
            session_id=row["session_id"],
            agent=row["agent"],
            role=row["role"],
            content=row["content"],
            turn_number=row["turn_number"],
            parent_id=row["parent_id"],
            metadata=_loads(row["metadata_json"], {}),
            created_at=row["created_at"],
        )


class PlanningTaskNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    session_id: str
    parent_id: str | None = None
    title: str
    description: str = ""
    status: PlanningNodeStatus = PlanningNodeStatus.proposed
    level: int = 0
    sort_order: int = 0
    task_id: str | None = None  # set after approval when real Task is created
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> PlanningTaskNode:
        return cls(
            id=row["id"],
            session_id=row["session_id"],
            parent_id=row["parent_id"],
            title=row["title"],
            description=row["description"],
            status=_enum(PlanningNodeStatus, row["status"], PlanningNodeStatus.proposed),
            level=row["level"],
            sort_order=row["sort_order"],
            task_id=row["task_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
