"""Core orchestration engine (ported from team-ai)."""

from __future__ import annotations

from .baseline import build_baseline_preamble, trim_output
from .gate import GateResult, run_tests
from .gitio import run_git, run_shell
from .memory import AttemptMemory
from .orchestrator import Orchestrator
from .pipeline import get_agent_pipeline
from .runner import AgentRunner, assemble_prompt
from .verdict import ParsedVerdict, check_grounding, parse_verdict
from .worktree import WorktreeError, WorktreeManager

__all__ = [
    "AgentRunner",
    "AttemptMemory",
    "GateResult",
    "Orchestrator",
    "ParsedVerdict",
    "WorktreeError",
    "WorktreeManager",
    "assemble_prompt",
    "build_baseline_preamble",
    "check_grounding",
    "get_agent_pipeline",
    "parse_verdict",
    "run_git",
    "run_shell",
    "run_tests",
    "trim_output",
]
