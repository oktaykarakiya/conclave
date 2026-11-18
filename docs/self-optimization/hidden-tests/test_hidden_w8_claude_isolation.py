"""Hidden acceptance tests for W8 (Claude Code worker isolation).

Written by the pilot from the spec alone, run against the task branch before merging; not part
of the repository's own suite.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conclave.config import ArgMode, ConclaveConfig
from conclave.providers.base import AgentResult
from conclave.providers.profiles import ResolvedProfile, build_invocation
from conclave.providers.router import Engine, EngineRouter

_ISOLATION_FLAGS = (
    "--strict-mcp-config",
    "--disable-slash-commands",
    "--exclude-dynamic-system-prompt-sections",
)


def _assert_isolated(args: list[str], env: dict[str, str]) -> None:
    for flag in _ISOLATION_FLAGS:
        assert args.count(flag) == 1, (flag, args)
    i = args.index("--setting-sources")
    assert args[i + 1] == "project,local"
    assert "--mcp-config" not in args
    assert "--dangerously-skip-permissions" in args
    assert env.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY") == "1"


def test_builtin_default_is_isolated() -> None:
    assert ConclaveConfig().execution.worker_isolation is True
    inv = build_invocation(ResolvedProfile(name="p", arg_mode=ArgMode.flag))
    _assert_isolated(inv.args, inv.env)


def test_isolated_flag_mode_keeps_model_and_effort() -> None:
    inv = build_invocation(
        ResolvedProfile(
            name="p", arg_mode=ArgMode.flag, model="claude-opus-5-5", effort="high", isolated=True
        )
    )
    _assert_isolated(inv.args, inv.env)
    assert inv.args[inv.args.index("--model") + 1] == "claude-opus-5-5"
    assert inv.args[inv.args.index("--effort") + 1] == "high"
    for base in ("--print", "--output-format", "stream-json", "--verbose"):
        assert base in inv.args


def test_not_isolated_is_byte_for_byte_today() -> None:
    inv = build_invocation(
        ResolvedProfile(name="p", arg_mode=ArgMode.flag, model="m", effort="high", isolated=False)
    )
    assert inv.args == [
        "--dangerously-skip-permissions",
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "m",
        "--effort",
        "high",
    ]
    assert inv.env == {}


def test_env_mode_isolated() -> None:
    inv = build_invocation(
        ResolvedProfile(
            name="p",
            arg_mode=ArgMode.env,
            base_url="http://proxy.invalid",
            model="m",
            isolated=True,
        )
    )
    _assert_isolated(inv.args, inv.env)
    assert inv.env["ANTHROPIC_BASE_URL"] == "http://proxy.invalid"
    assert inv.env["ANTHROPIC_MODEL"] == "m"
    assert "--model" not in inv.args


def test_extra_env_still_wins() -> None:
    inv = build_invocation(
        ResolvedProfile(
            name="p",
            arg_mode=ArgMode.flag,
            isolated=True,
            extra_env={"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"},
        )
    )
    assert inv.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "0"


class _Capture:
    def __init__(self) -> None:
        self.profiles: list[ResolvedProfile] = []

    async def run_agent(self, *, profile: ResolvedProfile, **_: Any) -> AgentResult:
        self.profiles.append(profile)
        return AgentResult(ok=True, text="ok")


async def test_router_keeps_isolated_for_claude_code(tmp_path: Path) -> None:
    cap = _Capture()
    router = EngineRouter(Engine.opencode, claude=cap, opencode=cap)  # type: ignore[arg-type]
    for isolated in (True, False):
        await router.run_agent(
            profile=ResolvedProfile(
                name="system-default",
                arg_mode=ArgMode.inherit,
                model="claude-code/claude-opus-5-5",
                isolated=isolated,
            ),
            prompt="x",
            timeout_seconds=5,
            cwd=tmp_path,
        )
    assert [p.isolated for p in cap.profiles] == [True, False]
    assert all(p.arg_mode == ArgMode.flag for p in cap.profiles)


async def test_claude_subprocess_receives_flags_and_env(tmp_path: Path, monkeypatch: Any) -> None:
    import asyncio

    from conclave.providers.claude_cli import ClaudeCliProvider

    seen: dict[str, Any] = {}

    async def fake_exec(*argv: str, **kw: Any) -> Any:
        seen["argv"] = list(argv)
        seen["env"] = kw.get("env")
        raise FileNotFoundError("stop after capture")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await ClaudeCliProvider().run_agent(
        profile=ResolvedProfile(name="p", arg_mode=ArgMode.flag, model="m", isolated=True),
        prompt="x",
        timeout_seconds=5,
        cwd=tmp_path,
    )
    assert seen, "the provider never spawned the CLI"
    _assert_isolated(seen["argv"], seen["env"] or {})
