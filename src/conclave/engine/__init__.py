"""Core orchestration engine (ported from team-ai)."""

from __future__ import annotations

from .baseline import build_baseline_preamble, trim_output
from .bug_fixer import (
    BugFixerController,
    CycleOutcome,
    CycleResult,
    SessionBudget,
)
from .coverage_ingest import ingest_coverage
from .discovery import discover_bug
from .gate import GateResult, run_tests
from .gitio import run_git, run_shell
from .hunter import HunterCandidate, parse_hunter_candidate
from .memory import AttemptMemory
from .orchestrator import Orchestrator
from .pipeline import get_agent_pipeline
from .region_scheduler import select_hunt_region
from .repro import ReproTest, parse_repro_test, repro_pathguard
from .repro_gate import ReproOutcome, ReproResult, reproduce_bug, resolve_repro_test_path
from .runner import AgentRunner, assemble_prompt
from .test_integrity import (
    collect_modified_or_deleted_tests,
    is_test_path,
    modified_or_deleted_tests,
)
from .verdict import ParsedVerdict, check_grounding, parse_verdict
from .worktree import WorktreeError, WorktreeManager

__all__ = [
    "AgentRunner",
    "AttemptMemory",
    "BugFixerController",
    "CycleOutcome",
    "CycleResult",
    "GateResult",
    "HunterCandidate",
    "Orchestrator",
    "ParsedVerdict",
    "ReproOutcome",
    "ReproResult",
    "ReproTest",
    "SessionBudget",
    "WorktreeError",
    "WorktreeManager",
    "assemble_prompt",
    "build_baseline_preamble",
    "check_grounding",
    "collect_modified_or_deleted_tests",
    "discover_bug",
    "get_agent_pipeline",
    "ingest_coverage",
    "is_test_path",
    "modified_or_deleted_tests",
    "parse_hunter_candidate",
    "parse_repro_test",
    "parse_verdict",
    "repro_pathguard",
    "reproduce_bug",
    "resolve_repro_test_path",
    "run_git",
    "run_shell",
    "run_tests",
    "select_hunt_region",
    "trim_output",
]
