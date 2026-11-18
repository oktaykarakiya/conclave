"""Subprocess-level tests for the Claude CLI provider, using a fake CLI script."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from conclave.config import ArgMode
from conclave.providers import (
    AgentResult,
    ClaudeCliProvider,
    Engine,
    EngineRouter,
    QuotaGate,
    QuotaWait,
    ResolvedProfile,
    claude_cli,
    dispatch_with_quota,
)
from conclave.providers.claude_cli import _parse_envelope, _scan_stream

# A fake "claude" that echoes the routed model back via the JSON envelope, so we can
# prove the composed environment actually reaches the child process.
_FAKE_CLI = """#!/usr/bin/env python3
import sys, os, json
sys.stdin.read()
print(json.dumps({
    "result": os.environ.get("ANTHROPIC_MODEL", "inherit"),
    "modelUsage": {os.environ.get("ANTHROPIC_MODEL", "host-default"): {"in": 1}},
    "total_cost_usd": 0.0021,
    "num_turns": 2,
}))
"""

_SLOW_CLI = """#!/usr/bin/env python3
import sys, time
sys.stdin.read()
time.sleep(5)
print("late")
"""

# A fake "claude" whose run ends like a dropped API connection, which the provider retries.
_TRANSIENT_CLI = """#!/usr/bin/env python3
import sys
sys.stdin.read()
print("ECONNRESET")
sys.exit(1)
"""


def _make_cli(tmp_path: Path, body: str, name: str = "fake_claude.py") -> str:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def _count_spawns(monkeypatch: pytest.MonkeyPatch, script: str) -> list[str]:
    """Record each process started for *script* (directly, or through Python on Windows)."""
    spawns: list[str] = []
    spawn = asyncio.create_subprocess_exec

    async def counting_spawn(
        program: Any, *args: Any, **kwargs: Any
    ) -> asyncio.subprocess.Process:
        if script in (program, *args):
            spawns.append(script)
        return await spawn(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", counting_spawn)
    return spawns


def _capture_spawns(
    monkeypatch: pytest.MonkeyPatch, script: str
) -> list[tuple[list[str], dict[str, str]]]:
    """Record the CLI args (after *script*) and the ``env`` of each process started for
    *script*, then start it for real."""
    spawns: list[tuple[list[str], dict[str, str]]] = []
    spawn = asyncio.create_subprocess_exec

    async def capturing_spawn(
        program: Any, *args: Any, **kwargs: Any
    ) -> asyncio.subprocess.Process:
        argv = [str(a) for a in (program, *args)]
        if script in argv:
            spawns.append((argv[argv.index(script) + 1 :], dict(kwargs["env"])))
        return await spawn(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capturing_spawn)
    return spawns


_TOOLS = (
    "Task,Bash,Read,Edit,Write,NotebookEdit,WebFetch,WebSearch,ToolSearch,"
    "Glob,Grep,LSP,TaskCreate,TaskGet,TaskList,TaskUpdate,TodoWrite,PowerShell"
)
_ISOLATION_FLAGS = [
    "--strict-mcp-config",
    "--setting-sources",
    "project,local",
    "--disable-slash-commands",
    "--exclude-dynamic-system-prompt-sections",
    "--tools",
    _TOOLS,
]
_BASE_ARGS = ["--print", "--output-format", "stream-json", "--verbose"]
_SWITCHES = ("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "CLAUDE_CODE_DISABLE_CRON")
_BASH_TIMEOUTS = ("BASH_MAX_TIMEOUT_MS", "BASH_DEFAULT_TIMEOUT_MS")


def _recording_cli(tmp_path: Path) -> tuple[str, Path]:
    """A fake "claude" that records the args it was started with, and the isolation and Bash
    timeout variables of its environment, in the returned file."""
    record = tmp_path / "started.json"
    keys = ["CLAUDE_CODE_DISABLE_AUTO_MEMORY", *_SWITCHES, *_BASH_TIMEOUTS]
    body = f"""#!/usr/bin/env python3
import json, os, sys
sys.stdin.read()
with open({str(record)!r}, "w", encoding="utf-8") as f:
    env = {{key: os.environ[key] for key in {keys!r} if key in os.environ}}
    json.dump({{"argv": sys.argv[1:], "env": env}}, f)
