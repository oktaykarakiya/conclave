"""Provider seam: a thin interface over an agent engine.

The only v1 implementation is :class:`conclave.providers.claude_cli.ClaudeCliProvider`
(Conclave is "Claude Code based"). The seam exists so a future provider slots in
without rewiring the orchestrator — it is deliberately minimal (no dead branches).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from .profiles import ResolvedProfile

OnChunk = Callable[[str], Awaitable[None]]


class AgentResult(BaseModel):
    """Outcome of a single agent dispatch."""

    ok: bool
    text: str = ""
    model_reported: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    exit_code: int | None = None
    error: str | None = None


class Provider(Protocol):
    async def run_agent(
        self,
        *,
        profile: ResolvedProfile,
        prompt: str,
        timeout_seconds: int,
        cwd: Path | None = None,
        on_chunk: OnChunk | None = None,
    ) -> AgentResult: ...


class ProfileTestResult(BaseModel):
    """Result of the per-profile "Test" button."""

    ok: bool
    model_reported: str | None = None
    latency_ms: int | None = None
    cost_usd: float | None = None
    error: str | None = None


async def probe_profile(
    provider: Provider,
    profile: ResolvedProfile,
    *,
    timeout_seconds: int = 120,
) -> ProfileTestResult:
    """Smoke-test a profile end-to-end (base URL, auth, model, effort) via a trivial dispatch."""
    start = time.monotonic()
    result = await provider.run_agent(
        profile=profile,
        prompt="Reply with exactly the single word: READY",
        timeout_seconds=timeout_seconds,
    )
    latency_ms = int((time.monotonic() - start) * 1000)
    return ProfileTestResult(
        ok=result.ok and result.error is None,
        model_reported=result.model_reported,
        latency_ms=latency_ms,
        cost_usd=result.cost_usd,
        error=result.error,
    )
