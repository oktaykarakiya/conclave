"""Provider seam: the opencode (default) and claude CLI engines, dispatch profile, effort,
and the usage-limit wait shared by every dispatch."""

from __future__ import annotations

from .base import AgentResult, OnChunk, Provider, QuotaExhausted
from .claude_cli import ClaudeCliProvider
from .effort import EffortChoice
from .opencode_cli import OpenCodeCliProvider
from .profiles import Invocation, ResolvedProfile, build_invocation
from .quota import (
    CONTINUATION_PROMPT,
    QuotaGate,
    QuotaListener,
    QuotaWait,
    WaitTracker,
    dispatch_with_quota,
    quota_key,
)
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
    "CONTINUATION_PROMPT",
    "AgentResult",
    "ClaudeCliProvider",
    "EffortChoice",
    "Engine",
    "EngineRouter",
    "Invocation",
    "OnChunk",
    "OpenCodeCliProvider",
    "Provider",
    "QuotaExhausted",
    "QuotaGate",
    "QuotaListener",
    "QuotaWait",
    "ResolvedProfile",
    "WaitTracker",
    "build_invocation",
    "dispatch_with_quota",
    "engine_status",
    "parse_engine",
    "quota_key",
    "resolve_dispatch_effort",
    "resolve_engine",
]
