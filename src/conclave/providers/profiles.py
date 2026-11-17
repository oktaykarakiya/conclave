"""Engine Profiles: how an agent dispatch is parameterized.

A profile keeps Conclave on the ``claude`` CLI but lets each agent target the system
default, Anthropic-direct, or an Anthropic-compatible endpoint such as DeepSeek. The
profile composes the exact CLI args + environment for a dispatch:

- ``inherit`` — pass nothing → the host's logged-in ``claude`` default (the unset case);
- ``flag``    — ``--model``/``--effort`` flags (native Claude / Anthropic-direct);
- ``env``     — ``ANTHROPIC_*`` / ``CLAUDE_CODE_*`` env routing (DeepSeek et al.).

An ``isolated`` profile (the default; ``execution.worker_isolation``) also keeps the operator's
own Claude Code setup out of the dispatch, limits it to the built-in tools a coding task needs
and turns off background tasks (see ``_ISOLATION_ARGS`` and ``ISOLATED_WORKER_TOOLS``).

``build_invocation`` is pure and the most heavily tested function here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import ArgMode

# Always-on CLI args: headless print mode + NDJSON event stream (live output for the UI; the
# final ``result`` event carries the result/usage/cost fields). stream-json needs --verbose.
_BASE_ARGS: tuple[str, ...] = ("--print", "--output-format", "stream-json", "--verbose")
_DEFAULT_CLI_FLAGS: tuple[str, ...] = ("--dangerously-skip-permissions",)

# The built-in tools an isolated worker gets (``--tools``, names as of Claude Code 2.1.326; every
# built-in left out is removed). The nine a coding task needs: Task (the CLI's alias of the Agent
# tool, for subagents), Bash, Read, Edit, Write, NotebookEdit, WebFetch, WebSearch, ToolSearch.
# Then the CLI's read/search and todo tools, which on Linux and macOS are only there when named:
# Glob, Grep, LSP (inactive unless the repository's own settings enable a code-intelligence
# plugin), TaskCreate, TaskGet, TaskList, TaskUpdate, TodoWrite; and PowerShell, Bash's
# counterpart on Windows (absent elsewhere), which ``--tools`` would remove there. Left out:
# - CronCreate, CronDelete, CronList, ScheduleWakeup: schedule prompts that wake the session
#   after its final answer;
# - ListAgents, SendMessage: reach other Claude sessions on the machine, the operator's own
#   included;
# - RemoteTrigger: creates and runs scheduled cloud agents on the account;
# - PushNotification: notifies the account's owner;
# - DesignSync: reads and writes the account's Claude Design projects;
# - Workflow: starts background multi-agent workflows;
# - Monitor: a background watcher, which CLAUDE_CODE_DISABLE_BACKGROUND_TASKS leaves on;
# - TaskStop: stops background tasks, which an isolated worker cannot start;
# - EnterWorktree, ExitWorktree: leave the task's worktree;
# - ReportFindings: hands findings to a host UI instead of the final text Conclave reads;
# - every other built-in: needs a person at the terminal, an MCP server, skills or a claude.ai
#   feature.
ISOLATED_WORKER_TOOLS: tuple[str, ...] = (
    "Task",
    "Bash",
    "Read",
    "Edit",
    "Write",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "ToolSearch",
    "Glob",
    "Grep",
    "LSP",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
    "TodoWrite",
    "PowerShell",
)

# Worker isolation (Claude Code 2.1.101+). No MCP server starts: --strict-mcp-config ignores
# every configured server and no --mcp-config is passed. Only the repository's project/local
# settings load, so user settings and the plugins and hooks they enable stay out. No skills or
# slash commands. Per-machine sections (cwd, env info, git status) move from the system prompt
# into the first user message, so the system prompt is the same in every worktree and caches
# across dispatches. Only the ISOLATED_WORKER_TOOLS built-ins are available.
_ISOLATION_ARGS: tuple[str, ...] = (
    "--strict-mcp-config",
    "--setting-sources",
    "project,local",
    "--disable-slash-commands",
    "--exclude-dynamic-system-prompt-sections",
    "--tools",
    ",".join(ISOLATED_WORKER_TOOLS),
)
# Auto-memory off. No background tasks or cron jobs: a ``--print`` run waits for them and wakes
# the session for another turn after its final answer (the provider sets the Bash timeouts so
# long commands still run in the foreground).
_ISOLATION_ENV: dict[str, str] = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    "CLAUDE_CODE_DISABLE_CRON": "1",
}


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
        env.update(_ISOLATION_ENV)
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
