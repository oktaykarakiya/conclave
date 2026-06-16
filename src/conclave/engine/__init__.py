"""Core orchestration engine (ported from team-ai)."""

from __future__ import annotations

from .baseline import build_baseline_preamble, trim_output
from .gate import GateResult, run_tests
from .gitio import run_git, run_shell
from .hunter import HunterCandidate, parse_hunter_candidate
from .memory import AttemptMemory
from .orchestrator import Orchestrator
from .pipeline import get_agent_pipeline
from .region_scheduler import select_hunt_region
from .runner import AgentRunner, assemble_prompt
from .verdict import ParsedVerdict, check_grounding, parse_verdict
from .worktree import WorktreeError, WorktreeManager

__all__ = [
    "AgentRunner",
    "AttemptMemory",
    "GateResult",
    "HunterCandidate",
    "Orchestrator",
    "ParsedVerdict",
    "WorktreeError",
    "WorktreeManager",
    "assemble_prompt",
    "build_baseline_preamble",
    "check_grounding",
    "get_agent_pipeline",
    "parse_hunter_candidate",
    "parse_verdict",
    "run_git",
    "run_shell",
    "run_tests",
    "select_hunt_region",
    "trim_output",
]
