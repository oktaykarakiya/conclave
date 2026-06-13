"""The ``claude`` CLI provider — a port of team-ai's dispatch with streaming.

Spawns the CLI under the profile's composed args + environment, pipes the prompt via
stdin (avoiding arg-length limits), optionally streams stdout chunks to ``on_chunk``,
and parses the JSON envelope for the result text, reported model, cost, and turns.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from pathlib import Path

from .base import AgentResult, OnChunk
from .profiles import ResolvedProfile, build_invocation

# Mirrors team-ai: even on a non-zero exit, treat output that clearly contains an
# agent verdict / completion as a usable result rather than a hard failure.
_SUCCESS_HINT = re.compile(r"(?i)(verdict|task completed|i have)")


class ClaudeCliProvider:
    """Runs agents by invoking the ``claude`` CLI as a subprocess."""

    async def run_agent(
        self,
        *,
        profile: ResolvedProfile,
        prompt: str,
        timeout_seconds: int,
        cwd: Path | None = None,
        on_chunk: OnChunk | None = None,
    ) -> AgentResult:
        invocation = build_invocation(profile)
        env = {**os.environ, **invocation.env}

        try:
            proc = await asyncio.create_subprocess_exec(
                profile.cli,
                *invocation.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=str(cwd) if cwd is not None else None,
            )
        except FileNotFoundError:
            return AgentResult(ok=False, error=f"CLI not found: {profile.cli!r}")

        async def drive() -> str:
            assert proc.stdin is not None
            assert proc.stdout is not None
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            parts: list[str] = []
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                parts.append(text)
                if on_chunk is not None:
                    await on_chunk(text)
            await proc.wait()
            return "".join(parts)

        try:
            raw = await asyncio.wait_for(drive(), timeout=timeout_seconds)
        except TimeoutError:
            proc.kill()
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()
            return AgentResult(ok=False, error=f"timed out after {timeout_seconds}s")

        return _parse_envelope(raw, proc.returncode)


def _parse_envelope(raw: str, exit_code: int | None) -> AgentResult:
    text = raw
    model_reported: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        text = str(parsed.get("result", raw))
        model_usage = parsed.get("modelUsage") or {}
        if isinstance(model_usage, dict) and model_usage:
            model_reported = ", ".join(str(k) for k in model_usage)
        cost = parsed.get("total_cost_usd")
        if isinstance(cost, int | float):
            cost_usd = float(cost)
        turns = parsed.get("num_turns")
        if isinstance(turns, int):
            num_turns = turns

    ok = exit_code == 0 or bool(_SUCCESS_HINT.search(text))
    return AgentResult(
        ok=ok,
        text=text,
        model_reported=model_reported,
        cost_usd=cost_usd,
        num_turns=num_turns,
        exit_code=exit_code,
    )
