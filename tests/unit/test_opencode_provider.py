"""Unit tests for the opencode provider's NDJSON event parsing.

Fixtures mirror real ``opencode run --format json`` output: one JSON event per line,
``text`` events carry ``part.text``, and ``step_finish`` events carry ``part.cost`` +
``part.tokens`` (with a nested ``cache`` read/write).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from conclave.config import ArgMode
from conclave.providers import AgentResult, opencode_cli
from conclave.providers.opencode_cli import OpenCodeCliProvider, _parse_events
from conclave.providers.profiles import ResolvedProfile

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