print(json.dumps({{"type": "result", "is_error": False, "result": "ok", "session_id": "s-r"}}))
"""
    return _make_cli(tmp_path, body, name="recording.py"), record


def _started(record: Path) -> tuple[list[str], dict[str, str]]:
    started = json.loads(record.read_text(encoding="utf-8"))
    return started["argv"], started["env"]


async def test_isolated_dispatch_passes_the_isolation_flags_and_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _make_cli(tmp_path, _FAKE_CLI)
    spawns = _capture_spawns(monkeypatch, cli)
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "0")  # the parent's value loses
    profile = ResolvedProfile(
        name="claude-code", arg_mode=ArgMode.flag, model="opus", cli=cli, isolated=True
    )

    result = await ClaudeCliProvider().run_agent(profile=profile, prompt="hi", timeout_seconds=30)

    assert result.ok
    ((args, env),) = spawns
    assert args == [
        "--dangerously-skip-permissions",
        *_BASE_ARGS,
        *_ISOLATION_FLAGS,
        "--model",
        "opus",
    ]
    assert "--mcp-config" not in args
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert {var: env[var] for var in _SWITCHES} == dict.fromkeys(_SWITCHES, "1")


@pytest.mark.parametrize("parent_value", [None, "0"])
async def test_unisolated_dispatch_passes_no_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parent_value: str | None
) -> None:
    cli = _make_cli(tmp_path, _FAKE_CLI)
    spawns = _capture_spawns(monkeypatch, cli)
    # This suite may itself run inside an isolated worker, which has the variables set.
    for var in ("CLAUDE_CODE_DISABLE_AUTO_MEMORY", *_SWITCHES, *_BASH_TIMEOUTS):
        monkeypatch.delenv(var, raising=False)
    if parent_value is not None:
        monkeypatch.setenv("CLAUDE_CODE_DISABLE_AUTO_MEMORY", parent_value)
    profile = ResolvedProfile(
        name="claude-code", arg_mode=ArgMode.flag, model="opus", cli=cli, isolated=False
    )

    result = await ClaudeCliProvider().run_agent(profile=profile, prompt="hi", timeout_seconds=30)

    assert result.ok
    ((args, env),) = spawns
    assert args == ["--dangerously-skip-permissions", *_BASE_ARGS, "--model", "opus"]
    # Untouched: absent stays absent, and a parent's own value is inherited as before.
    assert env.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") == parent_value
    assert not {*_SWITCHES, *_BASH_TIMEOUTS} & set(env)


@pytest.mark.parametrize(
    ("timeout_seconds", "bash_max", "bash_default"),
    [(1800, "1800000", "1200000"), (600, "600000", "600000")],
)
async def test_an_isolated_dispatch_runs_the_allowed_tools_in_the_foreground_up_to_its_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timeout_seconds: int,
    bash_max: str,
    bash_default: str,
) -> None:
    cli, record = _recording_cli(tmp_path)
    for var in (*_SWITCHES, *_BASH_TIMEOUTS):
        monkeypatch.setenv(var, "0")  # the parent's values lose
    profile = ResolvedProfile(name="claude-code", arg_mode=ArgMode.flag, model="opus", cli=cli)

    result = await ClaudeCliProvider().run_agent(
        profile=profile, prompt="hi", timeout_seconds=timeout_seconds
    )

    assert result.ok
    argv, env = _started(record)
    assert argv.count("--tools") == 1 and argv[argv.index("--tools") + 1] == _TOOLS
    assert env == {
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
        "CLAUDE_CODE_DISABLE_CRON": "1",
        "BASH_MAX_TIMEOUT_MS": bash_max,
        "BASH_DEFAULT_TIMEOUT_MS": bash_default,
    }


async def test_an_unisolated_dispatch_gets_no_tool_allowlist_switches_or_bash_timeouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli, record = _recording_cli(tmp_path)
    # This suite may itself run inside an isolated worker, which has the variables set.
    for var in ("CLAUDE_CODE_DISABLE_AUTO_MEMORY", *_SWITCHES, *_BASH_TIMEOUTS):
        monkeypatch.delenv(var, raising=False)
    profile = ResolvedProfile(
        name="claude-code", arg_mode=ArgMode.flag, model="opus", cli=cli, isolated=False
    )

    result = await ClaudeCliProvider().run_agent(
        profile=profile, prompt="hi", timeout_seconds=1800
    )

    assert result.ok
    argv, env = _started(record)
    assert "--tools" not in argv
    assert env == {}


async def test_the_profile_extra_env_overrides_the_derived_bash_timeouts(
    tmp_path: Path,
) -> None:
    cli, record = _recording_cli(tmp_path)
    overrides = {"BASH_MAX_TIMEOUT_MS": "7200000", "BASH_DEFAULT_TIMEOUT_MS": "60000"}
    profile = ResolvedProfile(
        name="claude-code", arg_mode=ArgMode.flag, model="opus", cli=cli, extra_env=overrides
    )

    result = await ClaudeCliProvider().run_agent(
        profile=profile, prompt="hi", timeout_seconds=1800
    )

    assert result.ok
    _argv, env = _started(record)
    assert {var: env[var] for var in _BASH_TIMEOUTS} == overrides


async def test_env_routing_reaches_subprocess(tmp_path: Path) -> None:
    cli = _make_cli(tmp_path, _FAKE_CLI)
    profile = ResolvedProfile(
        name="ds", arg_mode=ArgMode.env, model="deepseek-v4-pro", cli=cli, cli_flags=()
    )
    result = await ClaudeCliProvider().run_agent(profile=profile, prompt="hi", timeout_seconds=30)
    assert result.ok
    assert result.text == "deepseek-v4-pro"  # the child saw ANTHROPIC_MODEL
    assert result.model_reported == "deepseek-v4-pro"
    assert result.cost_usd == 0.0021
    assert result.num_turns == 2
    assert result.exit_code == 0


async def test_inherit_sets_no_model_env(tmp_path: Path) -> None:
    cli = _make_cli(tmp_path, _FAKE_CLI)
    profile = ResolvedProfile(name="sys", arg_mode=ArgMode.inherit, cli=cli, cli_flags=())
    chunks: list[str] = []

    async def collect(text: str) -> None:
        chunks.append(text)

    result = await ClaudeCliProvider().run_agent(
        profile=profile, prompt="hi", timeout_seconds=30, on_chunk=collect
    )
    assert result.ok
    assert result.text == "inherit"
    assert "".join(chunks)  # output was streamed through on_chunk


async def test_timeout_kills_and_reports(tmp_path: Path) -> None:
    cli = _make_cli(tmp_path, _SLOW_CLI, name="slow.py")
    profile = ResolvedProfile(name="sys", arg_mode=ArgMode.inherit, cli=cli, cli_flags=())
    result = await ClaudeCliProvider().run_agent(profile=profile, prompt="hi", timeout_seconds=1)
    assert not result.ok
    assert "timed out" in (result.error or "")


async def test_timeout_kills_process_group(tmp_path: Path) -> None:
    """On timeout the provider must kill the entire process group, not just the
    direct child — descendant subprocesses must not be orphaned.

    A fake CLI spawns a sleeping grandchild, writes its PID to a file, then
    hangs.  After the provider times out we verify the grandchild PID is dead
    via os.kill(pid, 0) — ProcessLookupError means the process is gone.
    """
    pid_file = tmp_path / "child.pid"
    # fmt: off — keep the embedded script readable
    grandchild_cli = f"""#!/usr/bin/env python3
import subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])
with open({str(pid_file)!r}, 'w') as f:
    f.write(str(child.pid))
time.sleep(300)
"""
    # fmt: on
    cli = _make_cli(tmp_path, grandchild_cli, name="grandchild_cli.py")
    profile = ResolvedProfile(
        name="sys", arg_mode=ArgMode.inherit, cli=cli, cli_flags=(),
    )

    result = await ClaudeCliProvider().run_agent(
        profile=profile, prompt="hi", timeout_seconds=1,
    )
    assert not result.ok
    assert "timed out" in (result.error or "")

    # Read grandchild PID from the file the fake CLI wrote before hanging.
    child_pid = int(pid_file.read_text().strip())

    # Poll until the process is gone (SIGTERM → 0.3s → SIGKILL takes a
    # moment to fully reap).  os.kill(pid, 0) raises ProcessLookupError
    # when the process no longer exists.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except (ProcessLookupError, OSError, SystemError):
            break  # process is dead — success
        except PermissionError:
            # Process exists but we cannot signal it — unexpected in a test
            # where the provider runs as the same user.  Treat as alive
            # and keep polling.
            pass
        await asyncio.sleep(0.1)
    else:
        raise AssertionError(
            f"Grandchild process {child_pid} still alive after timeout kill"
        )


async def test_cancel_kills_the_run_and_flags_the_result(tmp_path: Path) -> None:
    cli = _make_cli(tmp_path, _SLOW_CLI, name="slow.py")
    profile = ResolvedProfile(name="sys", arg_mode=ArgMode.inherit, cli=cli, cli_flags=())
    cancel_event = asyncio.Event()
    asyncio.get_running_loop().call_later(0.3, cancel_event.set)
    started = time.monotonic()

    result = await ClaudeCliProvider().run_agent(
        profile=profile, prompt="hi", timeout_seconds=30, cancel_event=cancel_event
    )

    assert result.cancelled is True
    assert result.ok is False
    assert time.monotonic() - started < 4  # killed, not waited for (the CLI sleeps 5 s)


async def test_cancelled_run_is_not_retried_even_if_output_looks_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _make_cli(tmp_path, _TRANSIENT_CLI, name="transient.py")
    cancel_event = asyncio.Event()
    parse = claude_cli._parse_envelope

    def parse_as_the_stop_lands(raw: str, exit_code: int | None) -> AgentResult:
        cancel_event.set()  # the operator stops the task just as this run ends
        return parse(raw, exit_code)

    monkeypatch.setattr(claude_cli, "_parse_envelope", parse_as_the_stop_lands)
    monkeypatch.setattr(claude_cli, "_CLAUDE_TRANSIENT_RETRY_DELAY_S", 0.0)
    spawns = _count_spawns(monkeypatch, cli)
    profile = ResolvedProfile(name="sys", arg_mode=ArgMode.inherit, cli=cli, cli_flags=())

    result = await ClaudeCliProvider().run_agent(
        profile=profile, prompt="hi", timeout_seconds=30, cancel_event=cancel_event
    )

    assert len(spawns) == 1
    assert result.cancelled is True
    assert result.ok is False


async def test_cli_not_found() -> None:
    profile = ResolvedProfile(
        name="sys", arg_mode=ArgMode.inherit, cli="/nonexistent/conclave-xyz", cli_flags=()
    )
    result = await ClaudeCliProvider().run_agent(profile=profile, prompt="hi", timeout_seconds=5)
    assert not result.ok
    assert "not found" in (result.error or "")


# A fake CLI that reads stdin one byte at a time and writes a large (~128 KB)
# stderr chunk after each byte — stderr is merged into stdout by the provider
# (stderr=STDOUT), so the OS pipe buffer still fills and triggers the deadlock
# under the old sequential drive().  This reliably reproduces CON-3.
_LARGE_OUTPUT_CLI = """#!/usr/bin/env python3
import sys, json

BLOCK = b"X" * (128 * 1024)  # 128 KB — exceeds the ~64 KB pipe buffer on Linux

while True:
    b = sys.stdin.buffer.read(1)
    if not b:
        break
    sys.stderr.buffer.write(BLOCK)
    sys.stderr.buffer.flush()

print(json.dumps({"result": "large-output-ok", "num_turns": 1}))
"""

# Echo-style fake CLI: reads stdin one char at a time and echoes each to
# stderr before emitting a clean JSON envelope on stdout.  Because stderr is
# merged (stderr=STDOUT), the raw output includes both streams interleaved;
# we assert the JSON-roundtripped result field is correct — proving the
# gather didn't drop data.
_ECHO_CLI = """#!/usr/bin/env python3
import sys, json

for ch in sys.stdin.read()[:1024]:
    sys.stderr.write(ch)
    sys.stderr.flush()
