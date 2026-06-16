"""Provider seam: the opencode (default) and claude CLI engines + dispatch profile."""

from __future__ import annotations

from .base import AgentResult, OnChunk, Provider
from .claude_cli import ClaudeCliProvider
from .opencode_cli import OpenCodeCliProvider
from .profiles import Invocation, ResolvedProfile, build_invocation

__all__ = [
    "AgentResult",
    "ClaudeCliProvider",
    "Invocation",
    "OnChunk",
    "OpenCodeCliProvider",
    "Provider",
    "ResolvedProfile",
    "build_invocation",
]
