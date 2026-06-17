"""The ``opencode`` CLI provider — drives opencode headless (``run --format json``).

Pipes the prompt via stdin (avoiding arg-length limits), runs with auto-approved
permissions in the task worktree, and parses opencode's NDJSON event stream for the
assistant text, token usage, and cost.

opencode owns model/provider selection through its own config (``opencode.jsonc``), so
Conclave passes no model flag by default — every dispatch uses opencode's configured
default (e.g. ``deepseek/deepseek-v4-pro``). A profile may still override with an
opencode-format ``provider/model`` string.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path

from .base import AgentResult, OnChunk
from .claude_cli import _SUCCESS_HINT, _kill_process_group, _wait_cancel
from .profiles import ResolvedProfile

# The opencode binary; overridable for non-PATH installs (e.g. ~/.opencode/bin/opencode).
_OPENCODE_BIN = os.environ.get("CONCLAVE_OPENCODE_BIN", "opencode")


class OpenCodeCliProvider:
    """Runs agents by invoking the ``opencode`` CLI as a subprocess."""

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
        args = [
            "run",
            "--format", "json",
            "--dangerously-skip-permissions",
            # A fixed title skips opencode's per-session title-generation LLM call.
            "--title", "conclave",
        ]
        # Pin opencode's working directory EXPLICITLY. Passing cwd to the subprocess is
        # not sufficient: opencode resolves its project directory independently of the
        # inherited process cwd, and would otherwise edit the daemon's own checkout
        # instead of the task worktree (silently corrupting the repo and merging nothing).
        if cwd is not None:
            args += ["--dir", str(cwd)]
        # opencode owns the default model; only override when a profile names an
        # opencode-format "provider/model" (claude-style names like "claude-opus-4-8"
        # have no "/" and are ignored so the opencode default is used).
        if profile.model and "/" in profile.model:
            args += ["--model", profile.model]

        try:
            proc = await asyncio.create_subprocess_exec(
                _OPENCODE_BIN,
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=str(cwd) if cwd is not None else None,
                start_new_session=True,
            )
        except FileNotFoundError:
            return AgentResult(ok=False, error=f"CLI not found: {_OPENCODE_BIN!r}")

        async def drive() -> str:
            assert proc.stdin is not None
            assert proc.stdout is not None
            stdin = proc.stdin
            stdout = proc.stdout
            parts: list[str] = []

            async def _write_stdin() -> None:
                stdin.write(prompt.encode("utf-8"))
                await stdin.drain()
                stdin.close()

            async def _read_stdout() -> None:
                # Read raw chunks, NOT readline(): opencode's NDJSON lines (a big file read or
                # a long tool result) can exceed asyncio's default 64 KiB StreamReader line
                # limit, which raises LimitOverrunError and aborts the whole dispatch.
                # _parse_events splits the accumulated output on newlines, so chunk boundaries
                # are harmless (mirrors the claude provider's raw-chunk read).
                while True:
                    chunk = await stdout.read(65536)
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    parts.append(text)
                    if on_chunk is not None:
                        await on_chunk(text)

            # Write stdin and read stdout concurrently so a chatty child cannot fill
            # the OS pipe buffer and deadlock (mirrors the claude provider, CON-3).
            await asyncio.gather(_write_stdin(), _read_stdout())
            await proc.wait()
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
                [drive_task], timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED
            )

        if cancel_wait_task is not None and cancel_wait_task in done:
            await _kill_process_group(proc.pid)
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()
            drive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drive_task
            return AgentResult(ok=False, error="cancelled")

        if drive_task in done:
            raw = drive_task.result()
            if cancel_wait_task is not None:
                cancel_wait_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_wait_task
            return _parse_events(raw, proc.returncode)

        # timeout: neither completed in time
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


def _add(acc: int | None, value: object) -> int | None:
    """Sum integer token counts across steps, tolerating missing/None values."""
    if not isinstance(value, int):
        return acc
    return value if acc is None else acc + value


def _parse_events(raw: str, exit_code: int | None) -> AgentResult:
    """Parse opencode's NDJSON event stream into an :class:`AgentResult`.

    Concatenates every ``text`` event's ``part.text`` for the result, and sums
    ``step_finish`` events' ``part.cost`` and ``part.tokens`` for usage. Malformed
    lines are skipped so partial/garbled output never crashes a dispatch.
    """
    text_parts: list[str] = []
    cost = 0.0
    cost_seen = False
    in_tok: int | None = None
    out_tok: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    steps = 0

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        etype = obj.get("type")
        raw_part = obj.get("part")
        part = raw_part if isinstance(raw_part, dict) else {}
        if etype == "text":
            txt = part.get("text")
            if isinstance(txt, str):
                text_parts.append(txt)
        elif etype == "step_finish":
            steps += 1
            c = part.get("cost")
            if isinstance(c, int | float):
                cost += float(c)
                cost_seen = True
            raw_tokens = part.get("tokens")
            tokens = raw_tokens if isinstance(raw_tokens, dict) else {}
            in_tok = _add(in_tok, tokens.get("input"))
            out_tok = _add(out_tok, tokens.get("output"))
            raw_cache = tokens.get("cache")
            cache = raw_cache if isinstance(raw_cache, dict) else {}
            cache_read = _add(cache_read, cache.get("read"))
            cache_write = _add(cache_write, cache.get("write"))

    text = "".join(text_parts)
    ok = exit_code == 0 or bool(_SUCCESS_HINT.search(text))
    return AgentResult(
        ok=ok,
        text=text,
        model_reported=None,
        cost_usd=cost if cost_seen else None,
        num_turns=steps or None,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_write,
        exit_code=exit_code,
    )