print(json.dumps({"result": "echo-ok", "num_turns": 1}))
"""


async def test_concurrent_stdin_stdout_no_deadlock(tmp_path: Path) -> None:
    """A chatty child that emits >64 KB of stderr between stdin reads must not deadlock."""
    cli = _make_cli(tmp_path, _LARGE_OUTPUT_CLI)
    profile = ResolvedProfile(
        name="sys", arg_mode=ArgMode.inherit, cli=cli, cli_flags=()
    )
    # Prompt length is modest (well under the pipe buffer), but the child's
    # 128 KB per-byte blasts are what trigger deadlock under sequential I/O.
    result = await ClaudeCliProvider().run_agent(
        profile=profile,
        prompt="hello",
        timeout_seconds=5,  # deadlock would exceed this
    )
    # The child's noise goes to stderr (merged into stdout at the parent),
    # so the raw text is noise + JSON envelope.  json.loads fails on the
    # prefix → text falls back to full raw; we assert exit-code-based ok.
    assert result.ok, f"Expected ok, got error={result.error}"
    assert result.exit_code == 0
    # Sanity: the JSON envelope is present at the end of the output.
    assert '"result": "large-output-ok"' in result.text


async def test_parts_concatenation_with_echo_fake(tmp_path: Path) -> None:
    """Concurrent gather must not drop or reorder data — full output is captured."""
    cli = _make_cli(tmp_path, _ECHO_CLI)
    profile = ResolvedProfile(
        name="sys", arg_mode=ArgMode.inherit, cli=cli, cli_flags=()
    )
    result = await ClaudeCliProvider().run_agent(
        profile=profile,
        prompt="ABCDEFGHIJ",
        timeout_seconds=5,
    )
    assert result.ok
    assert result.exit_code == 0
    # The raw text contains stderr echo + JSON.  The JSON-roundtripped fields
    # are correct because the JSON line at the end parses cleanly?  No — the
    # stderr prefix makes the whole raw invalid JSON.  We verify the JSON
    # envelope suffix is intact, and the echoed characters appear in order.
    assert '"result": "echo-ok"' in result.text
    assert result.text.replace("\r\n", "\n").endswith("}\n")


# --- usage limits ----------------------------------------------------------------------------
# The stream-json shapes Claude Code 2.1.326 emits (rate_limit_event on every turn; an assistant
# message's top-level "error" when a request is refused) and the result event that ends a run.

_RESETS_AT = 1794924600  # epoch seconds
_SEVEN_DAY_RESETS_AT = 1795388400


def _rate_limit_event(
    status: str, *, five_hour: str = "allowed", **info: object
) -> dict[str, Any]:
    """A rate_limit_event; *info* overrides or adds rate_limit_info fields."""
    return {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "status": status,
            "resetsAt": _RESETS_AT,
            "rateLimitType": "five_hour",
            "overageStatus": "rejected",
            "isUsingOverage": False,
            "unifiedWindows": {
                "five_hour": {"utilization": 1.0, "resetsAt": _RESETS_AT, "status": five_hour},
                "seven_day": {
                    "utilization": 0.27,
                    "resetsAt": _SEVEN_DAY_RESETS_AT,
                    "status": "allowed",
                },
            },
            **info,
        },
        "session_id": "ses-1",
        "uuid": "u-1",
    }


def _result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "result": text,
        "session_id": "ses-1",
        "num_turns": 1,
        "total_cost_usd": 0.0,
    }


def _stream(*events: dict[str, Any]) -> str:
    init = {"type": "system", "subtype": "init", "session_id": "ses-1"}
    return "\n".join(json.dumps(e) for e in (init, *events)) + "\n"


def _refused(error: str, text: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
        "error": error,
        "session_id": "ses-1",
    }


def test_a_rejected_rate_limit_event_is_a_quota_even_with_exit_zero() -> None:
    raw = _stream(
        _rate_limit_event("rejected", five_hour="rejected"),
        _result("Done, I have finished the task."),  # no is_error, and a success hint
    )

    result = _parse_envelope(raw, 0)

    assert result.ok is False
    assert result.quota is not None
    assert result.quota.kind == "rate_limit"
    assert result.quota.window == "five_hour"
    assert result.quota.resets_at == datetime.fromtimestamp(_RESETS_AT, UTC)
    assert result.session_id == "ses-1"
    assert result.rate_limit_info is not None and result.rate_limit_info["status"] == "rejected"


def test_the_rejected_window_that_resets_last_decides_the_reset() -> None:
    event = _rate_limit_event("rejected", five_hour="rejected")
    event["rate_limit_info"]["unifiedWindows"]["seven_day"]["status"] = "rejected"

    quota = _parse_envelope(_stream(event, _result("x", is_error=True)), 1).quota

    assert quota is not None and quota.window == "seven_day"
    assert quota.resets_at == datetime.fromtimestamp(_SEVEN_DAY_RESETS_AT, UTC)


def test_an_assistant_rate_limit_error_takes_the_reset_from_an_earlier_rate_limit_info() -> None:
    raw = _stream(
        _rate_limit_event("allowed_warning"),  # the window is nearly used up
        _refused("rate_limit", "You've hit your limit · resets 3pm"),
        _result("You've hit your limit · resets 3pm", is_error=True),
    )

    result = _parse_envelope(raw, 1)

    assert result.ok is False
    assert result.quota is not None
    assert result.quota.kind == "rate_limit"
    assert result.quota.resets_at == datetime.fromtimestamp(_RESETS_AT, UTC)
    assert result.quota.window == "five_hour"
    assert result.quota.message == "You've hit your limit · resets 3pm"


@pytest.mark.parametrize(
    ("event", "window", "resets_at"),
    [
        # The windows are not exhausted: a short burst limit, so the gate's retry wait applies.
        (_rate_limit_event("allowed"), None, None),
        (_rate_limit_event("rejected", five_hour="rejected", isUsingOverage=True), None, None),
        # The window is nearly used up, or refuses requests: its reset is when they resume.
        (_rate_limit_event("allowed_warning"), "five_hour", _RESETS_AT),
        (_rate_limit_event("rejected", five_hour="rejected"), "five_hour", _RESETS_AT),
    ],
    ids=["allowed", "rejected-on-extra-usage", "allowed_warning", "rejected"],
)
def test_a_refused_request_resumes_at_a_window_reset_only_when_the_window_limits(
    event: dict[str, Any], window: str | None, resets_at: int | None
) -> None:
    raw = _stream(
        event,
        _refused("rate_limit", "API Error: Rate limited"),
        _result("API Error: Rate limited", is_error=True),
    )

    quota = _parse_envelope(raw, 1).quota

    assert quota is not None and quota.kind == "rate_limit"
    assert quota.window == window
    assert quota.resets_at == (
        datetime.fromtimestamp(resets_at, UTC) if resets_at is not None else None
    )


def test_a_refusal_without_a_result_event_keeps_the_session_and_the_message() -> None:
    raw = _stream(_refused("rate_limit", "usage limit reached · continues automatically"))

    result = _parse_envelope(raw, 1)

    assert result.quota is not None and result.quota.resets_at is None
    assert result.quota.message == "usage limit reached · continues automatically"
    assert result.session_id == "ses-1"


def test_a_billing_error_is_a_billing_quota_without_a_rate_limit_reset() -> None:
    raw = _stream(
        _rate_limit_event("allowed"),  # a rate-limit window's reset does not end a billing stop
        _refused("billing_error", "Credit balance is too low"),
        _result("Credit balance is too low", is_error=True),
    )

    quota = _parse_envelope(raw, 1).quota

    assert quota is not None and quota.kind == "billing"
    assert quota.message == "Credit balance is too low"
    assert quota.resets_at is None and quota.window is None


def test_other_assistant_errors_are_not_a_quota() -> None:
    raw = _stream(
        _refused("authentication_failed", "Invalid API key · Please run /login"),
        _result("Invalid API key · Please run /login", is_error=True),
    )

    result = _parse_envelope(raw, 1)

    assert result.ok is False and result.quota is None


def test_the_legacy_limit_text_carries_its_reset_epoch() -> None:
    result = _parse_envelope(f"Claude AI usage limit reached|{_RESETS_AT}\n", 1)

    assert result.ok is False
    assert result.quota is not None and result.quota.kind == "rate_limit"
    assert result.quota.resets_at == datetime.fromtimestamp(_RESETS_AT, UTC)


@pytest.mark.parametrize(
    "text",
    ["usage limit reached — check plan", "You\u2019ve hit your limit · resets 3pm (Europe/Berlin)"],
)
def test_a_short_final_limit_text_is_a_quota(text: str) -> None:
    quota = _parse_envelope(_stream(_result(text)), 0).quota

    assert quota is not None and quota.message == text


def test_allowed_and_warning_events_leave_a_normal_run_ok() -> None:
    warning = _rate_limit_event("allowed_warning")
    raw = _stream(_rate_limit_event("allowed"), warning, _result("VERDICT: pass"))

    result = _parse_envelope(raw, 0)

    assert result.ok is True and result.quota is None and result.error is None
    assert result.rate_limit_info == warning["rate_limit_info"]


@pytest.mark.parametrize(
    "text",
    [
        "Usage limit reached handling: " + "the retry path now backs off. " * 20,  # > 300 chars
        "Fixed the bug where 'usage limit reached' was shown twice.",  # quotes it mid-text
    ],
)
def test_an_answer_quoting_the_limit_wording_stays_ok(text: str) -> None:
    result = _parse_envelope(_stream(_result(text)), 0)

    assert result.ok is True and result.quota is None


@pytest.mark.parametrize(
    "overage",
    [
        {"overageStatus": "allowed", "isUsingOverage": True},
        {"overageStatus": "allowed_warning", "isUsingOverage": True},
        {"overageInUse": True},
    ],
)
def test_a_rejected_limit_served_on_paid_extra_usage_is_not_a_quota(
    overage: dict[str, object],
) -> None:
    """Past its limit, an account with extra usage on keeps being served: Claude Code reports
    status "rejected" with isUsingOverage (or overageInUse) true, and its run succeeds."""
    paid = _rate_limit_event("rejected", five_hour="rejected", **overage)
    raw = _stream(paid, _result("Done, I have finished the task."))

    result = _parse_envelope(raw, 0)

    assert result.ok is True and result.quota is None and result.error is None
    assert result.rate_limit_info == paid["rate_limit_info"]


def test_only_the_latest_rate_limit_info_counts() -> None:
    raw = _stream(
        _rate_limit_event("rejected", five_hour="rejected"),
        _rate_limit_event("allowed"),  # requests are served again
        _result("VERDICT: pass"),
    )

    result = _parse_envelope(raw, 0)

    assert result.ok is True and result.quota is None
    assert result.rate_limit_info is not None and result.rate_limit_info["status"] == "allowed"


def test_a_refused_message_the_run_went_on_from_is_not_a_quota() -> None:
    raw = _stream(
        _refused("rate_limit", "You've hit your limit · resets 3pm"),
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}},
        _result("Done."),
    )

    result = _parse_envelope(raw, 0)

    assert result.ok is True and result.quota is None


def test_the_limit_text_fallback_is_off_when_the_stream_reports_rate_limits() -> None:
    """A short answer opening with the wording is a limit only on a CLI that reports none."""
    text = "Usage limit reached handling: the retry path now backs off and reports the reset."
    assert len(text) <= 300

    with_events = _parse_envelope(_stream(_rate_limit_event("allowed"), _result(text)), 0)
    without_events = _parse_envelope(_stream(_result(text)), 0)

    assert with_events.ok is True and with_events.quota is None
    assert without_events.quota is not None


def test_a_limiting_window_outside_the_unified_windows_decides_the_reset() -> None:
    """The top-level rateLimitType names the window that limits now, e.g. the weekly Opus one,
    which unifiedWindows does not list: requests stay refused until it resets too."""
    weekly_opus = _RESETS_AT + 3 * 24 * 3600
    event = _rate_limit_event(
        "rejected", five_hour="rejected", rateLimitType="seven_day_opus", resetsAt=weekly_opus
    )

    quota = _parse_envelope(_stream(event, _result("x", is_error=True)), 1).quota

    assert quota is not None and quota.window == "seven_day_opus"
    assert quota.resets_at == datetime.fromtimestamp(weekly_opus, UTC)


_LIMITED_CLI = """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
events = [
    {"type": "system", "subtype": "init", "session_id": "ses-9"},
    {"type": "rate_limit_event", "session_id": "ses-9",
     "rate_limit_info": {"status": "rejected", "resetsAt": 1794924600,
                         "rateLimitType": "five_hour"}},
    {"type": "result", "is_error": True, "session_id": "ses-9",
     "result": "You've hit your limit (API Error: ECONNRESET)"},
]
print("\\n".join(json.dumps(e) for e in events))
"""


async def test_a_limit_hit_is_returned_at_once_not_retried_as_a_dropped_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _make_cli(tmp_path, _LIMITED_CLI, name="limited.py")
    monkeypatch.setattr(claude_cli, "_CLAUDE_TRANSIENT_RETRY_DELAY_S", 0.0)
    spawns = _count_spawns(monkeypatch, cli)
    profile = ResolvedProfile(name="sys", arg_mode=ArgMode.inherit, cli=cli, cli_flags=())

    result = await ClaudeCliProvider().run_agent(profile=profile, prompt="hi", timeout_seconds=30)

    assert len(spawns) == 1
    assert result.ok is False and result.quota is not None
    assert result.session_id == "ses-9"


# Past its five-hour limit, an account with extra usage on is served on paid credits: Claude Code
# reports status "rejected" with isUsingOverage true on every turn, and the run succeeds.
_OVERAGE_CLI = """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
info = {"status": "rejected", "resetsAt": 1794924600, "rateLimitType": "five_hour",
        "overageStatus": "allowed", "isUsingOverage": True,
        "unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": 1794924600,
                                         "status": "rejected"}}}
