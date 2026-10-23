"""SQLite persistence layer for Conclave."""

from __future__ import annotations

from . import repositories
from .database import Database
from .models import (
    BUG_STATUS_TRANSITIONS,
    AgentPersona,
    Baseline,
    BugCandidate,
    BugStatus,
    CheckpointStatus,
    CoverageRegion,
    EventRow,
    IdempotencyRecord,
    IllegalBugTransition,
    InteractionLog,
    Project,
    ProjectMode,
    QuarantineEntry,
    StoreItem,
    StoreItemKind,
    Task,
    TaskCheckpoint,
    TaskOrigin,
    TaskState,
    UsageRow,
    VerdictRow,
)

__all__ = [
    "BUG_STATUS_TRANSITIONS",
    "AgentPersona",
    "Baseline",
    "BugCandidate",
    "BugStatus",
    "CheckpointStatus",
    "CoverageRegion",
    "Database",
    "EventRow",
    "IdempotencyRecord",
    "IllegalBugTransition",
    "InteractionLog",
    "Project",
    "ProjectMode",
    "QuarantineEntry",
    "StoreItem",
    "StoreItemKind",
    "Task",
    "TaskCheckpoint",
    "TaskOrigin",
    "TaskState",
    "UsageRow",
    "VerdictRow",
    "repositories",
]
