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
    CoverageRegion,
    EngineProfileRow,
    EventRow,
    IllegalBugTransition,
    Project,
    ProjectMode,
    QuarantineEntry,
    RepoKnowledgeRow,
    Task,
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
    "CoverageRegion",
    "Database",
    "EngineProfileRow",
    "EventRow",
    "IllegalBugTransition",
    "Project",
    "ProjectMode",
    "QuarantineEntry",
    "RepoKnowledgeRow",
    "Task",
    "TaskOrigin",
    "TaskState",
    "UsageRow",
    "VerdictRow",
    "repositories",
]
