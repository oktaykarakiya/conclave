"""Engine Profiles: how an agent dispatch is parameterized.

A profile keeps Conclave on the ``claude`` CLI but lets each agent target the system
default, Anthropic-direct, or an Anthropic-compatible endpoint such as DeepSeek. The
profile composes the exact CLI args + environment for a dispatch:

- ``inherit`` — pass nothing → the host's logged-in ``claude`` default (the unset case);
- ``flag``    — ``--model``/``--effort`` flags (native Claude / Anthropic-direct);
- ``env``     — ``ANTHROPIC_*`` / ``CLAUDE_CODE_*`` env routing (DeepSeek et al.).

An ``isolated`` profile (the default; ``execution.worker_isolation``) also keeps the operator's
own Claude Code setup out of the dispatch (see ``_ISOLATION_ARGS``).

``build_invocation`` is pure and the most heavily tested function here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import ArgMode

# Always-on CLI args: headless print mode + NDJSON event stream (live output for the UI; the
# final ``result`` event carries the result/usage/cost fields). stream-json needs --verbose.
_BASE_ARGS: tuple[str, ...] = ("--print", "--output-format", "stream-json", "--verbose")
_DEFAULT_CLI_FLAGS: tuple[str, ...] = ("--dangerously-skip-permissions",)

# Worker isolation (Claude Code 2.1.101+). No MCP server starts: --strict-mcp-config ignores
# every configured server and no --mcp-config is passed. Only the repository's project/local
# settings load, so user settings and the plugins and hooks they enable stay out. No skills or
# slash commands. Per-machine sections (cwd, env info, git status) move from the system prompt
# into the first user message, so the system prompt is the same in every worktree and caches
# across dispatches. The environment variable turns auto-memory off.
_ISOLATION_ARGS: tuple[str, ...] = (
    "--strict-mcp-config",
    "--setting-sources",
    "project,local",
    "--disable-slash-commands",
    "--exclude-dynamic-system-prompt-sections",
)
_DISABLE_AUTO_MEMORY_ENV = "CLAUDE_CODE_DISABLE_AUTO_MEMORY"


@dataclass(frozen=True)
class ResolvedProfile:
    """A fully-resolved dispatch spec (profile defaults overlaid with agent overrides
    and the resolved secret value)."""

    name: str
    arg_mode: ArgMode
    base_url: str | None = None
    auth_token: str | None = None  # sensitive: resolved secret value
    model: str | None = None
    subagent_model: str | None = None
    effort: str | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    cli: str = "claude"
    cli_flags: tuple[str, ...] = _DEFAULT_CLI_FLAGS
    cli_agent: str | None = None
    thinking: bool = False
    pure: bool = False
    isolated: bool = True  # worker isolation; secure by default
    # Continue this engine session instead of starting a new one (after a usage-limit wait).
    resume_session_id: str | None = None


@dataclass(frozen=True)
class Invocation:
    args: list[str]
    env: dict[str, str]


def build_invocation(profile: ResolvedProfile) -> Invocation:
    """Compose the CLI args and environment overrides for a dispatch (pure).

    Args come in this order: ``cli_flags``, the base args, the isolation flags (when
    ``profile.isolated``), ``--resume <id>`` (when ``profile.resume_session_id`` is set), then
    ``--model``/``--effort`` (flag mode). Without isolation or a session to resume nothing is
    added, so the args and environment are exactly those of a profile that predates them.
    """
    args: list[str] = [*profile.cli_flags, *_BASE_ARGS]
    env: dict[str, str] = {}

    if profile.isolated:
        args += _ISOLATION_ARGS
        env[_DISABLE_AUTO_MEMORY_ENV] = "1"
    if profile.resume_session_id:
        args += ["--resume", profile.resume_session_id]

    if profile.arg_mode is ArgMode.flag:
        if profile.model:
            args += ["--model", profile.model]
        if profile.effort:
            args += ["--effort", profile.effort]
    elif profile.arg_mode is ArgMode.env:
        if profile.base_url:
            env["ANTHROPIC_BASE_URL"] = profile.base_url
        if profile.auth_token:
            env["ANTHROPIC_AUTH_TOKEN"] = profile.auth_token
        if profile.model:
            env["ANTHROPIC_MODEL"] = profile.model
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = profile.model
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = profile.model
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = profile.subagent_model or profile.model
        if profile.subagent_model:
            env["CLAUDE_CODE_SUBAGENT_MODEL"] = profile.subagent_model
        if profile.effort:
            env["CLAUDE_CODE_EFFORT_LEVEL"] = profile.effort
    # arg_mode is inherit: add nothing beyond the base args.

    # Advanced escape hatch always applies last (and can override the above).
    env.update(profile.extra_env)
    return Invocation(args=args, env=env)
