"""The ``claude`` CLI provider — a port of team-ai's dispatch with streaming.

Spawns the CLI under the profile's composed args + environment, pipes the prompt via
stdin (avoiding arg-length limits), optionally streams stdout chunks to ``on_chunk``,
and parses the JSON envelope for the result text, reported model, cost, and turns.

A run refused for a usage limit or billing is reported as ``AgentResult.quota`` (never ok), see
:func:`_detect_quota`: the stream's latest ``rate_limit_info`` refuses requests, the last
assistant message's top-level ``error`` is ``rate_limit`` or ``billing_error``, or, for CLIs that
report no rate limits, the short final text is the CLI's own limit wording. The latest
``rate_limit_info`` of the stream is kept as ``AgentResult.rate_limit_info``. A run stopped by
its timeout, or because a refusing ``rate_limit_info`` stayed the latest for
:data:`_QUOTA_STOP_GRACE_S` while the run went on, is parsed from what it streamed so far: a
usage limit found there is reported as ``quota``, not as a timeout.

When one session ends several turns in a stream (a background task that finished after the
answer wakes it again), their per-turn counts are summed, see :func:`_merge_results`.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .base import AgentResult, OnChunk, QuotaExhausted, QuotaKind, epoch_to_utc
from .profiles import ResolvedProfile, build_invocation


def _build_win_subprocess_kw() -> dict[str, Any]:
    # sys.platform (not os.name) so mypy skips the Windows-only branch on POSIX.
    if sys.platform != "win32":
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

# An assistant message's top-level ``error`` for a refused request, and the quota kind it means.
_QUOTA_ERRORS: dict[str, QuotaKind] = {"rate_limit": "rate_limit", "billing_error": "billing"}
# Fallback for CLIs without the structured signals (a stream with no rate_limit_event at all):
# the whole final text is the CLI's own limit message, e.g. "You've hit your limit · resets
# 3pm", "usage limit reached · continues automatically when it resets", or the legacy
# "Claude AI usage limit reached|<epoch>" (the epoch is the reset time). Only a short text that
# starts with it counts, so an answer quoting the wording is never mistaken for a limit.
_LIMIT_TEXT = re.compile(
    r"(?i)^(?:claude (?:ai )?usage limit reached\|(?P<epoch>\d{9,13})"
    r"|(?:claude (?:ai )?)?usage limit reached\b|you(?:'|\u2019)ve hit your (?:usage )?limit\b)"
)
_LIMIT_TEXT_MAX_CHARS = 300
# How long a run may go on after the latest rate_limit_info refused its requests (the CLI waiting
# or retrying inside the run) before it is stopped and reported as a usage limit.
_QUOTA_STOP_GRACE_S = 120.0
# An isolated worker runs every Bash command in the foreground (no background tasks): one that
# names no timeout of its own gets at most this long.
_BASH_DEFAULT_TIMEOUT_S = 20 * 60

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
    # Session identity of a parent Claude Code process (e.g. the daemon was launched from a
    # Claude Code terminal). Inherited, they make each dispatch look like a nested session
    # of that parent instead of an independent headless run.
    "CLAUDECODE",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
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
        if profile.isolated:
            env.update(_bash_timeout_env(timeout_seconds))
        env.update(invocation.env)  # last, so the profile's extra_env wins

        exec_bin, exec_args = _resolve_cli_exec(profile.cli, list(invocation.args))
        last_result: AgentResult | None = None
        for attempt_idx in range(_CLAUDE_MAX_ATTEMPTS):
            if attempt_idx > 0:
                await asyncio.sleep(_CLAUDE_TRANSIENT_RETRY_DELAY_S)
            # Never start a run once cancelled, so a transient-looking failure is not retried.
            if cancel_event is not None and cancel_event.is_set():
                return AgentResult(ok=False, error="cancelled", cancelled=True)
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

            # Everything the run streams. Kept outside drive() so a stopped run can still be
            # parsed; safe to share, since only one coroutine runs at a time.
            output: list[str] = []
            watch = _RefusalWatch()

            async def drive(
                p: asyncio.subprocess.Process = proc,
                parts: list[str] = output,
                refusals: _RefusalWatch = watch,
            ) -> str:
                assert p.stdin is not None
                assert p.stdout is not None
                # Capture narrowed types so mypy sees them inside the inner coroutines.
                stdin = p.stdin
                stdout = p.stdout

                async def _write_stdin() -> None:
                    """Drain the prompt into stdin and close it to signal EOF. A CLI that exits
                    without reading it all (one rejecting its arguments) closes the pipe: ignored,
                    as asyncio's communicate() does, so its own output and exit code tell why."""
                    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                        stdin.write(prompt.encode("utf-8"))
                        await stdin.drain()
                    stdin.close()

                async def _read_stdout() -> None:
                    """Consume all stdout chunks, appending to parts and streaming on_chunk. One
                    decoder reads every chunk, so a character split across two reads stays whole;
                    a read that completes no character streams nothing."""
                    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                    while True:
                        chunk = await stdout.read(4096)
                        text = decoder.decode(chunk, final=not chunk)
                        if text:
                            parts.append(text)
                            refusals.feed(text)
                            if on_chunk is not None:
                                await on_chunk(text)
                        if not chunk:
                            break

                # Run stdin-writing and stdout-reading concurrently so a chatty child
                # that emits a large volume of stdout while stdin is still being
                # written cannot fill the OS pipe buffer and deadlock.
                await asyncio.gather(_write_stdin(), _read_stdout())
                await p.wait()
                return "".join(parts)

            drive_task = asyncio.create_task(drive())
            # A run can go on after a usage limit refused it (the CLI waiting or retrying inside
            # it): stop it once the refusal outlasts the grace period, not at the agent timeout.
            refusal_task = asyncio.create_task(watch.outlasts(_QUOTA_STOP_GRACE_S))
            cancel_wait_task: asyncio.Task[None] | None = None
            waiting: list[asyncio.Task[Any]] = [drive_task, refusal_task]
            if cancel_event is not None:
                cancel_wait_task = asyncio.create_task(_wait_cancel(cancel_event))
                waiting.append(cancel_wait_task)
            try:
                done, _pending = await asyncio.wait(
                    waiting, timeout=timeout_seconds, return_when=asyncio.FIRST_COMPLETED
                )
            except asyncio.CancelledError:
                # The task awaiting the dispatch was cancelled (a planning session's cancel, a
                # worker's stop, shutdown): stop the run as a cancel event does, then re-raise.
                await _kill_process_group(proc.pid)
                with contextlib.suppress(ProcessLookupError):
                    await proc.wait()
                await _cancel_tasks(drive_task, refusal_task, cancel_wait_task)
                raise

            # --- cancellation won the race ---
            if cancel_wait_task is not None and cancel_wait_task in done:
                await _kill_process_group(proc.pid)
                with contextlib.suppress(ProcessLookupError):
                    await proc.wait()
                await _cancel_tasks(drive_task, refusal_task)
                return AgentResult(ok=False, error="cancelled", cancelled=True)

            # --- drive completed normally ---
            if drive_task in done:
                await _cancel_tasks(refusal_task, cancel_wait_task)
                raw: str = drive_task.result()
                last_result = _parse_envelope(raw, proc.returncode)
                # A usage limit is not a dropped connection: re-running it at once would be
                # refused again. The shared dispatch path waits for the reset instead.
                if last_result.quota is not None:
                    return last_result
                # Match the outcome only, not the whole stream: tool output in the
                # transcript (e.g. a test printing ECONNREFUSED) must not re-run the agent.
                outcome = last_result.error or last_result.text
                if not last_result.ok and _TRANSIENT_CONN_ERROR.search(outcome):
                    last_result.error = "Cannot connect to API: transient connection error"
                    continue
                return last_result

            # --- timeout, or a refusal that outlasted the grace period ---
            await _kill_process_group(proc.pid)
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()
            await _cancel_tasks(drive_task, refusal_task, cancel_wait_task)
            # A usage limit the run reported before it was stopped is a usage limit, not a
            # timeout: the shared dispatch path waits for the reset and resumes the session.
            stopped = _parse_envelope("".join(output), proc.returncode)
            if stopped.quota is not None:
                return stopped
            return AgentResult(ok=False, error=f"timed out after {timeout_seconds}s")

        assert last_result is not None
        return last_result


