"""The ``opencode`` CLI provider — drives opencode headless (``run --format json``).

Pipes the prompt via stdin (avoiding arg-length limits), runs with auto-approved
permissions in the task worktree, and parses opencode's NDJSON event stream for the
assistant text, token usage, and cost.

opencode owns model/provider selection through its own config (``opencode.jsonc``), so
Conclave passes no model flag by default — every dispatch uses opencode's configured
default (e.g. ``deepseek/deepseek-flash``). A profile may still override with an
opencode-format ``provider/model`` string.

An ``error`` event refusing the request for a usage limit or balance (HTTP 402 or 429, or a
balance / quota / rate-limit message) is reported as ``AgentResult.quota`` (never ok), with the
reset time a 429's ``retry-after-ms`` or ``retry-after`` header gives.

Every run gets an explicit environment: the daemon's own, then the worker isolation overrides of
an isolated profile (``execution.worker_isolation``), then the profile's ``extra_env``, see
:class:`OpenCodeCliProvider`. An isolated worker loads no skills from ``.claude`` or ``.agents``
folders and starts no MCP server. opencode has no switch that turns every MCP server off, so the
servers its resolved config names (``opencode debug config``) are each disabled through
``OPENCODE_CONFIG_CONTENT``, which opencode merges over every other config source.

An isolated worker also runs in the worker sandbox (``execution.worker_sandbox``, see
:mod:`.sandbox`) when the profile's ``sandbox`` allows it and bubblewrap works: the same argv
inside ``bwrap``, with its own opencode folders for its model provider instead of the operator's.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import email.utils
import json
import logging
import os
import random
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import WorkerSandbox
from .base import UNKNOWN_SESSION_RE, AgentResult, OnChunk, QuotaExhausted
from .claude_cli import (
    _SUCCESS_HINT,
    _WIN_SUBPROCESS_KW,
    _cancel_tasks,
    _kill_process_group,
    _resolve_cli_exec,
    _wait_cancel,
)
from .profiles import ResolvedProfile
from .sandbox import (
    DISCOVERY_FOLDER,
    OpencodeFolders,
    SandboxError,
    bwrap_command,
    discovery_mounts,
    dispatch_mounts,
    fallback_message,
    home_folder,
    opencode_folders,
    opencode_homes,
    operator_credentials,
    prepare_discovery_folders,
    prepare_provider_folders,
    provider_credential,
    provider_folders,
    required_message,
    sandbox_status,
    setup_failure,
)

logger = logging.getLogger("conclave.providers.opencode_cli")

# The opencode binary; overridable for non-PATH installs (e.g. ~/.opencode/bin/opencode).
_OPENCODE_BIN = os.environ.get("CONCLAVE_OPENCODE_BIN", "opencode")

# Worker isolation: no skills from .claude/skills or .agents/skills (global or the repository's).
# opencode's own skill folders and its built-in skill still load.
_ISOLATION_ENV: dict[str, str] = {
    "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
    "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
}
# Config JSON that opencode merges over every other config source: isolation disables each MCP
# server here ({"mcp": {name: {"enabled": false}}}, which keeps the server's definition).
_CONFIG_CONTENT_ENV = "OPENCODE_CONFIG_CONTENT"
# `opencode debug config` takes about a second; one that hangs must not hold the dispatch.
_MCP_DISCOVERY_TIMEOUT_S = 30.0
# Worktrees whose MCP server names are kept per provider (the oldest is dropped first).
_MCP_CACHE_MAX = 128
# A failed discovery's warning quotes the end of its stderr, without terminal colour codes.
_STDERR_TAIL_CHARS = 300
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# The daemon's variables that point opencode back at the operator's own setup (extra config
# sources, credentials, the session database): a sandboxed dispatch runs without them, though a
# profile's extra_env may set any of them again. Reading the operator's config keeps all but the
# session database, which that discovery must not open either.
_SESSION_DB_ENV = "OPENCODE_DB"
_OPERATOR_SETUP_ENV = (
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_AUTH_CONTENT",
    _SESSION_DB_ENV,
)
# `opencode debug config` prints every value that looks secret (a key, token or password, every
# header, a URL with credentials) as this: such a value is left out of a provider block a sandboxed
# worker gets, so its opencode falls back to its other sources (the credential, the environment).
_REDACTED = "***"
_NO_MODEL = (
    "This opencode dispatch names no model, and the operator's opencode config has no default "
    "model: the worker sandbox runs each dispatch in the opencode folders of its model's "
    "provider. Set the agent's model to an opencode provider/model (e.g. deepseek/deepseek-flash), "
    "or a default model in the operator's opencode config."
)

# Transient API / connection drop patterns that warrant an immediate ~1s retry
# (and session resume when a sessionID was already assigned).
_TRANSIENT_API_ERROR_RE = re.compile(
    r"(?i)(cannot connect to api|unable to connect|econnreset|econnrefused|"
    r"etimedout|socket hang up|fetch failed|connection reset by peer|"
    r"502 bad gateway|503 service unavailable|504 gateway timeout)"
)
_TRANSIENT_RETRY_DELAY_S = 1.0
_MAX_DISPATCH_ATTEMPTS = 3

# Error messages of a refused request: the balance or quota ran out, or a rate limit.
_BALANCE_ERROR_RE = re.compile(
    r"(?i)(insufficient[ _]balance|insufficient[ _](?:credits?|funds|quota)|quota[ _]exceeded|"
    r"exceeded your (?:current )?quota|credit balance is too low|payment required)"
)
_RATE_LIMIT_ERROR_RE = re.compile(r"(?i)(rate[ _]limit|too many requests|usage limit)")


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
    """Runs agents by invoking the ``opencode`` CLI as a subprocess.

    A dispatch's environment is passed explicitly (``env=``) and is the same for every attempt
    of that dispatch, transient retries and resumes included. It is built in this order:

    1. the daemon's environment (``os.environ`` at dispatch time);
    2. when ``profile.isolated`` (``execution.worker_isolation``), the isolation overrides:
       ``OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1``, ``OPENCODE_DISABLE_EXTERNAL_SKILLS=1`` and,
       when the resolved config has MCP servers, ``OPENCODE_CONFIG_CONTENT`` disabling each of
       them. A daemon value of ``OPENCODE_CONFIG_CONTENT`` that is a JSON object is kept, with
       the ``enabled: false`` entries merged over it; any other value is left unchanged (with a
       warning) and the servers stay on;
    3. ``profile.extra_env``, last, so a profile can override any variable above.

    Without isolation the environment is 1 and 3 only, and the argv is the same either way. The
    MCP server names come from ``opencode debug config``, run in the dispatch's ``cwd`` under
    environment 1 and 3, at most once per dispatch and only on a cache miss: the names are kept
    per (binary, ``cwd``, model provider) for this provider's lifetime, up to
    :data:`_MCP_CACHE_MAX` entries. A failed discovery is logged and not kept; that dispatch runs
    without the skills but with its MCP servers.

    **The worker sandbox** (``profile.sandbox``, ``execution.worker_sandbox``) applies to an
    isolated dispatch only. ``off``: the above. ``auto``: the sandbox when it is available
    (:func:`~.sandbox.sandbox_status`), else the above, with one warning per bubblewrap binary
    for this provider's lifetime. ``required``: the sandbox, or a failed dispatch that starts
    nothing. A sandboxed dispatch runs the same opencode argv inside ``bwrap --dev-bind / /
    <mounts> --die-with-parent --``, its own process group as before, so a cancel or a timeout
    still ends every process of it. It differs in these points only:

    - its model provider (the model's part before its first ``/``) gets its own opencode config,
      data and state folders (``$CONCLAVE_HOME/opencode-homes/<provider>``), mounted over the
      operator's (symlinks resolved): its sessions persist there for a retry or a resume. The
      config folder holds only the provider's block of the operator's opencode config, and the
      data folder only the operator's ``api`` or ``wellknown`` credential for it, both refreshed
      at each dispatch; an ``oauth`` credential fails the dispatch before anything starts;
    - a dispatch without a ``provider/model`` runs the default ``model`` of the operator's
      opencode config, passed as ``--model``; without one it fails before its run starts;
    - environment 1 is the daemon's without ``OPENCODE_CONFIG``, ``OPENCODE_CONFIG_DIR``,
      ``OPENCODE_AUTH_CONTENT`` and ``OPENCODE_DB``, and ``~/.claude/CLAUDE.md`` does not exist
      in the sandbox;
    - MCP discovery runs inside the same sandbox, under the same environment;
    - a run that bubblewrap could not set up fails at once, with an error naming bubblewrap's
      message and the fix (:func:`~.sandbox.setup_failure`), instead of being retried.

    The operator's provider blocks and default model come from ``opencode debug config`` run in a
    sandbox of their own (the operator's config folder read-only, its data and state folders
    replaced by ``opencode-homes/.discovery``'s, the working directory ``/``), once per binary
    for this provider's lifetime (concurrent dispatches wait for one discovery): a change to the
    operator's opencode config reaches workers after a daemon restart. A failure is logged and not
    kept; the dispatch then runs without a provider block. Values the CLI prints redacted
    (``***``) are left out of a block.
    """

    def __init__(self) -> None:
        self._mcp_names: dict[tuple[str, str | None, str | None], tuple[str, ...]] = {}
        # The operator's opencode config, read once per binary (see the class docstring).
        self._operator_configs: dict[str, _OperatorConfig] = {}
        self._operator_config_lock = asyncio.Lock()
        # The bubblewrap binaries whose missing sandbox this provider has already warned about.
        self._unsandboxed_warned: set[str] = set()

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
        # Never start a discovery once cancelled (the first attempt would not start either).
        if cancel_event is not None and cancel_event.is_set():
            return AgentResult(ok=False, error="cancelled", cancelled=True)
        try:
            launch = await self._launch(profile, cwd)
        except SandboxError as exc:
            return AgentResult(ok=False, error=str(exc))

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
        # have no "/" and are ignored so the opencode default is used). A sandboxed dispatch
        # without one names the operator's default model (see _launch).
        model_hint = launch.model
        if launch.model is not None:
            base_args += ["--model", launch.model]
        if profile.effort:
            base_args += ["--variant", profile.effort]
        if profile.cli_agent:
            base_args += ["--agent", profile.cli_agent]
        if profile.thinking:
            base_args += ["--thinking"]
        if profile.pure:
            base_args += ["--pure"]

        # The latest attempt's result, carrying the usage of every attempt so far: each run is paid
        # for, so whatever this dispatch returns counts them all (see _plus_usage).
        last_result: AgentResult | None = None
        resume_session_id: str | None = profile.resume_session_id
        for attempt_idx in range(_MAX_DISPATCH_ATTEMPTS):
            if attempt_idx > 0:
                await asyncio.sleep(_TRANSIENT_RETRY_DELAY_S + random.random() * 0.25)
            # Never start a run once cancelled, so a transient or empty run is not retried.
            if cancel_event is not None and cancel_event.is_set():
                return _plus_usage(
                    AgentResult(ok=False, error="cancelled", cancelled=True), last_result
                )
            attempt_args = list(base_args)
            if resume_session_id:
                attempt_args += ["--session", resume_session_id]
            exec_bin, exec_args = _resolve_cli_exec(_OPENCODE_BIN, attempt_args)
            argv = [*launch.wrap, exec_bin, *exec_args]
            try:
                proc = await asyncio.create_subprocess_exec(
                    argv[0],
                    *argv[1:],
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=launch.env,
                    cwd=str(cwd) if cwd is not None else None,
                    start_new_session=True,
                    **_WIN_SUBPROCESS_KW,
                )
            except FileNotFoundError:
                missing = launch.wrap[0] if launch.wrap else _OPENCODE_BIN
                return _plus_usage(
                    AgentResult(ok=False, error=f"CLI not found: {missing!r}"), last_result
                )
            except OSError as exc:  # e.g. not executable (EACCES) or no valid binary (ENOEXEC)
                return _plus_usage(
                    AgentResult(ok=False, error=f"CLI could not be started: {exc}"), last_result
                )

            async def drive(p: asyncio.subprocess.Process = proc) -> str:
                assert p.stdin is not None
                assert p.stdout is not None
                stdin = p.stdin
                stdout = p.stdout
                parts: list[str] = []

                async def _write_stdin() -> None:
                    # A CLI that exits without reading it all (one rejecting its arguments)
                    # closes the pipe: ignored, as asyncio's communicate() does, so its own
                    # output and exit code tell why.
                    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                        stdin.write(prompt.encode("utf-8"))
                        await stdin.drain()
                    stdin.close()

                async def _read_stdout() -> None:
                    # Read raw chunks, NOT readline(): opencode's NDJSON lines (a big file read or
                    # a long tool result) can exceed asyncio's default 64 KiB StreamReader line
                    # limit, which raises LimitOverrunError and aborts the whole dispatch.
                    # _parse_events splits the accumulated output on newlines, so chunk boundaries
                    # are harmless (mirrors the claude provider's raw-chunk read). One decoder
                    # reads every chunk, so a character split across two reads stays whole.
                    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                    while True:
                        chunk = await stdout.read(65536)
                        text = decoder.decode(chunk, final=not chunk)
                        if text:
                            parts.append(text)
                            if on_chunk is not None:
                                await on_chunk(text)
                        if not chunk:
                            break

                # Write stdin and read stdout concurrently so a chatty child cannot fill
                # the OS pipe buffer and deadlock (mirrors the claude provider, CON-3).
                await asyncio.gather(_write_stdin(), _read_stdout())
                await p.wait()
                return "".join(parts)

            drive_task = asyncio.create_task(drive())
            cancel_wait_task: asyncio.Task[None] | None = None
            try:
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
            except asyncio.CancelledError:
                # The task awaiting the dispatch was cancelled (a planning session's cancel, a
                # worker's stop, shutdown): stop the run as a cancel event does, then re-raise.
                await _kill_process_group(proc.pid)
                with contextlib.suppress(ProcessLookupError):
                    await proc.wait()
                await _cancel_tasks(drive_task, cancel_wait_task)
                raise

            if cancel_wait_task is not None and cancel_wait_task in done:
                await _kill_process_group(proc.pid)
                with contextlib.suppress(ProcessLookupError):
                    await proc.wait()
                drive_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await drive_task
                return _plus_usage(
                    AgentResult(ok=False, error="cancelled", cancelled=True), last_result
                )

            if drive_task in done:
                raw = drive_task.result()
                if cancel_wait_task is not None:
                    cancel_wait_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await cancel_wait_task
                if launch.wrap and proc.returncode != 0:
                    failure = setup_failure(raw)
                    if failure is not None:  # opencode never started: a re-run cannot help
                        return _plus_usage(
                            AgentResult(ok=False, error=failure, exit_code=proc.returncode),
                            last_result,
                        )
                last_result = _plus_usage(
                    _parse_events(raw, proc.returncode, model_hint=model_hint), last_result
                )
                # Neither a usage limit nor a resume of a session opencode no longer has gets
                # better on a quick re-run: the shared dispatch path waits for the reset, or
                # starts afresh, instead.
                if last_result.quota is not None or (
                    resume_session_id is not None
                    and not last_result.ok
                    and UNKNOWN_SESSION_RE.search(last_result.error or "")
                ):
                    return last_result
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
            return _plus_usage(
                AgentResult(ok=False, error=f"timed out after {timeout_seconds}s"), last_result
            )

        assert last_result is not None
        return last_result

    async def _launch(self, profile: ResolvedProfile, cwd: Path | None) -> _Launch:
        """How every attempt of a dispatch of *profile* in *cwd* starts: in the worker sandbox
        when the profile is isolated, its ``sandbox`` is not ``off`` and the sandbox is
        available, else exactly as without the sandbox. Raises :class:`SandboxError` when the
        dispatch must not start (see the class docstring)."""
        model = profile.model if profile.model and "/" in profile.model else None
        if profile.isolated and profile.sandbox != WorkerSandbox.off:
            status = await asyncio.to_thread(sandbox_status)
            if status.available:
                return await self._sandboxed_launch(profile, cwd, status.binary, model)
            if profile.sandbox == WorkerSandbox.required:
                raise SandboxError(required_message(status))
            if status.binary not in self._unsandboxed_warned:
                self._unsandboxed_warned.add(status.binary)
                logger.warning("%s", fallback_message(status))
        env = await self._worker_env(dict(os.environ), profile, cwd)
        return _Launch(env=env, model=model)

    async def _sandboxed_launch(
        self, profile: ResolvedProfile, cwd: Path | None, bwrap: str, model: str | None
    ) -> _Launch:
        """A dispatch in the worker sandbox of the bubblewrap binary *bwrap*. *model* is the
        profile's ``provider/model``, if any. Nothing starts before the CLI, the model and the
        credential are known to be usable."""
        daemon_env = dict(os.environ)
        search_path = {**daemon_env, **profile.extra_env}.get("PATH")
        if shutil.which(_OPENCODE_BIN, path=search_path) is None:
            raise SandboxError(f"CLI not found: {_OPENCODE_BIN!r}")
        operator = opencode_folders(daemon_env)
        homes = opencode_homes(daemon_env)
        config: _OperatorConfig | None = None
        if model is None:
            config = await self._operator_config(bwrap, operator, homes, daemon_env)
            model = config.model if config is not None else None
            if model is None:
                raise SandboxError(_NO_MODEL)
        provider = model.partition("/")[0]
        if not provider:
            raise SandboxError(
                f"The model {model!r} names no provider: set the agent's model to an opencode "
                "provider/model (e.g. deepseek/deepseek-flash)."
            )
        # Read at each dispatch, so a rotated key reaches the next one.
        credential = provider_credential(provider, operator_credentials(daemon_env, operator.data))
        if config is None:
            config = await self._operator_config(bwrap, operator, homes, daemon_env)
        block = config.providers.get(provider) if config is not None else None
        own = provider_folders(homes, provider)
        try:
            mounts = await asyncio.to_thread(
                _prepare_dispatch, operator, own, provider, block, credential, daemon_env
            )
        except OSError as exc:
            raise SandboxError(
                f"The worker sandbox of provider {provider!r} could not be prepared: {exc}"
            ) from exc
        wrap = bwrap_command(bwrap, mounts)
        base = {key: value for key, value in daemon_env.items() if key not in _OPERATOR_SETUP_ENV}
        env = await self._worker_env(base, profile, cwd, wrap=wrap, provider=provider)
        return _Launch(env=env, wrap=wrap, model=model)

    async def _worker_env(
        self,
        base: dict[str, str],
        profile: ResolvedProfile,
        cwd: Path | None,
        *,
        wrap: tuple[str, ...] = (),
        provider: str | None = None,
    ) -> dict[str, str]:
        """The environment of a dispatch, built as the class docstring describes from *base*
        (environment 1). *wrap* and *provider* are the sandbox command and the model provider of a
        sandboxed dispatch, for its MCP discovery."""
        env = dict(base)
        if profile.isolated:
            names = await self._mcp_servers(cwd, {**env, **profile.extra_env}, wrap, provider)
            env.update(_ISOLATION_ENV)
            if names:
                content = _disable_mcp(env.get(_CONFIG_CONTENT_ENV), names)
                if content is not None:
                    env[_CONFIG_CONTENT_ENV] = content
        env.update(profile.extra_env)  # last, so a profile can override any variable above
        return env

    async def _mcp_servers(
        self,
        cwd: Path | None,
        env: dict[str, str],
        wrap: tuple[str, ...] = (),
        provider: str | None = None,
    ) -> tuple[str, ...] | None:
        """The MCP server names of the opencode config a dispatch in *cwd* resolves: cached, or
        discovered under *env* (inside the sandbox command *wrap* of the model provider
        *provider*, for a sandboxed dispatch) and then cached. ``None`` (logged) when discovery
        fails."""
        key = (_OPENCODE_BIN, str(cwd) if cwd is not None else None, provider)
        cached = self._mcp_names.get(key)
        if cached is not None:
            return cached
        try:
            names = await _discover_mcp_servers(cwd, env, wrap)
        except _McpDiscoveryError as exc:
            logger.warning(
                "MCP discovery (opencode debug config) failed in %s: %s; this dispatch keeps its "
                "MCP servers",
                cwd if cwd is not None else os.getcwd(),
                exc,
            )
            return None
        self._mcp_names[key] = names
        while len(self._mcp_names) > _MCP_CACHE_MAX:
            del self._mcp_names[next(iter(self._mcp_names))]
        return names

    async def _operator_config(
        self, bwrap: str, operator: OpencodeFolders, homes: Path, daemon_env: dict[str, str]
    ) -> _OperatorConfig | None:
        """The provider blocks and default model of the operator's opencode config (the folders
        *operator*, the daemon's environment *daemon_env*): cached per binary, or read with
        ``opencode debug config`` in its own sandbox of *bwrap* (see the class docstring) and
        then cached. ``None`` (logged, not cached) when that fails. Concurrent dispatches wait
        for one discovery, which they then share."""
        async with self._operator_config_lock:
            cached = self._operator_configs.get(_OPENCODE_BIN)
            if cached is not None:
                return cached
            discovery = homes / DISCOVERY_FOLDER
            # The operator's own variables apply, but the session database is not opened.
            env = {key: value for key, value in daemon_env.items() if key != _SESSION_DB_ENV}
            try:
                await asyncio.to_thread(prepare_discovery_folders, discovery)
                wrap = bwrap_command(bwrap, discovery_mounts(operator, discovery))
                # The root folder is outside any repository: no project config is read.
                config = _operator_config_of(await _debug_config(Path("/"), env, wrap))
            except (OSError, _McpDiscoveryError) as exc:
                logger.warning(
                    "Reading the operator's opencode config (opencode debug config, in the worker "
                    "sandbox) failed: %s; this dispatch runs without its provider's config block",
                    exc,
                )
                return None
            self._operator_configs[_OPENCODE_BIN] = config
            return config


@dataclass(frozen=True)
class _Launch:
    """How each attempt of a dispatch starts: its environment, the sandbox command its opencode
    argv runs in (``()`` without the sandbox) and the model it names (``None``: opencode's own
    default)."""

    env: dict[str, str]
    wrap: tuple[str, ...] = ()
    model: str | None = None


@dataclass(frozen=True)
class _OperatorConfig:
    """What a sandboxed dispatch takes from the operator's opencode config."""

    providers: dict[str, dict[str, Any]]  # the provider blocks, redacted values left out
    model: str | None  # the default model ("provider/model"), if any


def _prepare_dispatch(
    operator: OpencodeFolders,
    own: OpencodeFolders,
    provider: str,
    block: dict[str, Any] | None,
    credential: dict[str, Any] | None,
    daemon_env: dict[str, str],
) -> list[str]:
    """Refresh the provider's own folders *own* and return the mounts of its dispatch (blocking
    file work, run in a thread)."""
    prepare_provider_folders(own, provider, block, credential)
    return dispatch_mounts(operator, own, home_folder(daemon_env))


class _McpDiscoveryError(Exception):
    """Why ``opencode debug config`` gave no usable config: the MCP servers of a dispatch's, or
    the operator's provider blocks and default model."""


async def _debug_config(cwd: Path | None, env: dict[str, str], wrap: Sequence[str] = ()) -> str:
    """The output of ``opencode debug config`` run in *cwd* under *env*, inside the sandbox
    command *wrap* when given. Raises :class:`_McpDiscoveryError` naming the cause when the CLI
    cannot start, runs longer than :data:`_MCP_DISCOVERY_TIMEOUT_S` or exits non-zero. The process
    group is killed on a timeout or a cancellation."""
    # The binary is read at call time, as for a dispatch, so an override applies at once.
    exec_bin, exec_args = _resolve_cli_exec(_OPENCODE_BIN, ["debug", "config"])
    argv = [*wrap, exec_bin, *exec_args]
    try:
        proc = await asyncio.create_subprocess_exec(
            argv[0],
            *argv[1:],
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(cwd) if cwd is not None else None,
            start_new_session=True,
            **_WIN_SUBPROCESS_KW,
        )
    except OSError as exc:
        program = wrap[0] if wrap else _OPENCODE_BIN
        raise _McpDiscoveryError(f"cannot start {program!r}: {exc}") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), _MCP_DISCOVERY_TIMEOUT_S)
    except TimeoutError as exc:
        raise _McpDiscoveryError(f"timed out after {_MCP_DISCOVERY_TIMEOUT_S:g}s") from exc
    finally:
        if proc.returncode is None:  # timed out or cancelled: leave no process behind
            await _kill_process_group(proc.pid)
            with contextlib.suppress(ProcessLookupError):
                await proc.wait()
    if proc.returncode != 0:
        detail = " ".join(_ANSI_ESCAPE_RE.sub("", err.decode("utf-8", errors="replace")).split())
        raise _McpDiscoveryError(
            f"exit {proc.returncode}" + (f": {detail[-_STDERR_TAIL_CHARS:]}" if detail else "")
        )
    return out.decode("utf-8", errors="replace")


