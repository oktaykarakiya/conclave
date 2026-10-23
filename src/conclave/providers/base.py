"""Provider seam: a thin interface over an agent engine.

The only v1 implementation is :class:`conclave.providers.claude_cli.ClaudeCliProvider`
(Conclave is "Claude Code based"). The seam exists so a future provider slots in
without rewiring the orchestrator — it is deliberately minimal (no dead branches).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from .profiles import ResolvedProfile

OnChunk = Callable[[str], Awaitable[None]]


class AgentResult(BaseModel):
    """Outcome of a single agent dispatch."""

    ok: bool
    text: str = ""
    model_reported: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    exit_code: int | None = None
    error: str | None = None
    session_id: str | None = None
    reasoning_tokens: int | None = None
    tool_calls_count: int = 0
    tool_breakdown: dict[str, int] = Field(default_factory=dict)
    messages: list[dict[str, object]] = Field(default_factory=list)


class Provider(Protocol):
    async def run_agent(
        self,
        *,
        profile: ResolvedProfile,
        prompt: str,
        timeout_seconds: int,
        cwd: Path | None = None,
        on_chunk: OnChunk | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AgentResult: ...
