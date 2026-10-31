"""Engine routing (opencode vs Claude Code) and the Claude CLI's stream-json parsing."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from conclave.config import ArgMode
from conclave.providers import (
    AgentResult,
    ClaudeCliProvider,
    Engine,
    EngineRouter,
    OnChunk,
    OpenCodeCliProvider,
    ResolvedProfile,
    parse_engine,
    resolve_engine,
)
from conclave.providers.claude_cli import _parse_envelope


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, Engine.opencode),
        ("", Engine.opencode),
        ("opencode", Engine.opencode),
        (" Claude ", Engine.claude),
        ("claude-code", Engine.claude),
    ],
)
def test_parse_engine(value: str | None, expected: Engine) -> None:
    assert parse_engine(value) is expected


def test_parse_engine_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unknown engine"):
        parse_engine("gpt")


@pytest.mark.parametrize(
    ("model", "default", "expected"),
    [
        # The prefix always selects Claude Code, whatever the default engine.
        ("claude-code/opus", Engine.opencode, (Engine.claude, "opus")),
        ("claude-code/claude-sonnet-4-5", Engine.opencode, (Engine.claude, "claude-sonnet-4-5")),
        ("claude-code/default", Engine.opencode, (Engine.claude, None)),
        ("claude-code", Engine.opencode, (Engine.claude, None)),
        # opencode default: the model is passed through untouched.
        (
            "deepseek/deepseek-v4-pro",
            Engine.opencode,
            (Engine.opencode, "deepseek/deepseek-v4-pro"),
        ),
        (None, Engine.opencode, (Engine.opencode, None)),
        # claude default: keep what the claude CLI understands, drop opencode-only models.
        (None, Engine.claude, (Engine.claude, None)),
        ("sonnet", Engine.claude, (Engine.claude, "sonnet")),
        ("anthropic/claude-sonnet-4-5", Engine.claude, (Engine.claude, "claude-sonnet-4-5")),
        ("google/gemini-2.5-flash-lite", Engine.claude, (Engine.claude, None)),
    ],
)
def test_resolve_engine(
    model: str | None, default: Engine, expected: tuple[Engine, str | None]
) -> None:
    assert resolve_engine(model, default) == expected


class _Recorder:
    def __init__(self, label: str) -> None:
        self.label = label
        self.profiles: list[ResolvedProfile] = []

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
        self.profiles.append(profile)
        return AgentResult(ok=True, text=self.label)


def _router(default: Engine) -> tuple[EngineRouter, _Recorder, _Recorder]:
    oc, cc = _Recorder("opencode"), _Recorder("claude")
    router = EngineRouter(
        default,
        opencode=oc,  # type: ignore[arg-type]
        claude=cc,  # type: ignore[arg-type]
        claude_bin="/opt/claude",
    )
    return router, oc, cc


def _profile(model: str | None, effort: str | None = None) -> ResolvedProfile:
    return ResolvedProfile(name="opencode", arg_mode=ArgMode.inherit, model=model, effort=effort)


async def test_router_sends_prefixed_model_to_claude_with_flags() -> None:
    router, oc, cc = _router(Engine.opencode)
    result = await router.run_agent(
        profile=_profile("claude-code/opus", "high"), prompt="p", timeout_seconds=5
    )
    assert result.text == "claude"
    assert not oc.profiles
    (sent,) = cc.profiles
    assert (sent.arg_mode, sent.model, sent.effort, sent.cli) == (
        ArgMode.flag,
        "opus",
        "high",
        "/opt/claude",
    )
    # The recorder reported no model, so the router labels the dispatch itself.
    assert result.model_reported == "claude-code/opus"


async def test_router_keeps_opencode_profile_untouched() -> None:
    router, oc, cc = _router(Engine.opencode)
    profile = _profile("deepseek/deepseek-v4-pro", "max")
    result = await router.run_agent(profile=profile, prompt="p", timeout_seconds=5)
    assert result.text == "opencode"
    assert oc.profiles == [profile]
    assert not cc.profiles


async def test_router_claude_default_drops_opencode_model() -> None:
    router, oc, cc = _router(Engine.claude)
    await router.run_agent(
        profile=_profile("google/gemini-2.5-flash-lite"), prompt="p", timeout_seconds=5
    )
    assert not oc.profiles
    assert cc.profiles[0].model is None


def test_router_default_instances() -> None:
    router = EngineRouter()
    assert router.default is Engine.opencode
    assert isinstance(router._opencode, OpenCodeCliProvider)
    assert isinstance(router._claude, ClaudeCliProvider)


# --- stream-json parsing -------------------------------------------------------------

_STREAM = "\n".join(
    json.dumps(e)
    for e in [
        {"type": "system", "subtype": "init", "session_id": "s-1", "model": "claude-opus-5-5"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "looking"},
                    {"type": "tool_use", "name": "Read", "input": {}},
                    {"type": "tool_use", "name": "Bash", "input": {}},
                ]
            },
        },
        {"type": "user", "message": {"content": [{"type": "tool_result"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}},
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "VERDICT: pass",
            "session_id": "s-1",
            "num_turns": 4,
            "total_cost_usd": 0.12,
            "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 5},
            "modelUsage": {"claude-opus-5-5": {}},
        },
    ]
)


def test_parse_stream_json() -> None:
    r = _parse_envelope(_STREAM + "\n", 0)
    assert r.ok and r.error is None
    assert r.text == "VERDICT: pass"
    assert r.session_id == "s-1"
    assert (r.num_turns, r.cost_usd, r.input_tokens, r.output_tokens) == (4, 0.12, 10, 20)
    assert r.cache_read_tokens == 5
    assert r.model_reported == "claude-opus-5-5"
    assert r.tool_breakdown == {"Read": 2, "Bash": 1}
    assert r.tool_calls_count == 3


def test_parse_stream_without_result_keeps_raw() -> None:
    raw = '{"type": "system", "subtype": "init"}\nKilled\n'
    r = _parse_envelope(raw, -9)
    assert not r.ok
    assert r.text == raw
    assert r.error == "{\"type\": \"system\", \"subtype\": \"init\"}\nKilled"


def test_parse_is_error_fails_even_with_success_hint() -> None:
    raw = json.dumps(
        {"type": "result", "is_error": True, "result": "I have no credits: please /login"}
    )
    r = _parse_envelope(raw, 1)
    assert not r.ok
    assert r.error is not None and "/login" in r.error


# --- subprocess env -----------------------------------------------------------------

_ENV_CLI = """#!/usr/bin/env python3
import json, os, sys
sys.stdin.read()
print(json.dumps({"type": "result", "is_error": False,
                  "result": ",".join(sorted(k for k in os.environ
                                            if k in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID",
                                                     "CLAUDE_CODE_USE_VERTEX")))}))
