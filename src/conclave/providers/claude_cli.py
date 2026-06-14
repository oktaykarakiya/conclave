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
import signal
from pathlib import Path

from .base import AgentResult, OnChunk
from .profiles import ResolvedProfile, build_invocation

# Mirrors team-ai: even on a non-zero exit, treat output that clearly contains an
# agent verdict / completion as a usable result rather than a hard failure.
_SUCCESS_HINT = re.compile(r"(?i)(verdict|task completed|i have)")

# Provider-controlled env vars that must NOT leak from the parent process into
# inherit/flag-mode dispatches. In env-mode, build_invocation reintroduces them.
_PROVIDER_ENV_KEYS: frozenset[str] = frozenset({
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_EFFORT_LEVEL",
})


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
        # Strip provider-controlled env vars from the parent environment so they
        # never leak into inherit/flag-mode dispatches. invocation.env reintroduces
        # them for env-mode profiles.
        env = {
            k: v for k, v in os.environ.items() if k not in _PROVIDER_ENV_KEYS
        }
        env.update(invocation.env)

        try:
            proc = await asyncio.create_subprocess_exec(
                profile.cli,
                *invocation.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                cwd=str(cwd) if cwd is not None else None,
                start_new_session=True,
            )
        except FileNotFoundError:
            return AgentResult(ok=False, error=f"CLI not found: {profile.cli!r}")

        async def drive() -> str:
            assert proc.stdin is not None
            assert proc.stdout is not None
            # Capture narrowed types so mypy sees them inside the inner coroutines.
            stdin = proc.stdin
            stdout = proc.stdout

            # Shared mutable list — safe in CPython asyncio since only one coroutine
            # runs at a time (no true parallelism); await gather() joins before we
            # read it below.
            parts: list[str] = []

            async def _write_stdin() -> None:
                """Drain the prompt into stdin and close it to signal EOF."""
                stdin.write(prompt.encode("utf-8"))
                await stdin.drain()
                stdin.close()

            async def _read_stdout() -> None:
                """Consume all stdout chunks, appending to parts and streaming on_chunk."""
                while True:
                    chunk = await stdout.read(4096)
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    parts.append(text)
                    if on_chunk is not None:
                        await on_chunk(text)

            # Run stdin-writing and stdout-reading concurrently so a chatty child
            # that emits a large volume of stdout while stdin is still being
            # written cannot fill the OS pipe buffer and deadlock.
            await asyncio.gather(_write_stdin(), _read_stdout())
            await proc.wait()
            return "".join(parts)

        try:
            raw = await asyncio.wait_for(drive(), timeout=timeout_seconds)
        except TimeoutError:
            # Terminate the entire process group so descendant subagents
            # (e.g. tools spawned by the CLI) aren't orphaned.  Two-phase
            # escalation — SIGTERM first for a graceful shutdown window,
            # then SIGKILL for any stragglers.
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
                await asyncio.sleep(0.3)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()
            return AgentResult(ok=False, error=f"timed out after {timeout_seconds}s")

        return _parse_envelope(raw, proc.returncode)


def _parse_envelope(raw: str, exit_code: int | None) -> AgentResult:
    text = raw
    model_reported: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    tokens: dict[str, int | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_creation_tokens": None,
    }
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
        usage = parsed.get("usage")
        if isinstance(usage, dict):
            tokens["input_tokens"] = _as_int(usage.get("input_tokens"))
            tokens["output_tokens"] = _as_int(usage.get("output_tokens"))
            tokens["cache_read_tokens"] = _as_int(usage.get("cache_read_input_tokens"))
            tokens["cache_creation_tokens"] = _as_int(usage.get("cache_creation_input_tokens"))

    ok = exit_code == 0 or bool(_SUCCESS_HINT.search(text))
    return AgentResult(
        ok=ok,
        text=text,
        model_reported=model_reported,
        cost_usd=cost_usd,
        num_turns=num_turns,
        input_tokens=tokens["input_tokens"],
        output_tokens=tokens["output_tokens"],
        cache_read_tokens=tokens["cache_read_tokens"],
        cache_creation_tokens=tokens["cache_creation_tokens"],
        exit_code=exit_code,
    )


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) else None
