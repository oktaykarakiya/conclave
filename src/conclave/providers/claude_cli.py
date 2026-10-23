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
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from .base import AgentResult, OnChunk
from .profiles import ResolvedProfile, build_invocation


def _build_win_subprocess_kw() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 1)
    si.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        "startupinfo": si,
    }


_WIN_SUBPROCESS_KW: dict[str, Any] = _build_win_subprocess_kw()

# Mirrors team-ai: even on a non-zero exit, treat output that clearly contains an
# agent verdict / completion as a usable result rather than a hard failure.
_SUCCESS_HINT = re.compile(r"(?i)(verdict|task completed|i have)")

# Transient API connection drop pattern — triggers a 1-second automatic retry.
_TRANSIENT_CONN_ERROR = re.compile(
    r"(?i)(cannot connect to api|unable to connect|econnreset|econnrefused|"
    r"etimedout|socket hang up|fetch failed|connection reset by peer|"
    r"502 bad gateway|503 service unavailable|504 gateway timeout)"
)
_CLAUDE_TRANSIENT_RETRY_DELAY_S = 1.0
_CLAUDE_MAX_ATTEMPTS = 3

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
        cancel_event: asyncio.Event | None = None,
    ) -> AgentResult:
        invocation = build_invocation(profile)
        # Strip provider-controlled env vars from the parent environment so they
        # never leak into inherit/flag-mode dispatches. invocation.env reintroduces
        # them for env-mode profiles.
        env = {
            k: v for k, v in os.environ.items() if k not in _PROVIDER_ENV_KEYS
        }
        env.update(invocation.env)

        exec_bin, exec_args = _resolve_cli_exec(profile.cli, list(invocation.args))
        last_result: AgentResult | None = None
        for attempt_idx in range(_CLAUDE_MAX_ATTEMPTS):
            if attempt_idx > 0:
                await asyncio.sleep(_CLAUDE_TRANSIENT_RETRY_DELAY_S)
            try:
                proc = await asyncio.create_subprocess_exec(
                    exec_bin,
                    *exec_args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=env,
                    cwd=str(cwd) if cwd is not None else None,
                    start_new_session=True,
                    **_WIN_SUBPROCESS_KW,
                )
            except FileNotFoundError:
                return AgentResult(ok=False, error=f"CLI not found: {profile.cli!r}")

            async def drive(p: asyncio.subprocess.Process = proc) -> str:
                assert p.stdin is not None
                assert p.stdout is not None
                # Capture narrowed types so mypy sees them inside the inner coroutines.
                stdin = p.stdin
                stdout = p.stdout

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
                await p.wait()
                return "".join(parts)

            drive_task = asyncio.create_task(drive())
            cancel_wait_task: asyncio.Task[None] | None = None

            if cancel_event is not None:
                cancel_wait_task = asyncio.create_task(_wait_cancel(cancel_event))
                done, _pending = await asyncio.wait(
                    [drive_task, cancel_wait_task],
                    timeout=timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            else:
                done, _pending = await asyncio.wait(
                    [drive_task],
                    timeout=timeout_seconds,
                    return_when=asyncio.FIRST_COMPLETED,
                )

            # --- cancellation won the race ---
            if cancel_wait_task is not None and cancel_wait_task in done:
                await _kill_process_group(proc.pid)
                with contextlib.suppress(ProcessLookupError):
                    await proc.wait()
                drive_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await drive_task
                return AgentResult(ok=False, error="cancelled")

            # --- drive completed normally ---
            if drive_task in done:
                raw: str = drive_task.result()
                if cancel_wait_task is not None:
                    cancel_wait_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await cancel_wait_task
                last_result = _parse_envelope(raw, proc.returncode)
                if not last_result.ok and _TRANSIENT_CONN_ERROR.search(raw):
                    last_result.error = "Cannot connect to API: transient connection error"
                    continue
                return last_result

            # --- timeout: neither completed in time ---
            await _kill_process_group(proc.pid)
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()
            drive_task.cancel()
            if cancel_wait_task is not None:
                cancel_wait_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drive_task
            if cancel_wait_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_wait_task
            return AgentResult(ok=False, error=f"timed out after {timeout_seconds}s")

        assert last_result is not None
        return last_result


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


def _resolve_cli_exec(cli: str, args: list[str]) -> tuple[str, list[str]]:
    """On Windows, invoke ``.py`` CLI scripts through ``sys.executable`` if present,
    and resolve PATH commands (preferring native ``.exe`` over ``.cmd`` shims)."""
    if os.name == "nt" and cli.lower().endswith(".py") and Path(cli).is_file():
        return sys.executable, [cli, *args]
    if os.name == "nt" and not Path(cli).is_file():
        resolved = shutil.which(cli)
        if resolved:
            res_path = Path(resolved)
            if res_path.suffix.lower() in (".cmd", ".bat"):
                native_exe = (
                    res_path.parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
                )
                if native_exe.is_file():
                    return str(native_exe), args
            return resolved, args
    return cli, args


async def _kill_process_group(pid: int | None) -> None:
    """Two-phase process-group termination: SIGTERM, brief grace, then SIGKILL.

    Descendant subagents (e.g. tools spawned by the CLI) share the process group
    so a targeted ``killpg`` ensures they are not orphaned.
    """
    if pid is None:
        return
    try:
        getpgid_fn = getattr(os, "getpgid", None)
        killpg_fn = getattr(os, "killpg", None)
        if getpgid_fn is not None and killpg_fn is not None:
            pgid = getpgid_fn(pid)
            killpg_fn(pgid, signal.SIGTERM)
            await asyncio.sleep(0.3)
            killpg_fn(pgid, getattr(signal, "SIGKILL", signal.SIGTERM))
        else:
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/F",
                "/T",
                "/PID",
                str(pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                **_WIN_SUBPROCESS_KW,
            )
            await killer.wait()
            os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError, SystemError):
        pass


async def _wait_cancel(event: asyncio.Event) -> None:
    """Bridge :meth:`asyncio.Event.wait` (which returns ``True``) to a ``None``-returning
    coroutine so ``asyncio.create_task`` type-checks against ``Task[None]``."""
    await event.wait()