def _parse_envelope(raw: str, exit_code: int | None) -> AgentResult:
    """Parse the CLI's output: a single ``--output-format json`` envelope, or the
    ``stream-json`` NDJSON stream whose final ``{"type": "result"}`` event carries the
    same fields. Tool calls, the session id and the rate-limit signals are collected from the
    stream events.

    The answer is the result's ``result`` text only. A run whose JSON output has none (an error
    result such as ``error_during_execution`` or ``error_max_turns``, or a stream that ended
    without a result event) gave no answer: its text is empty and it is never ok, whatever its
    exit code, so nothing it streamed (a file it read, its own thinking) is taken for an answer.
    Its error is the error result's ``errors`` (else its subtype), or the end of the stream.
    Output that is not JSON at all is kept as the text.
    """
    text = raw
    model_reported: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    session_id: str | None = None
    is_error = False
    tool_breakdown: dict[str, int] = {}
    tokens: dict[str, int | None] = {
        "input_tokens": None,
        "output_tokens": None,
        "cache_read_tokens": None,
        "cache_creation_tokens": None,
    }
    scan = _StreamScan()
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        parsed = None
    # The envelope is a result; any other single JSON value is read as a stream of one line.
    if not (isinstance(parsed, dict) and parsed.get("type", "result") == "result"):
        scan = _scan_stream(raw, tool_breakdown)
        parsed = scan.result
    no_answer = False
    if isinstance(parsed, dict):
        answer = parsed.get("result")
        text = answer if isinstance(answer, str) else ""
        no_answer = not isinstance(answer, str)
        is_error = parsed.get("is_error") is True
        model_usage = parsed.get("modelUsage") or {}
        if isinstance(model_usage, dict) and model_usage:
            model_reported = ", ".join(str(k) for k in model_usage)
        cost = parsed.get("total_cost_usd")
        if isinstance(cost, int | float):
            cost_usd = float(cost)
        turns = parsed.get("num_turns")
        if isinstance(turns, int):
            num_turns = turns
        sid = parsed.get("session_id")
        if isinstance(sid, str) and sid:
            session_id = sid
        usage = parsed.get("usage")
        if isinstance(usage, dict):
            tokens["input_tokens"] = _as_int(usage.get("input_tokens"))
            tokens["output_tokens"] = _as_int(usage.get("output_tokens"))
            tokens["cache_read_tokens"] = _as_int(usage.get("cache_read_input_tokens"))
            tokens["cache_creation_tokens"] = _as_int(usage.get("cache_creation_input_tokens"))
    elif scan.events:  # a stream that ended without a result event
        no_answer = True
        text = ""
    session_id = session_id or scan.session_id

    quota = _detect_quota(scan, text)
    ok = (
        not no_answer
        and (exit_code == 0 or bool(_SUCCESS_HINT.search(text)))
        and not is_error
        and quota is None
    )
    error: str | None = None
    if not ok:
        error = (
            text.strip()[-500:]
            or _result_errors(parsed)
            or (raw.strip()[-500:] if parsed is None else "")
            or (quota.message if quota is not None else "")
            or f"claude exited with code {exit_code}"
        )
    return AgentResult(
        ok=ok,
        text=text,
        model_reported=model_reported,
        cost_usd=cost_usd,
        # total_cost_usd is the session's running total: a --resume run continues it.
        cost_is_session_total=True,
        num_turns=num_turns,
        input_tokens=tokens["input_tokens"],
        output_tokens=tokens["output_tokens"],
        cache_read_tokens=tokens["cache_read_tokens"],
        cache_creation_tokens=tokens["cache_creation_tokens"],
        exit_code=exit_code,
        error=error,
        session_id=session_id,
        tool_calls_count=sum(tool_breakdown.values()),
        tool_breakdown=tool_breakdown,
        quota=quota,
        rate_limit_info=scan.rate_limit,
    )


