"""Per-dispatch engine routing: opencode (default) or the Claude Code CLI.

The engine is picked per agent from its ``model`` string, so one project can mix engines
(e.g. a DeepSeek developer reviewed by Claude):

- ``claude-code/<model>`` → the ``claude`` CLI with ``--model <model>``; ``claude-code/default``
  (or a bare ``claude-code``) passes no model, i.e. the logged-in Claude Code default;
- anything else → the daemon's default engine (``CONCLAVE_ENGINE``, opencode unless set).

When the default engine is ``claude``, an un-prefixed model is still honoured where it can
be: bare Claude names (``opus``, ``claude-sonnet-4-5``) and ``anthropic/<model>`` are passed
through, while other opencode-format ``provider/model`` strings fall back to the Claude Code
default rather than failing the dispatch.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from enum import StrEnum
from pathlib import Path

from ..config import ArgMode
from .base import AgentResult, OnChunk
from .claude_cli import ClaudeCliProvider
from .opencode_cli import OpenCodeCliProvider
from .profiles import ResolvedProfile

CLAUDE_CODE_PREFIX = "claude-code/"

# The claude binary; overridable for non-PATH installs (mirrors CONCLAVE_OPENCODE_BIN).
_CLAUDE_BIN = os.environ.get("CONCLAVE_CLAUDE_BIN", "claude")


class Engine(StrEnum):
    opencode = "opencode"
    claude = "claude"


def parse_engine(value: str | None) -> Engine:
    """Parse a ``CONCLAVE_ENGINE`` value. Empty → opencode; unknown → ``ValueError``."""
    raw = (value or "").strip().lower()
    if raw in ("", "opencode"):
        return Engine.opencode
    if raw in ("claude", "claude-code", "claude_code"):
        return Engine.claude
    raise ValueError(f"unknown engine {value!r} (expected 'opencode' or 'claude')")


def resolve_engine(model: str | None, default: Engine) -> tuple[Engine, str | None]:
    """Return ``(engine, model_for_that_engine)`` for an agent's configured model (pure)."""
    if model is not None and (model == "claude-code" or model.startswith(CLAUDE_CODE_PREFIX)):
        name = model.removeprefix("claude-code").removeprefix("/").strip()
        return Engine.claude, (name if name and name != "default" else None)
    if default is Engine.opencode:
        return Engine.opencode, model
    if not model:
        return Engine.claude, None
    if model.startswith("anthropic/"):
        return Engine.claude, model.removeprefix("anthropic/")
    # An opencode-only "provider/model" means nothing to the claude CLI: use its default.
    return Engine.claude, (None if "/" in model else model)


class EngineRouter:
    """A :class:`~conclave.providers.base.Provider` that delegates each dispatch to the
    opencode or Claude Code provider according to :func:`resolve_engine`."""

    def __init__(
        self,
        default: Engine = Engine.opencode,
        *,
        opencode: OpenCodeCliProvider | None = None,
        claude: ClaudeCliProvider | None = None,
        claude_bin: str | None = None,
    ) -> None:
        self.default = default
        self._opencode = opencode or OpenCodeCliProvider()
        self._claude = claude or ClaudeCliProvider()
        self._claude_bin = claude_bin or _CLAUDE_BIN

    async def run_agent(
        self,
        *,
        profile: ResolvedProfile,
        prompt: str,
        timeout_seconds: int,
        cwd: Path | None = None,
        on_chunk: OnChunk | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AgentResult:
        engine, model = resolve_engine(profile.model, self.default)
        if engine is Engine.opencode:
            return await self._opencode.run_agent(
                profile=profile,
                prompt=prompt,
                timeout_seconds=timeout_seconds,
                cwd=cwd,
                on_chunk=on_chunk,
                cancel_event=cancel_event,
            )
        # Claude Code takes model/effort as flags and authenticates via its own login.
        claude_profile = replace(
            profile,
            name="claude-code",
            arg_mode=ArgMode.flag,
            model=model,
            cli=self._claude_bin,
        )
        result = await self._claude.run_agent(
            profile=claude_profile,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            cwd=cwd,
            on_chunk=on_chunk,
            cancel_event=cancel_event,
        )
        if result.model_reported is None:
            result.model_reported = f"{CLAUDE_CODE_PREFIX}{model or 'default'}"
        return result
