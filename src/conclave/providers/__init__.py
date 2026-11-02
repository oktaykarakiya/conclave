"""Provider seam: the opencode (default) and claude CLI engines, dispatch profile, effort."""

from __future__ import annotations

from .base import AgentResult, OnChunk, Provider
from .claude_cli import ClaudeCliProvider
from .effort import EffortChoice
from .opencode_cli import OpenCodeCliProvider
from .profiles import Invocation, ResolvedProfile, build_invocation
from .router import (
    CLAUDE_CODE_PREFIX,
    Engine,
    EngineRouter,
    engine_status,
    parse_engine,
    resolve_dispatch_effort,
    resolve_engine,
)

__all__ = [
    "CLAUDE_CODE_PREFIX",
    "AgentResult",
    "ClaudeCliProvider",
    "EffortChoice",
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
    "resolve_dispatch_effort",
    "resolve_engine",
]
