"""Unit tests for the opencode provider's NDJSON event parsing.

Fixtures mirror real ``opencode run --format json`` output: one JSON event per line,
``text`` events carry ``part.text``, and ``step_finish`` events carry ``part.cost`` +
``part.tokens`` (with a nested ``cache`` read/write).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from conclave.config import ArgMode
from conclave.providers import AgentResult, opencode_cli
from conclave.providers.opencode_cli import OpenCodeCliProvider, _parse_events
from conclave.providers.profiles import ResolvedProfile


@pytest.fixture(autouse=True)
def _no_mcp_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake CLIs below answer any argv as a run: keep an isolated dispatch's MCP discovery
    (``opencode debug config``, covered in test_opencode_isolation.py) from starting them."""

    async def no_servers(*args: Any, **kwargs: Any) -> tuple[str, ...]:
        return ()

    monkeypatch.setattr(opencode_cli, "_discover_mcp_servers", no_servers)


# A representative single-step stream (captured from a real deepseek run).
_STREAM = "\n".join(
    [
        '{"type":"step_start","part":{"type":"step-start"}}',
        '{"type":"text","part":{"type":"text","text":"Hello "}}',
        '{"type":"text","part":{"type":"text","text":"world"}}',
        '{"type":"step_finish","part":{"type":"step-finish","reason":"stop",'
        '"tokens":{"total":7387,"input":7372,"output":3,"reasoning":12,'
        '"cache":{"write":0,"read":0}},"cost":0.00103628}}',
    ]
)


def test_parse_events_extracts_text_cost_tokens() -> None:
    r = _parse_events(_STREAM, exit_code=0)
    assert r.ok is True
    assert r.text == "Hello world"
    assert r.cost_usd == 0.00103628
    assert r.input_tokens == 7372
    assert r.output_tokens == 3
    assert r.reasoning_tokens == 12
    assert r.cache_read_tokens == 0
    assert r.cache_creation_tokens == 0
    assert r.num_turns == 1
    assert r.exit_code == 0


def test_parse_events_extracts_session_id_and_reasoning() -> None:
    stream = "\n".join(
        [
            '{"type":"step_start","sessionID":"ses_abc123","part":{"type":"step-start"}}',
            '{"type":"text","sessionID":"ses_abc123","part":{"type":"text","text":"done"}}',
            '{"type":"step_finish","sessionID":"ses_abc123","part":{"tokens":'
            '{"input":10,"output":5,"reasoning":42},"cost":0.001}}',
        ]
    )
    r = _parse_events(stream, exit_code=0, model_hint="deepseek/deepseek-reasoner")
    assert r.session_id == "ses_abc123"
    assert r.reasoning_tokens == 42
    assert r.model_reported == "deepseek/deepseek-reasoner"


def test_parse_events_extracts_tool_calls_and_breakdown() -> None:
    stream = "\n".join(
        [
            '{"type":"tool_use","sessionID":"ses_t1","part":{"type":"tool","tool":"bash"}}',
            '{"type":"tool","part":{"type":"tool-invocation","name":"read"}}',
            '{"type":"tool_use","part":{"type":"tool","tool":"bash"}}',
            '{"type":"text","part":{"type":"text","text":"Checked files"}}',
        ]
    )
    r = _parse_events(stream, exit_code=0)
    assert r.tool_calls_count == 3
    assert r.tool_breakdown == {"bash": 2, "read": 1}
    assert len(r.messages) == 1
    assert r.messages[0]["role"] == "assistant"
    assert r.messages[0]["text"] == "Checked files"


def test_parse_events_sums_multiple_steps() -> None:
    stream = "\n".join(
        [
            '{"type":"text","part":{"type":"text","text":"a"}}',
            '{"type":"step_finish","part":{"tokens":{"input":10,"output":2,'
            '"cache":{"read":1,"write":3}},"cost":0.001}}',
            '{"type":"text","part":{"type":"text","text":"b"}}',
            '{"type":"step_finish","part":{"tokens":{"input":20,"output":4,'
            '"cache":{"read":2,"write":5}},"cost":0.002}}',
        ]
    )
    r = _parse_events(stream, exit_code=0)
    assert r.text == "ab"
    assert r.num_turns == 2
    assert r.input_tokens == 30
    assert r.output_tokens == 6
    assert r.cache_read_tokens == 3
    assert r.cache_creation_tokens == 8
    assert abs((r.cost_usd or 0.0) - 0.003) < 1e-9


