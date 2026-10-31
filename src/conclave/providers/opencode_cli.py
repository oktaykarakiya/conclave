"""The ``opencode`` CLI provider — drives opencode headless (``run --format json``).

Pipes the prompt via stdin (avoiding arg-length limits), runs with auto-approved
permissions in the task worktree, and parses opencode's NDJSON event stream for the
assistant text, token usage, and cost.

opencode owns model/provider selection through its own config (``opencode.jsonc``), so
Conclave passes no model flag by default — every dispatch uses opencode's configured
default (e.g. ``deepseek/deepseek-flash``). A profile may still override with an
opencode-format ``provider/model`` string.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import re
from pathlib import Path

from .base import AgentResult, OnChunk
from .claude_cli import (
    _SUCCESS_HINT,
    _WIN_SUBPROCESS_KW,
    _kill_process_group,
    _resolve_cli_exec,
    _wait_cancel,
)
from .profiles import ResolvedProfile

# The opencode binary; overridable for non-PATH installs (e.g. ~/.opencode/bin/opencode).
_OPENCODE_BIN = os.environ.get("CONCLAVE_OPENCODE_BIN", "opencode")

# Transient API / connection drop patterns that warrant an immediate ~1s retry
# (and session resume when a sessionID was already assigned).
_TRANSIENT_API_ERROR_RE = re.compile(
    r"(?i)(cannot connect to api|unable to connect|econnreset|econnrefused|"
    r"etimedout|socket hang up|fetch failed|connection reset by peer|"
    r"502 bad gateway|503 service unavailable|504 gateway timeout)"
)
_TRANSIENT_RETRY_DELAY_S = 1.0
_MAX_DISPATCH_ATTEMPTS = 3


def _is_transient_error(result: AgentResult, raw: str = "") -> bool:
    if result.error and _TRANSIENT_API_ERROR_RE.search(result.error):
        return True
    if not result.ok and raw and _TRANSIENT_API_ERROR_RE.search(raw):
        return True
    if (
        result.text
        and _TRANSIENT_API_ERROR_RE.search(result.text)
        and len(result.text.strip()) < 400
    ):
        return True
    return False


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
        base_args = [
            "run",
            "--format", "json",
            "--auto",
            # A fixed title skips opencode's per-session title-generation LLM call.
            "--title", "conclave",
        ]
        # Pin opencode's working directory EXPLICITLY. Passing cwd to the subprocess is
        # not sufficient: opencode resolves its project directory independently of the
        # inherited process cwd, and would otherwise edit the daemon's own checkout
        # instead of the task worktree (silently corrupting the repo and merging nothing).
        if cwd is not None:
            base_args += ["--dir", str(cwd)]
        # opencode owns the default model; only override when a profile names an
        # opencode-format "provider/model" (claude-style names like "claude-opus-4-8"
        # have no "/" and are ignored so the opencode default is used).
        model_hint: str | None = None
        if profile.model and "/" in profile.model:
            base_args += ["--model", profile.model]
            model_hint = profile.model
        if profile.effort:
            base_args += ["--variant", profile.effort]
        if profile.cli_agent:
            base_args += ["--agent", profile.cli_agent]
        if profile.thinking:
            base_args += ["--thinking"]
        if profile.pure:
            base_args += ["--pure"]

        last_result: AgentResult | None = None
        resume_session_id: str | None = None
        for attempt_idx in range(_MAX_DISPATCH_ATTEMPTS):
            if attempt_idx > 0:
                await asyncio.sleep(_TRANSIENT_RETRY_DELAY_S + random.random() * 0.25)
            attempt_args = list(base_args)
            if resume_session_id:
                attempt_args += ["--session", resume_session_id]
            exec_bin, exec_args = _resolve_cli_exec(_OPENCODE_BIN, attempt_args)
            try:
                proc = await asyncio.create_subprocess_exec(
                    exec_bin,
                    *exec_args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(cwd) if cwd is not None else None,
                    start_new_session=True,
                    **_WIN_SUBPROCESS_KW,
                )
            except FileNotFoundError:
                return AgentResult(ok=False, error=f"CLI not found: {_OPENCODE_BIN!r}")

            async def drive(p: asyncio.subprocess.Process = proc) -> str:
                assert p.stdin is not None
                assert p.stdout is not None
                stdin = p.stdin
                stdout = p.stdout
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
                last_result = _parse_events(raw, proc.returncode, model_hint=model_hint)
                if last_result.session_id:
                    resume_session_id = last_result.session_id
                if _is_transient_error(last_result, raw):
                    last_result.ok = False
                    if not last_result.error:
                        last_result.error = "Transient API connection error"
                    continue
                if last_result.text.strip():
                    return last_result
                continue

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

        assert last_result is not None
        return last_result


def _add(acc: int | None, value: object) -> int | None:
    """Sum integer token counts across steps, tolerating missing/None values."""
    if not isinstance(value, int):
        return acc
    return value if acc is None else acc + value


def _parse_events(
    raw: str, exit_code: int | None, model_hint: str | None = None
) -> AgentResult:
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
    reasoning_tok: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    steps = 0
    session_id: str | None = None
    model_reported: str | None = None
    tool_calls_count = 0
    tool_breakdown: dict[str, int] = {}
    error_parts: list[str] = []

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            if _TRANSIENT_API_ERROR_RE.search(line):
                error_parts.append(line)
            continue
        if not isinstance(obj, dict):
            continue
        sid = obj.get("sessionID")
        if isinstance(sid, str) and sid:
            session_id = sid
        etype = obj.get("type")
        raw_part = obj.get("part")
        part = raw_part if isinstance(raw_part, dict) else {}
        raw_model = part.get("model") or obj.get("model")
        if isinstance(raw_model, str) and raw_model:
            model_reported = raw_model
        if etype == "text":
            txt = part.get("text")
            if isinstance(txt, str):
                text_parts.append(txt)
        elif etype == "error":
            raw_err = obj.get("error") or part.get("error") or part.get("message")
            if isinstance(raw_err, dict):
                data_obj = raw_err.get("data")
                msg = (
                    (data_obj.get("message") if isinstance(data_obj, dict) else None)
                    or raw_err.get("message")
                    or raw_err.get("name")
                    or json.dumps(raw_err)
                )
                error_parts.append(str(msg))
            elif raw_err is not None:
                error_parts.append(str(raw_err))
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
            reasoning_tok = _add(reasoning_tok, tokens.get("reasoning"))
            raw_cache = tokens.get("cache")
            cache = raw_cache if isinstance(raw_cache, dict) else {}
            cache_read = _add(cache_read, cache.get("read"))
            cache_write = _add(cache_write, cache.get("write"))
        elif etype in ("tool_use", "tool") or part.get("type") in (
            "tool",
            "tool-invocation",
            "tool_use",
        ):
            raw_tool = part.get("tool") or part.get("name") or obj.get("tool") or "tool"
            tool_name = str(raw_tool) if raw_tool else "tool"
            tool_calls_count += 1
            tool_breakdown[tool_name] = tool_breakdown.get(tool_name, 0) + 1

    text = "".join(text_parts)
    error_str = "; ".join(error_parts) if error_parts else None
    if model_reported is None:
        model_reported = model_hint
    messages: list[dict[str, object]] = []
    if text or tool_breakdown:
        messages.append(
            {
                "role": "assistant",
                "text": text,
                "tools": [{"name": k, "count": v} for k, v in tool_breakdown.items()],
            }
        )
    has_transient_err = bool(error_str and _TRANSIENT_API_ERROR_RE.search(error_str))
    ok = (
        (exit_code == 0 or bool(_SUCCESS_HINT.search(text)))
        and not (has_transient_err and not text.strip())
        and not (error_str and not text.strip())
    )
    return AgentResult(
        ok=ok,
        text=text,
        model_reported=model_reported,
        cost_usd=cost if cost_seen else None,
        num_turns=steps or None,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_write,
        exit_code=exit_code,
        error=error_str if not ok or has_transient_err else None,
        session_id=session_id,
        reasoning_tokens=reasoning_tok,
        tool_calls_count=tool_calls_count,
        tool_breakdown=tool_breakdown,
        messages=messages,
    )