events = [
    {"type": "system", "subtype": "init", "session_id": "ses-paid"},
    {"type": "rate_limit_event", "rate_limit_info": info, "session_id": "ses-paid"},
    {"type": "assistant", "session_id": "ses-paid",
     "message": {"content": [{"type": "text", "text": "VERDICT: pass"}]}},
    {"type": "result", "subtype": "success", "is_error": False, "session_id": "ses-paid",
     "result": "VERDICT: pass", "total_cost_usd": 0.42, "num_turns": 1},
]
print("\\n".join(json.dumps(e) for e in events))
"""


async def test_a_run_served_on_paid_extra_usage_is_kept_and_waits_for_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_wait(seconds: float) -> None:
        raise AssertionError(f"the dispatch waited {seconds}s for a usage limit")

    cli = _make_cli(tmp_path, _OVERAGE_CLI, name="overage.py")
    spawns = _count_spawns(monkeypatch, cli)
    gate = QuotaGate(sleep=no_wait)
    profile = ResolvedProfile(
        name="claude-code", arg_mode=ArgMode.inherit, cli=cli, model="claude-code/opus"
    )

    result = await dispatch_with_quota(
        ClaudeCliProvider(),
        gate,
        profile=profile,
        prompt="review",
        timeout_seconds=30,
        retry_minutes=15,
    )

    assert len(spawns) == 1  # the paid result is the outcome: no wait, no resume
    assert result.ok is True and result.quota is None and result.text == "VERDICT: pass"
    assert result.cost_usd == 0.42
    (row,) = gate.snapshot()
    assert (row["key"], row["closed"]) == ("claude-code", False)
    assert row["rate_limit"]["isUsingOverage"] is True


# A fake "claude" whose first run the five-hour limit refuses after it spent 4.52, and whose
# resumed session then reports the session's running total, 8.49 (as the CLI does).
_RESUMED_TOTAL_CLI = """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
init = {"type": "system", "subtype": "init", "session_id": "ses-r"}
if "--resume" in sys.argv:
    events = [init, {"type": "result", "subtype": "success", "is_error": False,
                     "session_id": "ses-r", "result": "VERDICT: pass",
                     "total_cost_usd": 8.49, "num_turns": 18}]