def test_parse_events_skips_malformed_lines() -> None:
    stream = "\n".join(
        [
            "not json at all",
            '{"type":"text","part":{"type":"text","text":"ok"}}',
            "{ broken json",
            "[1,2,3]",  # valid JSON but not a dict
            "",
        ]
    )
    r = _parse_events(stream, exit_code=0)
    assert r.text == "ok"  # malformed lines ignored, never crash


def test_parse_events_ok_from_exit_code_and_hint() -> None:
    # Non-zero exit but the text carries a verdict hint -> still ok (mirrors claude).
    hinted = '{"type":"text","part":{"type":"text","text":"VERDICT: pass"}}'
    assert _parse_events(hinted, exit_code=1).ok is True
    # Non-zero exit, no hint -> not ok.
    plain = '{"type":"text","part":{"type":"text","text":"some plain output"}}'
    assert _parse_events(plain, exit_code=1).ok is False


def test_parse_events_no_step_finish_leaves_usage_none() -> None:
    r = _parse_events('{"type":"text","part":{"type":"text","text":"hi"}}', exit_code=0)
    assert r.text == "hi"
    assert r.cost_usd is None
    assert r.input_tokens is None
    assert r.num_turns is None


async def test_run_agent_reads_ndjson_line_over_64kib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single NDJSON line larger than asyncio's 64 KiB readline limit must not abort the
    dispatch (regression: readline() raised LimitOverrunError; we now read raw chunks)."""
    fake = tmp_path / "fake_opencode.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        "big = 'x' * 100000\n"  # > 64 KiB in one line
        "sys.stdout.write('{\"type\":\"text\",\"part\":"
        "{\"type\":\"text\",\"text\":\"' + big + '\"}}\\n')\n"
    )
    fake.chmod(0o755)
    monkeypatch.setattr(
        "conclave.providers.opencode_cli._OPENCODE_BIN", str(fake)
    )
    profile = ResolvedProfile(name="opencode", arg_mode=ArgMode.inherit)
    result = await OpenCodeCliProvider().run_agent(
        profile=profile, prompt="hello", timeout_seconds=30
    )
    assert result.ok
    assert result.text == "x" * 100000  # full line read + parsed, no LimitOverrunError


async def test_run_agent_passes_profile_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "fake_opencode_flags.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "sys.stdin.buffer.read()\n"
        "joined = ' '.join(sys.argv[1:])\n"
        "ev = {'type': 'text', 'part': {'type': 'text', 'text': joined}}\n"
        "sys.stdout.write(json.dumps(ev) + '\\n')\n"
    )
    fake.chmod(0o755)
    monkeypatch.setattr(
        "conclave.providers.opencode_cli._OPENCODE_BIN", str(fake)
    )
    profile = ResolvedProfile(
        name="opencode",
        arg_mode=ArgMode.inherit,
        model="deepseek/deepseek-reasoner",
        effort="high",
        cli_agent="build",
        thinking=True,
        pure=True,
    )
    result = await OpenCodeCliProvider().run_agent(
        profile=profile, prompt="hello", timeout_seconds=30
    )
    assert result.ok
    args = result.text.split()
    assert "--auto" in args
    assert "--dangerously-skip-permissions" not in args
    assert "--model" in args
    assert "deepseek/deepseek-reasoner" in args
    assert "--variant" in args
    assert "high" in args
    assert "--agent" in args
    assert "build" in args
    assert "--thinking" in args
    assert "--pure" in args
    assert result.model_reported == "deepseek/deepseek-reasoner"


async def test_run_agent_retries_transient_connection_error_and_resumes_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_file = tmp_path / "attempt_count.txt"
    fake = tmp_path / "fake_opencode_transient.py"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, pathlib, sys\n"
        "sys.stdin.buffer.read()\n"
        f"p = pathlib.Path({str(state_file)!r})\n"
        "n = int(p.read_text()) if p.exists() else 0\n"
        "p.write_text(str(n + 1))\n"
        "if n == 0:\n"
        "    err = {\n"
        "        'type': 'error',\n"
        "        'sessionID': 'ses_resume_99',\n"
        "        'error': {'data': {'message': 'Cannot connect to API: Unable to connect'}},\n"
        "    }\n"
        "    sys.stdout.write(json.dumps(err) + '\\n')\n"
        "    sys.exit(1)\n"
        "joined = ' '.join(sys.argv[1:])\n"
        "ev = {\n"
        "    'type': 'text',\n"
        "    'sessionID': 'ses_resume_99',\n"
        "    'part': {'type': 'text', 'text': 'VERDICT: pass | args=' + joined},\n"
        "}\n"
        "sys.stdout.write(json.dumps(ev) + '\\n')\n"
    )
    fake.chmod(0o755)
    monkeypatch.setattr("conclave.providers.opencode_cli._OPENCODE_BIN", str(fake))
    monkeypatch.setattr("conclave.providers.opencode_cli._TRANSIENT_RETRY_DELAY_S", 0.05)

    profile = ResolvedProfile(
        name="opencode",
        arg_mode=ArgMode.inherit,
        model="google/gemini-2.5-flash-lite",
    )
    result = await OpenCodeCliProvider().run_agent(
        profile=profile, prompt="review gate", timeout_seconds=30
    )
    assert result.ok is True
    assert "VERDICT: pass" in result.text
    assert "--session ses_resume_99" in result.text
    assert state_file.read_text() == "2"


def _fake_opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, body: str) -> str:
    """Install a fake ``opencode`` running *body* after it reads the prompt; return its path."""
    fake = tmp_path / name
    fake.write_text(
        "#!/usr/bin/env python3\nimport json, sys, time\nsys.stdin.buffer.read()\n" + body
    )
    fake.chmod(0o755)
    monkeypatch.setattr("conclave.providers.opencode_cli._OPENCODE_BIN", str(fake))
    return str(fake)


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


async def test_cancel_kills_the_run_and_flags_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_opencode(tmp_path, monkeypatch, "fake_opencode_slow.py", "time.sleep(5)\n")
    cancel_event = asyncio.Event()
    asyncio.get_running_loop().call_later(0.3, cancel_event.set)
    started = time.monotonic()

    result = await OpenCodeCliProvider().run_agent(
        profile=ResolvedProfile(name="opencode", arg_mode=ArgMode.inherit),
        prompt="hello",
        timeout_seconds=30,
        cancel_event=cancel_event,
    )

    assert result.cancelled is True
    assert result.ok is False
    assert time.monotonic() - started < 4  # killed, not waited for (the CLI sleeps 5 s)


async def test_cancelled_run_is_not_retried_even_if_output_looks_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake_opencode(
        tmp_path,
        monkeypatch,
        "fake_opencode_dropped.py",
        "err = {'type': 'error', 'error': {'data': {'message': "
        "'Cannot connect to API: Unable to connect'}}}\n"
        "sys.stdout.write(json.dumps(err) + '\\n')\n"
        "sys.exit(1)\n",
    )
    cancel_event = asyncio.Event()
    parse = opencode_cli._parse_events

    def parse_as_the_stop_lands(
        raw: str, exit_code: int | None, model_hint: str | None = None
    ) -> AgentResult:
        cancel_event.set()  # the operator stops the task just as this run ends
        return parse(raw, exit_code, model_hint=model_hint)

    monkeypatch.setattr(opencode_cli, "_parse_events", parse_as_the_stop_lands)
    monkeypatch.setattr(opencode_cli, "_TRANSIENT_RETRY_DELAY_S", 0.0)
    spawns = _count_spawns(monkeypatch, fake)

    result = await OpenCodeCliProvider().run_agent(
        profile=ResolvedProfile(name="opencode", arg_mode=ArgMode.inherit),
        prompt="hello",
        timeout_seconds=30,
        cancel_event=cancel_event,
    )

    assert len(spawns) == 1
    assert result.cancelled is True
    assert result.ok is False


# --- usage limits ----------------------------------------------------------------------------
# The error event opencode 1.19.5 emits (and exits 1) when DeepSeek answers HTTP 402.
_BALANCE_EVENT = {
    "type": "error",
    "sessionID": "ses_402",
    "error": {
        "name": "APIError",
        "data": {
            "message": "Insufficient Balance (request_id: 7f3a)",
            "statusCode": 402,
            "isRetryable": False,
            "responseHeaders": {"content-type": "application/json"},
        },
    },
}
_NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def _rate_limited(headers: dict[str, str] | None = None) -> str:
    data: dict[str, Any] = {"message": "Too Many Requests", "statusCode": 429, "isRetryable": True}
    if headers is not None:
        data["responseHeaders"] = headers
    event = {"type": "error", "sessionID": "ses_429", "error": {"name": "APIError", "data": data}}
    return json.dumps(event) + "\n"


def test_parse_events_reports_402_insufficient_balance_as_billing() -> None:
    result = _parse_events(json.dumps(_BALANCE_EVENT) + "\n", exit_code=1, now=_NOW)

    assert result.ok is False
    assert result.quota is not None
    assert result.quota.kind == "billing"
    assert result.quota.resets_at is None
    assert result.quota.message == "Insufficient Balance (request_id: 7f3a)"
    assert result.session_id == "ses_402"
    assert result.error == "Insufficient Balance (request_id: 7f3a)"


@pytest.mark.parametrize(
    ("headers", "after"),
    [
        ({"retry-after": "30"}, timedelta(seconds=30)),
        ({"Retry-After-Ms": "1500"}, timedelta(milliseconds=1500)),
        ({"retry-after": "Tue, 17 Nov 2026 12:05:00 GMT"}, timedelta(minutes=5)),
        (None, None),
        ({"content-type": "application/json"}, None),
    ],
)
def test_parse_events_reports_429_with_the_retry_after_reset(
    headers: dict[str, str] | None, after: timedelta | None
) -> None:
    result = _parse_events(_rate_limited(headers), exit_code=1, now=_NOW)

    assert result.ok is False
    assert result.quota is not None and result.quota.kind == "rate_limit"
    assert result.quota.resets_at == (_NOW + after if after is not None else None)
    assert result.session_id == "ses_429"


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        ("You exceeded your current quota, please check your plan", "billing"),
        ("Rate limit reached for requests", "rate_limit"),
    ],
)
def test_parse_events_reads_the_quota_from_the_message_without_a_status(
    message: str, kind: str
) -> None:
    event = {"type": "error", "error": {"name": "APIError", "data": {"message": message}}}

    quota = _parse_events(json.dumps(event), exit_code=1, now=_NOW).quota

    assert quota is not None and quota.kind == kind


def test_parse_events_other_errors_are_not_a_quota() -> None:
    event = {"type": "error", "error": {"data": {"message": "Bad Request", "statusCode": 400}}}

    result = _parse_events(json.dumps(event), exit_code=1)

    assert result.ok is False and result.quota is None


async def test_a_quota_error_is_spawned_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake_opencode(
        tmp_path,
        monkeypatch,
        "fake_opencode_402.py",
        f"sys.stdout.write(json.dumps({_BALANCE_EVENT!r}) + '\\n')\nsys.exit(1)\n",
    )
    monkeypatch.setattr(opencode_cli, "_TRANSIENT_RETRY_DELAY_S", 0.0)
    spawns = _count_spawns(monkeypatch, fake)

    result = await OpenCodeCliProvider().run_agent(
        profile=ResolvedProfile(name="opencode", arg_mode=ArgMode.inherit),
        prompt="hello",
        timeout_seconds=30,
    )

    assert len(spawns) == 1  # neither the transient nor the empty-output retry re-runs it
    assert result.quota is not None and result.quota.kind == "billing"
    assert result.session_id == "ses_402"


async def test_a_session_to_resume_is_passed_as_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_opencode(
        tmp_path,
        monkeypatch,
        "fake_opencode_args.py",
        "ev = {'type': 'text', 'part': {'type': 'text', 'text': ' '.join(sys.argv[1:])}}\n"
        "sys.stdout.write(json.dumps(ev) + '\\n')\n",
    )

    result = await OpenCodeCliProvider().run_agent(
        profile=ResolvedProfile(
            name="opencode", arg_mode=ArgMode.inherit, resume_session_id="ses_77"
        ),
        prompt="go on",
        timeout_seconds=30,
    )

    args = result.text.split()
    assert args[args.index("--session") + 1] == "ses_77"


async def test_resuming_an_unknown_session_is_not_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake_opencode(
        tmp_path,
        monkeypatch,
        "fake_opencode_unknown.py",
        "sys.stdout.write('Error: Session not found\\n')\nsys.exit(1)\n",
    )
    monkeypatch.setattr(opencode_cli, "_TRANSIENT_RETRY_DELAY_S", 0.0)
    spawns = _count_spawns(monkeypatch, fake)

    result = await OpenCodeCliProvider().run_agent(
        profile=ResolvedProfile(
            name="opencode", arg_mode=ArgMode.inherit, resume_session_id="ses_gone"
        ),
        prompt="go on",
        timeout_seconds=30,
    )

    assert len(spawns) == 1
    assert result.ok is False and "Session not found" in (result.error or "")


@pytest.mark.parametrize(
    "headers",
    [{"retry-after": "1e12"}, {"retry-after": "inf"}, {"retry-after-ms": "1e18"}],
    ids=["seconds-past-year-9999", "infinite", "milliseconds-past-timedelta"],
)
def test_an_out_of_range_retry_after_is_an_unknown_reset(headers: dict[str, str]) -> None:
    result = _parse_events(_rate_limited(headers), exit_code=1, now=_NOW)

    assert result.ok is False
    assert result.quota is not None and result.quota.kind == "rate_limit"
    assert result.quota.resets_at is None  # the gate waits execution.quota_retry_minutes


def test_an_out_of_range_retry_after_ms_falls_back_to_retry_after() -> None:
    headers = {"retry-after-ms": "1e18", "retry-after": "30"}

    quota = _parse_events(_rate_limited(headers), exit_code=1, now=_NOW).quota

    assert quota is not None and quota.resets_at == _NOW + timedelta(seconds=30)


def test_a_failed_run_without_an_error_event_reports_why() -> None:
    config_error = (
        "Error: Configuration is invalid at /home/u/.config/opencode/opencode.json\n"
        '  Unrecognized key: "mcpp"\n'
    )

    failed = _parse_events(config_error, exit_code=1)
    killed = _parse_events("", exit_code=-9)
    served = _parse_events('{"type":"text","part":{"type":"text","text":"done"}}', exit_code=0)

    assert failed.ok is False and "Configuration is invalid" in (failed.error or "")
    assert killed.ok is False and killed.error == "opencode exited with code -9"
    assert served.ok is True and served.error is None


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
def test_a_line_separator_inside_a_json_string_keeps_the_event(separator: str) -> None:
    """JSON.stringify leaves U+2028, U+2029 and U+0085 raw inside strings: they end no line."""
    text = f"line one{separator}line two"
    event = {"type": "text", "sessionID": "ses_s", "part": {"type": "text", "text": text}}

    result = _parse_events(json.dumps(event, ensure_ascii=False) + "\n", exit_code=0)

    assert result.text == text
    assert result.session_id == "ses_s"


# --- the CLI process: cancellation, a closed stdin, streamed text -------------------------------


def _running(pid: int) -> bool:
    """Whether *pid* is a live process; a zombie (killed, not reaped yet) is not."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except FileNotFoundError:  # gone since os.kill, unless there is no /proc to tell
        return not Path("/proc/self").is_dir()
    except OSError:
        return True
    return stat.rsplit(")", 1)[-1].split()[0] != "Z"