@dataclass
class _StreamScan:
    """What :func:`_scan_stream` collects from a ``stream-json`` run."""

    # The last ``result`` event, with the per-turn counts of earlier ones of its session added
    result: dict[str, Any] | None = None
    events: int = 0  # the JSON object lines read
    session_id: str | None = None  # the last session id any event carried
    rate_limit: dict[str, Any] | None = None  # the latest ``rate_limit_info``
    error: str | None = None  # the top-level ``error`` of the last assistant message, if any
    error_text: str = ""  # that message's text


def _scan_stream(raw: str, tool_breakdown: dict[str, int]) -> _StreamScan:
    """Walk ``stream-json`` lines: tally ``tool_use`` blocks into *tool_breakdown*, keep the last
    ``result`` event (``None`` when the stream never produced one, e.g. a killed process or
    non-JSON output), count the events read, and keep the rate-limit signals. A
    ``result`` that follows one of the same session is merged with it (:func:`_merge_results`):
    a session woken for another turn after its answer ends each turn with its own.

    Lines end at a newline only: JSON leaves U+2028, U+2029 and U+0085 raw inside strings, so
    :meth:`str.splitlines` would cut the event holding one in two."""
    scan = _StreamScan()
    for line in raw.split("\n"):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        scan.events += 1
        sid = obj.get("session_id")
        if isinstance(sid, str) and sid:
            scan.session_id = sid
        etype = obj.get("type")
        if etype == "result":
            previous = scan.result
            if previous is not None and previous.get("session_id") == obj.get("session_id"):
                scan.result = _merge_results(previous, obj)
            else:
                scan.result = obj
        elif etype == "rate_limit_event":
            info = obj.get("rate_limit_info")
            if isinstance(info, dict):
                scan.rate_limit = info
        elif etype == "assistant":
            message = obj.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            blocks = content if isinstance(content, list) else []
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = str(block.get("name") or "tool")
                    tool_breakdown[name] = tool_breakdown.get(name, 0) + 1
            # Only the last assistant message's error counts: a run that went on after a
            # refused request (a later message without one) was not stopped by it.
            error = obj.get("error")
            scan.error = error if isinstance(error, str) and error else None
            scan.error_text = (
                "".join(
                    str(block.get("text") or "")
                    for block in blocks
                    if isinstance(block, dict) and block.get("type") == "text"
                ).strip()
                if scan.error is not None
                else ""
            )
    return scan


