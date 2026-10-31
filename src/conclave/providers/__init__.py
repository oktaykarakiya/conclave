"""Provider seam: the opencode (default) and claude CLI engines + dispatch profile."""

from __future__ import annotations

from .base import AgentResult, OnChunk, Provider
from .claude_cli import ClaudeCliProvider
from .opencode_cli import OpenCodeCliProvider
from .profiles import Invocation, ResolvedProfile, build_invocation
from .router import (
    CLAUDE_CODE_PREFIX,
    Engine,
    EngineRouter,
    engine_status,
    parse_engine,
    resolve_engine,
)

__all__ = [
    "CLAUDE_CODE_PREFIX",
    "AgentResult",
    "ClaudeCliProvider",
    "Engine",
    "EngineRouter",
    "Invocation",
    "OnChunk",
    "OpenCodeCliProvider",
    "Provider",
    "ResolvedProfile",
    "build_invocation",
    "engine_status",
    "parse_engine",
    "resolve_engine",
]