async def _discover_mcp_servers(
    cwd: Path | None, env: dict[str, str], wrap: Sequence[str] = ()
) -> tuple[str, ...]:
    """The MCP server names ``opencode debug config`` reports when run in *cwd* under *env*,
    inside the sandbox command *wrap* when given (see :func:`_debug_config` and
    :func:`_mcp_server_names`, whose :class:`_McpDiscoveryError` it raises)."""
    return _mcp_server_names(await _debug_config(cwd, env, wrap))


def _config_object(text: str) -> dict[str, Any]:
    """The JSON object an ``opencode debug config`` output holds; else raises
    :class:`_McpDiscoveryError`."""
    try:
        config = json.loads(text)
    except ValueError as exc:
        raise _McpDiscoveryError(f"output is not JSON ({exc})") from exc
    if not isinstance(config, dict):
        raise _McpDiscoveryError("output is not a JSON object")
    return config


def _operator_config_of(text: str) -> _OperatorConfig:
    """The provider blocks and default model of the operator's ``opencode debug config`` output
    *text*. A provider entry that is no object, and a model that is no ``provider/model``, are
    ignored; values the CLI redacted are left out of each block (:func:`_unredacted`)."""
    config = _config_object(text)
    raw = config.get("provider")
    providers = (
        {name: _unredacted(block) for name, block in raw.items() if isinstance(block, dict)}
        if isinstance(raw, dict)
        else {}
    )
    model = config.get("model")
    return _OperatorConfig(
        providers=providers, model=model if isinstance(model, str) and "/" in model else None
    )