# The per-turn counts of a ``result`` event, summed over the turns of one session.
_SUMMED_RESULT_KEYS: tuple[str, ...] = ("num_turns", "duration_ms")
_SUMMED_USAGE_KEYS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _merge_results(earlier: dict[str, Any], latest: dict[str, Any]) -> dict[str, Any]:
    """One ``result`` event for two turns of a session: a copy of the *latest*, with the per-turn
    counts (``num_turns``, ``duration_ms`` and the ``usage`` tokens) summed. The text,
    ``is_error``, session id and the CLI's running totals (``total_cost_usd``,
    ``duration_api_ms``) stay the latest's. ``modelUsage`` is a running total too, so it is not
    summed: the latest's, plus any model only the earlier one names, so the reported models cover
    every model behind the summed tokens."""
    merged = {**latest, **_summed(earlier, latest, _SUMMED_RESULT_KEYS)}
    earlier_usage, latest_usage = _mapping(earlier.get("usage")), _mapping(latest.get("usage"))
    if earlier_usage or latest_usage:
        merged["usage"] = {
            **latest_usage,
            **_summed(earlier_usage, latest_usage, _SUMMED_USAGE_KEYS),
        }
    models = {**_mapping(earlier.get("modelUsage")), **_mapping(latest.get("modelUsage"))}
    if models:
        merged["modelUsage"] = models
    return merged


def _summed(a: dict[str, Any], b: dict[str, Any], keys: tuple[str, ...]) -> dict[str, int]:
    """For each of *keys* that *a* or *b* holds as an integer, the sum of those integers."""
    totals: dict[str, int] = {}
    for key in keys:
        values = [value for value in (a.get(key), b.get(key)) if isinstance(value, int)]
        if values:
            totals[key] = sum(values)
    return totals


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _result_errors(result: dict[str, Any] | None) -> str:
    """Why a ``result`` without an answer ended: its ``errors`` (an error result's messages), else
    its subtype (``error_max_turns``, ...); ``""`` when it names neither."""
    if result is None:
        return ""
    errors = result.get("errors")
    if isinstance(errors, list) and (joined := "; ".join(str(e) for e in errors if e)):
        return joined[-500:]
    subtype = result.get("subtype")
    return subtype if isinstance(subtype, str) and subtype != "success" else ""


