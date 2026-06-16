"""Unit tests for Engine Profile env/arg composition (the DeepSeek recipe, etc.)."""

from __future__ import annotations

from conclave.config import ArgMode
from conclave.providers import ResolvedProfile, build_invocation


def test_inherit_passes_only_base_args() -> None:
    inv = build_invocation(ResolvedProfile(name="system-default", arg_mode=ArgMode.inherit))
    assert inv.args == ["--dangerously-skip-permissions", "--print", "--output-format", "json"]
    assert inv.env == {}


def test_flag_mode_appends_model_and_effort() -> None:
    inv = build_invocation(
        ResolvedProfile(name="claude", arg_mode=ArgMode.flag, model="claude-opus-4-8", effort="max")
    )
    assert inv.args[-4:] == ["--model", "claude-opus-4-8", "--effort", "max"]
    assert inv.env == {}


def test_env_mode_matches_deepseek_recipe() -> None:
    inv = build_invocation(
        ResolvedProfile(
            name="deepseek",
            arg_mode=ArgMode.env,
            base_url="https://api.deepseek.com/anthropic",
            auth_token="sk-secret",
            model="deepseek-v4-pro",
            subagent_model="deepseek-v4-flash",
            effort="max",
        )
    )
    assert inv.env == {
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "sk-secret",
        "ANTHROPIC_MODEL": "deepseek-v4-pro",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-pro",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
        "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-v4-flash",
        "CLAUDE_CODE_EFFORT_LEVEL": "max",
    }
    # In env mode, model/effort are routed via env, NOT CLI flags.
    assert "--model" not in inv.args
    assert "--effort" not in inv.args


def test_env_mode_haiku_defaults_to_model_without_subagent() -> None:
    inv = build_invocation(ResolvedProfile(name="ds", arg_mode=ArgMode.env, model="m"))
    assert inv.env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "m"
    assert "CLAUDE_CODE_SUBAGENT_MODEL" not in inv.env


def test_extra_env_overrides_last() -> None:
    inv = build_invocation(
        ResolvedProfile(
            name="x",
            arg_mode=ArgMode.env,
            model="m",
            extra_env={"ANTHROPIC_MODEL": "override", "FOO": "bar"},
        )
    )
    assert inv.env["ANTHROPIC_MODEL"] == "override"
    assert inv.env["FOO"] == "bar"
