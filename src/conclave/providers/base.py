"""Provider seam: a thin interface over an agent engine.

The only v1 implementation is :class:`conclave.providers.claude_cli.ClaudeCliProvider`
(Conclave is "Claude Code based"). The seam exists so a future provider slots in
without rewiring the orchestrator — it is deliberately minimal (no dead branches).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from .profiles import ResolvedProfile

OnChunk = Callable[[str], Awaitable[None]]

# A resume of a session the engine no longer has: Claude Code's and opencode's wording.
UNKNOWN_SESSION_RE = re.compile(r"(?i)(no conversation found with session id|session not found)")


QuotaKind = Literal["rate_limit", "billing"]


@dataclass(frozen=True)
class QuotaExhausted:
    """The engine refused the request because a usage limit or the account balance ran out."""

    kind: QuotaKind
    resets_at: datetime | None = None  # timezone-aware UTC, when the engine reported it
    window: str | None = None  # the limit's window, e.g. five_hour, seven_day, seven_day_opus
    message: str = ""  # the engine's own text


def epoch_to_utc(value: object) -> datetime | None:
    """An epoch timestamp in seconds (a value above 1e11 is read as milliseconds) as UTC."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000 if value > 1e11 else value, UTC)
    except (OverflowError, OSError, ValueError):
        return None


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
    # The dispatch ended because its cancel event was set: never retry or continue after it.
    cancelled: bool = False
    session_id: str | None = None
    reasoning_tokens: int | None = None
    tool_calls_count: int = 0
    tool_breakdown: dict[str, int] = Field(default_factory=dict)
    messages: list[dict[str, object]] = Field(default_factory=list)
    # The engine refused the run for a usage limit or balance; such a result is never ok. The
    # shared dispatch path (providers.quota.dispatch_with_quota) waits and resumes instead of
    # handing it to a caller.
    quota: QuotaExhausted | None = None
    # The latest rate_limit_info a Claude Code run reported (status, windows, reset times).
    rate_limit_info: dict[str, Any] | None = None


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
