"""Subprocess-level tests for the Claude CLI provider, using a fake CLI script."""

from __future__ import annotations

from pathlib import Path

from conclave.config import ArgMode
from conclave.providers import ClaudeCliProvider, ResolvedProfile, probe_profile

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


def _make_cli(tmp_path: Path, body: str, name: str = "fake_claude.py") -> str:
    script = tmp_path / name
    script.write_text(body)
    script.chmod(0o755)
    return str(script)


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


async def test_cli_not_found() -> None:
    profile = ResolvedProfile(
        name="sys", arg_mode=ArgMode.inherit, cli="/nonexistent/conclave-xyz", cli_flags=()
    )
    result = await ClaudeCliProvider().run_agent(profile=profile, prompt="hi", timeout_seconds=5)
    assert not result.ok
    assert "not found" in (result.error or "")


async def test_probe_profile_smoke(tmp_path: Path) -> None:
    cli = _make_cli(tmp_path, _FAKE_CLI)
    profile = ResolvedProfile(name="ds", arg_mode=ArgMode.env, model="m", cli=cli, cli_flags=())
    report = await probe_profile(ClaudeCliProvider(), profile, timeout_seconds=30)
    assert report.ok
    assert report.model_reported == "m"
    assert report.latency_ms is not None