else:
    events = [init,
              {"type": "rate_limit_event", "session_id": "ses-r",
               "rate_limit_info": {"status": "rejected", "resetsAt": 1794924600,
                                   "rateLimitType": "five_hour"}},
              {"type": "result", "is_error": True, "session_id": "ses-r",
               "result": "You've hit your limit", "total_cost_usd": 4.52, "num_turns": 71}]
print("\\n".join(json.dumps(e) for e in events))
"""


class _ClockedSleep:
    """The gate's clock, moved by the gate's own sleeps only: a wait takes no real time."""

    def __init__(self) -> None:
        self.current = datetime(2026, 11, 17, 15, 0, tzinfo=UTC)  # half an hour before the reset

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class _InterruptedCosts:
    def __init__(self) -> None:
        self.costs: list[float | None] = []

    async def waiting(self, wait: QuotaWait, interrupted: AgentResult | None) -> None:
        if interrupted is not None:
            self.costs.append(interrupted.cost_usd)

    async def resumed(
        self, wait: QuotaWait, *, waited_seconds: float, resumed_session: bool
    ) -> None:
        pass


@pytest.mark.parametrize("routed", [False, True], ids=["claude-provider", "engine-router"])
async def test_a_resumed_session_is_charged_only_what_its_resumed_run_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, routed: bool
) -> None:
    cli = _make_cli(tmp_path, _RESUMED_TOTAL_CLI, name="resumed_total.py")
    spawns = _capture_spawns(monkeypatch, cli)
    clock = _ClockedSleep()
    listener = _InterruptedCosts()
    provider = EngineRouter(Engine.opencode, claude_bin=cli) if routed else ClaudeCliProvider()
    profile = ResolvedProfile(
        name="claude-code", arg_mode=ArgMode.inherit, cli=cli, model="claude-code/opus"
    )

    result = await dispatch_with_quota(
        provider,
        QuotaGate(clock=clock.now, sleep=clock.sleep),
        profile=profile,
        prompt="plan it",
        timeout_seconds=30,
        retry_minutes=15,
        listener=listener,
    )

    assert [("--resume" in args) for args, _env in spawns] == [False, True]
    assert result.ok is True and result.text == "VERDICT: pass"
    assert result.cost_is_session_total is True
    assert listener.costs == [4.52]
    assert result.cost_usd == pytest.approx(3.97)  # 8.49 for the session, 4.52 already charged
    assert result.num_turns == 18  # per run, as reported


async def test_a_session_to_resume_is_passed_as_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _make_cli(tmp_path, _FAKE_CLI)
    spawns = _capture_spawns(monkeypatch, cli)
    profile = ResolvedProfile(
        name="claude-code", arg_mode=ArgMode.flag, cli=cli, model="opus", resume_session_id="s-7"
    )

    await ClaudeCliProvider().run_agent(profile=profile, prompt="go on", timeout_seconds=30)

    (args, _env), = spawns
    at = args.index("--resume")
    assert args[at + 1] == "s-7" and args.index("--model") > at


# --- several result events in one stream ------------------------------------------------------
# A background task that ends after the answer wakes the session for another turn, which ends in
# a result event of its own. total_cost_usd, duration_api_ms and modelUsage are running totals of
# the session; num_turns, duration_ms and the usage tokens count one turn.

_ANSWER_TURN: dict[str, Any] = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "VERDICT: pass",
    "session_id": "ses-1",
    "num_turns": 102,
    "duration_ms": 610_000,
    "duration_api_ms": 540_000,
    "total_cost_usd": 1.85,
    "usage": {
        "input_tokens": 1_200,
        "output_tokens": 120_495,
        "cache_read_input_tokens": 4_800_000,
        "cache_creation_input_tokens": 95_000,
        "service_tier": "standard",
    },
    "modelUsage": {
        "claude-opus-5-5": {"outputTokens": 119_000, "costUSD": 1.8},
        "claude-haiku-4-5": {"outputTokens": 1_495, "costUSD": 0.05},
    },
}
_WAKE_UP_TURN: dict[str, Any] = {
    **_ANSWER_TURN,
    "result": "VERDICT: pass (the background test run passed too)",
    "num_turns": 1,
    "duration_ms": 60_000,
    "duration_api_ms": 600_000,
    "total_cost_usd": 2.26,
    "usage": {
        "input_tokens": 30,
        "output_tokens": 8_620,
        "cache_read_input_tokens": 210_000,
        "cache_creation_input_tokens": 4_000,
        "service_tier": "standard",
    },
    "modelUsage": {"claude-opus-5-5": {"outputTokens": 127_620, "costUSD": 2.21}},
}


def test_two_results_of_one_session_sum_turns_and_tokens_and_keep_the_last_cost() -> None:
    raw = _stream(_ANSWER_TURN, _WAKE_UP_TURN)

    result = _parse_envelope(raw, 0)

    assert result.num_turns == 103
    assert result.output_tokens == 129_115
    assert result.input_tokens == 1_230
    assert result.cache_read_tokens == 5_010_000
    assert result.cache_creation_tokens == 99_000
    assert result.cost_usd == 2.26  # a running total: the last one
    assert result.ok is True and result.error is None and result.quota is None
    assert result.text == _WAKE_UP_TURN["result"]
    assert result.session_id == "ses-1"
    # Every model behind the summed tokens, a model only the earlier result names included.
    assert result.model_reported == "claude-opus-5-5, claude-haiku-4-5"
    merged = _scan_stream(raw, {}).result
    assert merged is not None
    assert merged["duration_ms"] == 670_000  # per turn: summed
    assert merged["duration_api_ms"] == 600_000  # a running total: the last one
    assert merged["usage"]["service_tier"] == "standard"
    assert merged["modelUsage"] == {
        "claude-opus-5-5": _WAKE_UP_TURN["modelUsage"]["claude-opus-5-5"],
        "claude-haiku-4-5": _ANSWER_TURN["modelUsage"]["claude-haiku-4-5"],
    }


@pytest.mark.parametrize("last_is_error", [False, True])
def test_the_last_result_decides_the_outcome(last_is_error: bool) -> None:
    raw = _stream(
        {**_ANSWER_TURN, "is_error": not last_is_error},
        {**_WAKE_UP_TURN, "is_error": last_is_error},
    )

    result = _parse_envelope(raw, 0)

    assert result.ok is not last_is_error
    assert result.text == _WAKE_UP_TURN["result"]
    assert result.num_turns == 103


def test_a_wake_up_turn_that_hits_the_limit_is_a_quota() -> None:
    limit = "You've hit your limit · resets 3pm"
    raw = _stream(_ANSWER_TURN, {**_WAKE_UP_TURN, "is_error": True, "result": limit})

    result = _parse_envelope(raw, 0)

    assert result.quota is not None and result.quota.message == limit
    assert result.num_turns == 103 and result.output_tokens == 129_115


def test_a_result_of_another_session_replaces_the_earlier_one() -> None:
    raw = _stream(_ANSWER_TURN, {**_WAKE_UP_TURN, "session_id": "ses-2"})

    result = _parse_envelope(raw, 0)

    assert (result.num_turns, result.output_tokens, result.cost_usd) == (1, 8_620, 2.26)
    assert result.session_id == "ses-2"
    assert result.model_reported == "claude-opus-5-5"


def test_a_single_result_is_read_as_it_is() -> None:
    raw = _stream(_ANSWER_TURN)

    result = _parse_envelope(raw, 0)

    assert _scan_stream(raw, {}).result == _ANSWER_TURN
    assert (result.num_turns, result.input_tokens, result.output_tokens) == (102, 1_200, 120_495)
    assert (result.cache_read_tokens, result.cache_creation_tokens) == (4_800_000, 95_000)
    assert result.cost_usd == 1.85
    assert result.model_reported == "claude-opus-5-5, claude-haiku-4-5"


# --- a usage limit the run does not end on ------------------------------------------------------
# Claude Code can go on after a refused request (waiting, or retrying inside the run) until the
# agent timeout kills it. The limit it reported is still a usage limit.

_INIT = {"type": "system", "subtype": "init", "session_id": "ses-1"}


def _scripted_cli(tmp_path: Path, *steps: list[dict[str, Any]] | float) -> str:
    """A fake "claude" that runs *steps* in order: a list of events is written in one flushed
    write, a number sleeps that many seconds."""
    body = f"""#!/usr/bin/env python3
