"""Unit tests for Engine Profile env/arg composition (the DeepSeek recipe, etc.)."""

from __future__ import annotations

import pytest

from conclave.config import ArgMode
from conclave.providers import ResolvedProfile, build_invocation

_BASE_ARGS = ["--print", "--output-format", "stream-json", "--verbose"]
_BASE = ["--dangerously-skip-permissions", *_BASE_ARGS]  # with the default cli_flags
_ISOLATION = [
    "--strict-mcp-config",
    "--setting-sources",
    "project,local",
    "--disable-slash-commands",
    "--exclude-dynamic-system-prompt-sections",
]
_MEMORY_VAR = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"

# The next three tests pin isolated=False: they are the invocation as it was before worker
# isolation, which a non-isolated profile must still produce byte for byte.


def test_inherit_passes_only_base_args() -> None:
    inv = build_invocation(
        ResolvedProfile(name="system-default", arg_mode=ArgMode.inherit, isolated=False)
    )
    assert inv.args == [
        "--dangerously-skip-permissions", "--print", "--output-format", "stream-json", "--verbose"
    ]
    assert inv.env == {}


def test_flag_mode_appends_model_and_effort() -> None:
    inv = build_invocation(
        ResolvedProfile(
            name="claude",
            arg_mode=ArgMode.flag,
            model="claude-opus-4-8",
            effort="max",
            isolated=False,
        )
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
            isolated=False,
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


# --- worker isolation ----------------------------------------------------------------


def test_profiles_are_isolated_by_default() -> None:
    profile = ResolvedProfile(name="x", arg_mode=ArgMode.inherit)
    assert profile.isolated is True
    inv = build_invocation(profile)
    assert inv.args == [*_BASE, *_ISOLATION]
    assert inv.env == {_MEMORY_VAR: "1"}


def test_isolated_flag_mode_orders_cli_flags_base_isolation_then_model_and_effort() -> None:
    inv = build_invocation(
        ResolvedProfile(
            name="claude-code",
            arg_mode=ArgMode.flag,
            model="claude-opus-4-8",
            effort="max",
            cli_flags=("--dangerously-skip-permissions", "--custom"),
        )
    )
    assert inv.args == [
        "--dangerously-skip-permissions",
        "--custom",
        *_BASE_ARGS,
        *_ISOLATION,
        "--model",
        "claude-opus-4-8",
        "--effort",
        "max",
    ]
    assert inv.env == {_MEMORY_VAR: "1"}


def test_isolated_env_mode_adds_isolation_to_the_deepseek_recipe() -> None:
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
    assert inv.args == [*_BASE, *_ISOLATION]  # model/effort go via env, not flags
    assert inv.env == {
        "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "sk-secret",
        "ANTHROPIC_MODEL": "deepseek-v4-pro",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-pro",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
        "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-v4-flash",
        "CLAUDE_CODE_EFFORT_LEVEL": "max",
        _MEMORY_VAR: "1",
    }


@pytest.mark.parametrize("mode", list(ArgMode))
def test_isolation_only_adds_its_flags_and_env(mode: ArgMode) -> None:
    def build(isolated: bool) -> tuple[list[str], dict[str, str]]:
        inv = build_invocation(
            ResolvedProfile(
                name="p",
                arg_mode=mode,
                base_url="https://example.invalid",
                model="m",
                effort="high",
                isolated=isolated,
            )
        )
        return inv.args, inv.env

    on_args, on_env = build(True)
    off_args, off_env = build(False)
    # Not isolated: none of the isolation flags or the variable, and never an MCP config.
    assert not set(_ISOLATION) & set(off_args)
    assert _MEMORY_VAR not in off_env
    assert "--mcp-config" not in on_args + off_args
    # Isolated: the same invocation plus the flags right after the base args, plus the variable.
    assert on_args == [*off_args[: len(_BASE)], *_ISOLATION, *off_args[len(_BASE) :]]
    assert on_env == {**off_env, _MEMORY_VAR: "1"}


def test_extra_env_overrides_the_isolation_env() -> None:
    inv = build_invocation(
        ResolvedProfile(name="x", arg_mode=ArgMode.inherit, extra_env={_MEMORY_VAR: "0"})
    )
    assert inv.env == {_MEMORY_VAR: "0"}
    assert inv.args == [*_BASE, *_ISOLATION]  # the flags stay


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