def _unredacted(value: Any) -> Any:
    """*value* without the object entries ``opencode debug config`` printed redacted."""
    if isinstance(value, dict):
        return {key: _unredacted(item) for key, item in value.items() if item != _REDACTED}
    if isinstance(value, list):
        return [_unredacted(item) for item in value]
    return value


def _mcp_server_names(text: str) -> tuple[str, ...]:
    """The sorted MCP server names of an ``opencode debug config`` output (``()`` without any).

    Only opencode 1.x entries are accepted: an object whose ``type`` is ``local`` or ``remote``,
    or that has a boolean ``enabled``. Anything else raises :class:`_McpDiscoveryError`, so a
    config format this provider does not know (such as servers under ``mcp.servers``) never
    yields an ``OPENCODE_CONFIG_CONTENT`` that opencode would reject.
    """
    servers = _config_object(text).get("mcp")
    if servers is None:
        return ()
    if not isinstance(servers, dict):
        raise _McpDiscoveryError("'mcp' is not an object")
    for name, entry in servers.items():
        if not isinstance(entry, dict) or not (
            entry.get("type") in ("local", "remote") or isinstance(entry.get("enabled"), bool)
        ):
            raise _McpDiscoveryError(f"unrecognized 'mcp' entry {name!r}")
    return tuple(sorted(servers))