class _RefusalWatch:
    """Follows a run's stream as it arrives, for a usage limit that refuses its requests.

    :meth:`feed` takes each chunk of output and reads every complete line once (a partial line
    waits for the rest of it). The watch is armed while the latest ``rate_limit_info`` refuses
    requests (:func:`_refuses`); a newer one that does not refuse disarms it, the rule
    :func:`_detect_quota` applies to the whole stream.
    """

    def __init__(self) -> None:
        self._partial: list[str] = []  # the current line so far
        self._armed = asyncio.Event()
        self._since = 0.0  # time.monotonic() when the watch was last armed

    def feed(self, text: str) -> None:
        start = 0
        while (end := text.find("\n", start)) >= 0:
            self._partial.append(text[start:end])
            self._read_line("".join(self._partial))
            self._partial.clear()
            start = end + 1
        if start < len(text):
            self._partial.append(text[start:])

    def _read_line(self, line: str) -> None:
        if "rate_limit_event" not in line:  # most lines are other events: skip parsing them
            return
        try:
            event = json.loads(line)
        except ValueError:
            return
        if not isinstance(event, dict) or event.get("type") != "rate_limit_event":
            return
        info = event.get("rate_limit_info")
        if not isinstance(info, dict):
            return
        if not _refuses(info):
            self._armed.clear()
        elif not self._armed.is_set():
            self._since = time.monotonic()
            self._armed.set()

    async def outlasts(self, grace: float) -> None:
        """Return once the watch has stayed armed for *grace* seconds."""
        while True:
            await self._armed.wait()
            since = self._since
            await asyncio.sleep(max(since + grace - time.monotonic(), 0.0))
            if self._armed.is_set() and self._since == since:
                return


def _detect_quota(scan: _StreamScan, text: str) -> QuotaExhausted | None:
    """The usage limit or billing refusal a run ended on, or ``None``.

    A run is refused when any of these holds:

    - the stream's latest ``rate_limit_info`` refuses requests (:func:`_refuses`). Only the
      latest counts: the CLI reports one on every turn, so an earlier ``rejected`` that a later
      one replaced no longer stops anything;
    - the last assistant message's top-level ``error`` is ``rate_limit`` or ``billing_error``
      (``billing``);
    - the stream reported no rate limits at all (an older CLI, or an account whose responses
      carry none) and the short final text is the CLI's own limit message.

    The reset time is the latest reset of the limiting windows (:func:`_limit_window`) of the
    refusing info (status ``rejected``), else (for a rate limit, not a billing refusal) of the
    latest info when its status is ``allowed_warning``, else the legacy text's epoch, else unknown
    (the gate then waits ``execution.quota_retry_minutes``). A latest info that is ``allowed``, or
    a rejected limit that paid extra usage serves, leaves the windows usable: a request refused
    then hit a short burst limit, and the window's reset, possibly hours away, is not the time to
    resume.
    """
    final = text.strip()
    limit_text = (
        _LIMIT_TEXT.match(final)
        if scan.rate_limit is None and final and len(final) <= _LIMIT_TEXT_MAX_CHARS
        else None
    )
    refusing = scan.rate_limit if _refuses(scan.rate_limit) else None
    error_kind = _QUOTA_ERRORS.get(scan.error or "")
    if refusing is None and error_kind is None and limit_text is None:
        return None
    kind: QuotaKind = error_kind or "rate_limit"
    window, resets_at = _limit_window(refusing)
    latest = scan.rate_limit
    if (
        resets_at is None
        and kind == "rate_limit"
        and latest is not None
        and latest.get("status") == "allowed_warning"
    ):
        window, resets_at = _limit_window(latest)
    if resets_at is None and limit_text is not None and limit_text["epoch"]:
        resets_at = epoch_to_utc(int(limit_text["epoch"]))
    # The engine's own words: the limit text, else the refused message, else the result's text
    # (never the raw stream a run without a result event leaves as its text).
    message = (
        (final if limit_text is not None else "")
        or scan.error_text
        or (final if scan.result is not None else "")
        or ("billing error" if kind == "billing" else "usage limit reached")
    )
    return QuotaExhausted(
        kind=kind, resets_at=resets_at, window=window, message=message[:_LIMIT_TEXT_MAX_CHARS]
    )