async def _gone_within(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while _running(pid):
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(0.05)
    return True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process liveness")
async def test_cancelling_the_dispatch_task_kills_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A planning session's cancel, a worker's stop and shutdown cancel the task awaiting the
    dispatch: the CLI must not run on without a timeout."""
    pid_file = tmp_path / "cli.pid"
    _fake_opencode(
        tmp_path,
        monkeypatch,
        "fake_opencode_sleepy.py",
        f"import os\nopen({str(pid_file)!r}, 'w').write(str(os.getpid()))\ntime.sleep(30)\n",
    )
    dispatch = asyncio.create_task(
        OpenCodeCliProvider().run_agent(
            profile=ResolvedProfile(name="opencode", arg_mode=ArgMode.inherit),
            prompt="hello",
            timeout_seconds=60,
        )
    )
    deadline = time.monotonic() + 10
    while not (pid_file.exists() and pid_file.read_text().strip()):
        assert time.monotonic() < deadline, "the fake opencode never started"
        await asyncio.sleep(0.05)
    pid = int(pid_file.read_text())
    try:
        dispatch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await dispatch
        assert await _gone_within(pid), f"the CLI ({pid}) still runs after the cancel"
    finally:
        if _running(pid):
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


async def test_a_cli_that_exits_before_reading_its_prompt_fails_with_its_own_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "old_opencode.py"
    fake.write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        "sys.stdout.write(\"error: unknown option '--auto'\\n\")\nsys.stdout.flush()\n"
        "sys.exit(1)\n"
    )
    fake.chmod(0o755)
    monkeypatch.setattr(opencode_cli, "_OPENCODE_BIN", str(fake))
    monkeypatch.setattr(opencode_cli, "_TRANSIENT_RETRY_DELAY_S", 0.0)

    result = await OpenCodeCliProvider().run_agent(
        profile=ResolvedProfile(name="opencode", arg_mode=ArgMode.inherit),
        prompt="x" * (1 << 20),  # more than a pipe holds
        timeout_seconds=30,
    )

    assert result.ok is False
    assert "unknown option" in (result.error or "")


async def test_a_character_split_across_reads_is_decoded_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = "before \u2014 after"
    line = json.dumps({"type": "text", "part": {"type": "text", "text": text}}, ensure_ascii=False)
    payload = (line + "\n").encode("utf-8")
    cut = payload.index("\u2014".encode()) + 1  # after the dash's first byte
    (tmp_path / "payload.bin").write_bytes(payload)
    _fake_opencode(
        tmp_path,
        monkeypatch,
        "fake_opencode_split.py",
        f"data = open({str(tmp_path / 'payload.bin')!r}, 'rb').read()\n"
        f"sys.stdout.buffer.write(data[:{cut}])\nsys.stdout.buffer.flush()\n"
        "time.sleep(0.3)\n"
        f"sys.stdout.buffer.write(data[{cut}:])\nsys.stdout.buffer.flush()\n",
    )
    streamed: list[str] = []

    async def collect(chunk: str) -> None:
        streamed.append(chunk)

    result = await OpenCodeCliProvider().run_agent(
        profile=ResolvedProfile(name="opencode", arg_mode=ArgMode.inherit),
        prompt="hello",
        timeout_seconds=30,
        on_chunk=collect,
    )

    assert result.ok is True
    assert result.text == text
    assert "\ufffd" not in "".join(streamed)
    assert all(streamed)  # no empty chunk is streamed


# --- a dispatch's usage across its attempts -----------------------------------------------------


def _step(cost: float, output: int, input_tokens: int = 10) -> dict[str, Any]:
    return {
        "type": "step_finish",
        "sessionID": "ses_f9",
        "part": {"cost": cost, "tokens": {"input": input_tokens, "output": output}},
    }


_BASH_CALL = {"type": "tool_use", "sessionID": "ses_f9", "part": {"type": "tool", "tool": "bash"}}
_ANSWER = {"type": "text", "sessionID": "ses_f9", "part": {"type": "text", "text": "VERDICT: pass"}}
_DROPPED = {
    "type": "error",
    "sessionID": "ses_f9",
    "error": {"data": {"message": "Cannot connect to API: Unable to connect"}},
}


@pytest.mark.parametrize(
    ("attempts", "runs", "cost", "output", "turns", "tools"),
    [
        # An empty reply (the tool call and its step were paid for), then the answer.
        ([([_BASH_CALL, _step(0.4, 50)], 0), ([_ANSWER, _step(0.1, 7)], 0)], 2, 0.5, 57, 2, 1),
        # A dropped connection after a paid step, then the answer.
        ([([_step(0.2, 5), _DROPPED], 1), ([_ANSWER, _step(0.1, 7)], 0)], 2, 0.3, 12, 2, 0),
        # Every attempt used, none with an answer: the last one is counted once.
        ([([_step(0.1, 1)], 0)] * 3, 3, 0.3, 3, 3, 0),
    ],
    ids=["empty-reply", "transient-error", "every-attempt-used"],
)
async def test_a_rerun_dispatch_counts_the_usage_of_every_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attempts: list[tuple[list[dict[str, Any]], int]],
    runs: int,
    cost: float,
    output: int,
    turns: int,
    tools: int,
) -> None:
    state = tmp_path / "runs"
    fake = _fake_opencode(
        tmp_path,
        monkeypatch,
        "fake_opencode_attempts.py",
        f"import pathlib\nattempts = json.loads({json.dumps(attempts)!r})\n"
        f"state = pathlib.Path({str(state)!r})\n"
        "n = int(state.read_text()) if state.exists() else 0\n"
        "state.write_text(str(n + 1))\n"
        "events, code = attempts[n]\n"
        "sys.stdout.write(''.join(json.dumps(e) + '\\n' for e in events))\n"
        "sys.exit(code)\n",
    )
    monkeypatch.setattr(opencode_cli, "_TRANSIENT_RETRY_DELAY_S", 0.0)
    spawns = _count_spawns(monkeypatch, fake)

    result = await OpenCodeCliProvider().run_agent(
        profile=ResolvedProfile(name="opencode", arg_mode=ArgMode.inherit),
        prompt="review",
        timeout_seconds=30,
    )

    assert len(spawns) == runs
    assert result.cost_usd == pytest.approx(cost)
    assert result.output_tokens == output
    assert result.input_tokens == 10 * runs
    assert result.num_turns == turns
    assert result.tool_calls_count == tools
    assert result.tool_breakdown == ({"bash": tools} if tools else {})