"""


async def test_parent_claude_session_env_is_not_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "fake_claude.py"
    script.write_text(_ENV_CLI, encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "parent")
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")  # user config: must survive
    profile = ResolvedProfile(name="c", arg_mode=ArgMode.flag, cli=str(script), cli_flags=())
    result = await ClaudeCliProvider().run_agent(profile=profile, prompt="x", timeout_seconds=30)
    assert result.ok
    assert result.text == "CLAUDE_CODE_USE_VERTEX"


_FAILING_STREAM_CLI = """#!/usr/bin/env python3
import json, sys
sys.stdin.read()
count = __import__("pathlib").Path(sys.argv[0] + ".runs")
count.write_text(str(int(count.read_text()) + 1 if count.exists() else 1))
print(json.dumps({"type": "user", "message": {"content": "pytest: ECONNREFUSED 127.0.0.1"}}))
print(json.dumps({"type": "result", "is_error": True, "result": "tests failed"}))
sys.exit(1)
"""


async def test_transient_pattern_in_tool_output_does_not_retry(tmp_path: Path) -> None:
    script = tmp_path / "fake_claude.py"
    script.write_text(_FAILING_STREAM_CLI, encoding="utf-8")
    script.chmod(0o755)
    profile = ResolvedProfile(name="c", arg_mode=ArgMode.flag, cli=str(script), cli_flags=())
    result = await ClaudeCliProvider().run_agent(profile=profile, prompt="x", timeout_seconds=30)
    assert not result.ok
    assert result.error == "tests failed"
    assert (tmp_path / "fake_claude.py.runs").read_text() == "1"
