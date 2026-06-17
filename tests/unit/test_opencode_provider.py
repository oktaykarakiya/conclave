"""Unit tests for the opencode provider's NDJSON event parsing.

Fixtures mirror real ``opencode run --format json`` output: one JSON event per line,
``text`` events carry ``part.text``, and ``step_finish`` events carry ``part.cost`` +
``part.tokens`` (with a nested ``cache`` read/write).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conclave.config import ArgMode
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
    assert r.cache_read_tokens == 0
    assert r.cache_creation_tokens == 0
    assert r.num_turns == 1
    assert r.exit_code == 0


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