def _disable_mcp(existing: str | None, names: tuple[str, ...]) -> str | None:
    """The ``OPENCODE_CONFIG_CONTENT`` that disables the MCP servers *names*: the daemon's own
    value *existing* (a JSON object, or unset or empty) with ``{"mcp": {name: {"enabled":
    false}}}`` merged over it. ``None``, with a warning, when *existing* is anything else: it is
    then left unchanged, since it cannot be merged into."""
    patch: dict[str, Any] = {"mcp": {name: {"enabled": False} for name in names}}
    if not existing:  # opencode treats an empty value as unset
        return json.dumps(patch)
    try:
        current = json.loads(existing)
    except ValueError:
        current = None
    if not isinstance(current, dict):
        logger.warning(
            "%s in the daemon's environment is not a JSON object; it is left unchanged, so this "
            "dispatch keeps its MCP servers",
            _CONFIG_CONTENT_ENV,
        )
        return None
    return json.dumps(_merged(current, patch))


def _merged(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """*base* with *patch* merged over it: nested objects are merged, any other value of *patch*
    replaces the one in *base*. Keys are kept exactly as they are."""
    merged = dict(base)
    for key, value in patch.items():
        current = merged.get(key)
        merged[key] = (
            _merged(current, value)
            if isinstance(current, dict) and isinstance(value, dict)
            else value
        )
    return merged


def _add(acc: int | None, value: object) -> int | None:
    """Sum integer token counts across steps, tolerating missing/None values."""
    if not isinstance(value, int):
        return acc
    return value if acc is None else acc + value


def _plus_usage(result: AgentResult, earlier: AgentResult | None) -> AgentResult:
    """*result* with the usage of the *earlier* attempts of its dispatch added: their cost,
    tokens, turns and tool calls. Everything else is *result*'s own. opencode reports each run's
    own usage (``cost_is_session_total`` false), so a dispatch that ran again used the sum."""
    if earlier is None:
        return result
    costs = [cost for cost in (earlier.cost_usd, result.cost_usd) if cost is not None]
    tools = dict(earlier.tool_breakdown)
    for name, count in result.tool_breakdown.items():
        tools[name] = tools.get(name, 0) + count
    return result.model_copy(
        update={
            "cost_usd": sum(costs) if costs else None,
            "num_turns": _add(earlier.num_turns, result.num_turns),
            "input_tokens": _add(earlier.input_tokens, result.input_tokens),
            "output_tokens": _add(earlier.output_tokens, result.output_tokens),
            "reasoning_tokens": _add(earlier.reasoning_tokens, result.reasoning_tokens),
            "cache_read_tokens": _add(earlier.cache_read_tokens, result.cache_read_tokens),
            "cache_creation_tokens": _add(
                earlier.cache_creation_tokens, result.cache_creation_tokens
            ),
            "tool_calls_count": earlier.tool_calls_count + result.tool_calls_count,
            "tool_breakdown": tools,
        }
    )


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return float(value.strip())
    return None


def _retry_after(headers: object, now: datetime) -> datetime | None:
    """The time a 429's ``retry-after-ms`` (milliseconds) or ``retry-after`` (seconds, or an HTTP
    date) response header says to retry at, else ``None``. Header names are case-insensitive. A
    value beyond what a datetime holds (an infinite or absurd delay) counts as absent."""
    if not isinstance(headers, dict):
        return None
    lowered = {str(name).lower(): value for name, value in headers.items()}
    millis = _as_float(lowered.get("retry-after-ms"))
    if millis is not None and millis >= 0:
        with contextlib.suppress(OverflowError, ValueError):
            return now + timedelta(milliseconds=millis)
    raw = lowered.get("retry-after")
    seconds = _as_float(raw)
    if seconds is not None and seconds >= 0:
        with contextlib.suppress(OverflowError, ValueError):
            return now + timedelta(seconds=seconds)
    if isinstance(raw, str):
        with contextlib.suppress(TypeError, ValueError, IndexError, OverflowError):
            when = email.utils.parsedate_to_datetime(raw)
            return (when if when.tzinfo is not None else when.replace(tzinfo=UTC)).astimezone(UTC)
    return None


def _quota_error(data: dict[str, Any], message: str, now: datetime) -> QuotaExhausted | None:
    """The quota refusal an ``error`` event reports (its ``data``: ``statusCode``,
    ``responseHeaders``), or ``None``. A balance or quota that ran out has no reset time."""
    status = data.get("statusCode")
    if status == 402 or _BALANCE_ERROR_RE.search(message):
        return QuotaExhausted(kind="billing", message=message)
    if status == 429 or _RATE_LIMIT_ERROR_RE.search(message):
        return QuotaExhausted(
            kind="rate_limit",
            resets_at=_retry_after(data.get("responseHeaders"), now),
            message=message,
        )
    return None


def _parse_events(
    raw: str,
    exit_code: int | None,
    model_hint: str | None = None,
    now: datetime | None = None,
) -> AgentResult:
    """Parse opencode's NDJSON event stream into an :class:`AgentResult`.

    Concatenates every ``text`` event's ``part.text`` for the result, and sums
    ``step_finish`` events' ``part.cost`` and ``part.tokens`` for usage. Malformed
    lines are skipped so partial/garbled output never crashes a dispatch. An ``error`` event
    that refuses the request for a usage limit or balance sets ``quota``; a ``retry-after``
    reset is counted from *now* (the current time when omitted).

    A failed run always says why: its ``error`` events, else the end of the CLI's own output
    that is no event (a config error in plain text), else its exit code. Lines end at a newline
    only: JSON leaves U+2028, U+2029 and U+0085 raw inside strings.
    """
    now = now if now is not None else datetime.now(UTC)
    quota: QuotaExhausted | None = None
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
    plain_lines: list[str] = []  # output that is no JSON event: the CLI's own messages

    for line in raw.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            plain_lines.append(line)
            if _TRANSIENT_API_ERROR_RE.search(line) or UNKNOWN_SESSION_RE.search(line):
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
                data = data_obj if isinstance(data_obj, dict) else {}
                msg = (
                    data.get("message")
                    or raw_err.get("message")
                    or raw_err.get("name")
                    or json.dumps(raw_err)
                )
                error_parts.append(str(msg))
                quota = _quota_error(data, str(msg), now) or quota
            elif raw_err is not None:
                error_parts.append(str(raw_err))
                quota = _quota_error({}, str(raw_err), now) or quota
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
        and quota is None
    )
    error = error_str if not ok or has_transient_err else None
    if not ok and not error:
        error = "\n".join(plain_lines)[-500:] or f"opencode exited with code {exit_code}"
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
        error=error,
        session_id=session_id,
        reasoning_tokens=reasoning_tok,
        tool_calls_count=tool_calls_count,
        tool_breakdown=tool_breakdown,
        messages=messages,
        quota=quota,
    )
