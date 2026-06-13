"""Core orchestration engine (ported from team-ai)."""

from __future__ import annotations

from .baseline import build_baseline_preamble, trim_output
from .memory import AttemptMemory
from .pipeline import get_agent_pipeline
from .verdict import ParsedVerdict, check_grounding, parse_verdict

__all__ = [
    "AttemptMemory",
    "ParsedVerdict",
    "build_baseline_preamble",
    "check_grounding",
    "get_agent_pipeline",
    "parse_verdict",
    "trim_output",
]