def _refuses(info: dict[str, Any] | None) -> bool:
    """Whether a ``rate_limit_info`` says requests are refused: the CLI's own still-usable test
    (``status != "rejected" || isUsingOverage || overageInUse``) fails. With ``isUsingOverage``
    or ``overageInUse`` true, paid extra usage serves the requests past the rejected limit."""
    return (
        info is not None
        and info.get("status") == "rejected"
        and info.get("isUsingOverage") is not True
        and info.get("overageInUse") is not True
    )


def _limit_window(info: dict[str, Any] | None) -> tuple[str | None, datetime | None]:
    """``(window, resets_at)`` of a ``rate_limit_info``: its own ``rateLimitType`` and
    ``resetsAt`` (the window that limits now), and, when its status is ``rejected``, every
    rejected unified window too; of those, the one that resets last, since requests stay refused
    until all of them have reset."""
    if info is None:
        return None, None
    window_type = info.get("rateLimitType")
    own_window = window_type if isinstance(window_type, str) and window_type else None
    limiting: list[tuple[datetime, str | None]] = []
    if (own_reset := epoch_to_utc(info.get("resetsAt"))) is not None:
        limiting.append((own_reset, own_window))
    windows = info.get("unifiedWindows")
    if info.get("status") == "rejected" and isinstance(windows, dict):
        limiting += [
            (resets_at, str(name))
            for name, window in windows.items()
            if isinstance(window, dict)
            and window.get("status") == "rejected"
            and (resets_at := epoch_to_utc(window.get("resetsAt"))) is not None
        ]
    if not limiting:
        return own_window, None
    # The latest reset; on a tie, a window with a name.
    resets_at, name = max(limiting, key=lambda candidate: (candidate[0], candidate[1] is not None))
    return name, resets_at


def _as_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _bash_timeout_env(timeout_seconds: int) -> dict[str, str]:
    """The Bash timeouts of an isolated dispatch, whose commands all run in the foreground: a
    command may ask for up to the dispatch's whole timeout (``BASH_MAX_TIMEOUT_MS``), and one that
    names none gets the smaller of :data:`_BASH_DEFAULT_TIMEOUT_S` and that timeout
    (``BASH_DEFAULT_TIMEOUT_MS``)."""
    return {
        "BASH_MAX_TIMEOUT_MS": str(timeout_seconds * 1000),
        "BASH_DEFAULT_TIMEOUT_MS": str(min(timeout_seconds, _BASH_DEFAULT_TIMEOUT_S) * 1000),
    }


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

    *pid* must have been started with ``start_new_session=True`` (every caller does), so it
    leads its own group and the group's id is *pid*. The group is signalled by that id, never
    by a ``getpgid`` lookup, which fails once the leader has exited and been reaped: a
    descendant still holding the CLI's output pipe is then killed all the same.
    """
    if pid is None:
        return
    try:
        killpg_fn = getattr(os, "killpg", None)
        if killpg_fn is not None:
            killpg_fn(pid, signal.SIGTERM)
            await asyncio.sleep(0.3)
            killpg_fn(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
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


async def _cancel_tasks(*tasks: asyncio.Task[Any] | None) -> None:
    """Cancel each task that is still running, then wait for every one of them to end."""
    for task in tasks:
        if task is not None:
            task.cancel()
    for task in tasks:
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _wait_cancel(event: asyncio.Event) -> None:
    """Bridge :meth:`asyncio.Event.wait` (which returns ``True``) to a ``None``-returning
    coroutine so ``asyncio.create_task`` type-checks against ``Task[None]``."""
    await event.wait()