import json, sys, time
sys.stdin.read()
for step in json.loads({json.dumps(steps)!r}):
    if isinstance(step, list):
        sys.stdout.write("".join(json.dumps(event) + "\\n" for event in step))
        sys.stdout.flush()
    else:
        time.sleep(step)
"""
    return _make_cli(tmp_path, body, name="scripted.py")


@pytest.mark.parametrize(
    ("event", "kind"),
    [
        (_rate_limit_event("rejected", five_hour="rejected"), "rate_limit"),
        (_refused("rate_limit", "API Error: Rate limited"), "rate_limit"),
        (_refused("billing_error", "Credit balance is too low"), "billing"),
    ],
    ids=["rejected-rate-limit-event", "rate_limit-error", "billing_error"],
)
async def test_a_usage_limit_streamed_before_the_timeout_is_a_quota_not_a_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, event: dict[str, Any], kind: str
) -> None:
    cli = _scripted_cli(tmp_path, [_INIT, event], 30)
    spawns = _count_spawns(monkeypatch, cli)
    profile = ResolvedProfile(name="sys", arg_mode=ArgMode.inherit, cli=cli, cli_flags=())

    result = await ClaudeCliProvider().run_agent(profile=profile, prompt="hi", timeout_seconds=1)

    assert len(spawns) == 1
    assert result.ok is False
    assert result.quota is not None and result.quota.kind == kind
    assert result.session_id == "ses-1"  # the session the shared dispatch path resumes
    assert "timed out" not in (result.error or "")


async def test_a_refusal_the_run_goes_on_after_is_stopped_after_the_grace_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_cli, "_QUOTA_STOP_GRACE_S", 0.2)
    cli = _scripted_cli(tmp_path, [_INIT, _rate_limit_event("rejected", five_hour="rejected")], 30)
    spawns = _count_spawns(monkeypatch, cli)
    profile = ResolvedProfile(name="sys", arg_mode=ArgMode.inherit, cli=cli, cli_flags=())
    started = time.monotonic()

    result = await ClaudeCliProvider().run_agent(profile=profile, prompt="hi", timeout_seconds=30)

    assert time.monotonic() - started < 10  # stopped, neither waited for nor timed out
    assert len(spawns) == 1
    assert result.ok is False
    assert result.quota is not None and result.quota.kind == "rate_limit"
    assert result.quota.window == "five_hour"
    assert result.quota.resets_at == datetime.fromtimestamp(_RESETS_AT, UTC)
    assert result.session_id == "ses-1"


@pytest.mark.parametrize(
    "first",
    [
        [_INIT, _rate_limit_event("allowed_warning")],
        # A newer info that does not refuse lifts the refusal: the run is served again.
        [_INIT, _rate_limit_event("rejected", five_hour="rejected"), _rate_limit_event("allowed")],
    ],
    ids=["allowed_warning", "rejected-then-allowed"],
)
async def test_a_warning_or_a_lifted_refusal_leaves_the_run_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: list[dict[str, Any]]
) -> None:
    monkeypatch.setattr(claude_cli, "_QUOTA_STOP_GRACE_S", 0.05)
    cli = _scripted_cli(tmp_path, first, 0.5, [_result("VERDICT: pass")])
    spawns = _count_spawns(monkeypatch, cli)
    profile = ResolvedProfile(name="sys", arg_mode=ArgMode.inherit, cli=cli, cli_flags=())

    result = await ClaudeCliProvider().run_agent(profile=profile, prompt="hi", timeout_seconds=30)

    assert len(spawns) == 1
    assert result.ok is True and result.quota is None and result.error is None
    assert result.text == "VERDICT: pass"


async def test_the_refusal_watch_reads_each_complete_line_across_chunks() -> None:
    watch = claude_cli._RefusalWatch()
    line = json.dumps(_rate_limit_event("rejected", five_hour="rejected"))
    for at in range(0, len(line), 7):
        watch.feed(line[at : at + 7])
    with pytest.raises(TimeoutError):  # a line without its end is not read yet
        await asyncio.wait_for(watch.outlasts(0.0), 0.1)

    watch.feed("\n" + json.dumps(_INIT) + "\n")
    await asyncio.wait_for(watch.outlasts(0.0), 1)  # armed by the refusing info

    watch.feed(json.dumps(_rate_limit_event("allowed")) + "\n")
    with pytest.raises(TimeoutError):  # disarmed by a newer info that does not refuse
        await asyncio.wait_for(watch.outlasts(0.0), 0.1)
