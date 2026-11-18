# W8 Isolate Claude Code worker dispatches (worker_isolation, default on)

Isolate Claude Code worker dispatches: no MCP servers, user plugins, skills or auto-memory (worker_isolation, default on).

PROBLEM (measured on this host, Claude Code 2.1.326, from the stream-json `system/init` event of a dispatch built exactly like `ClaudeCliProvider` builds one today): `claude --dangerously-skip-permissions --print --output-format stream-json --verbose [--model M] [--effort E]`, run in the task worktree with the daemon's environment, loads the operator's whole Claude Code setup:
- 8 MCP servers, including `conclave` itself (the live daemon's MCP endpoint, 64 tools: `conclave_merge_task`, `conclave_patch_config`, `conclave_reload_system`, `conclave_delete_task`, ...), Greptile and claude.ai connectors. With permissions bypassed, any worker can merge tasks or change the config of the daemon that is running it.
- 15 plugins (4 of them start language servers), 43 skills, 81 slash commands.
- auto-memory, which writes under `~/.claude/projects/<worktree path>/memory/` for every worktree.
- A one-line prompt costs 28.6k input tokens.
With `--strict-mcp-config --setting-sources project,local --disable-slash-commands --exclude-dynamic-system-prompt-sections` and `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, the same dispatch had no MCP servers, only Claude Code's built-in plugins, 0 skills, 0 slash commands, no memory path, bypassPermissions still active (the Read tool worked), and 21.5k input tokens.

REQUIRED BEHAVIOUR
1. New setting `execution.worker_isolation: bool`, built-in default `true`, layered like every other setting (built-in -> global -> project delta), with a Field description, so it appears in the config schema. Add a toggle for it to the Settings page next to the other execution switches (`frontend/src/pages/SettingsView.tsx`), with a one-line explanation; rebuild the SPA and commit `src/conclave/web/static/`.
2. Carry it to every dispatch as `ResolvedProfile.isolated: bool` (dataclass default `True`, secure by default). `AgentRunner._resolve_profile` sets it from the effective config. The planning-session dispatch (`planning/session.py`, which calls `provider.run_agent` itself) sets it from that project's effective config too. Check there is no other `run_agent` caller (today: `engine/runner.py`, `planning/session.py`, and the router delegating to the two providers).
3. `build_invocation` / `ClaudeCliProvider`, when `profile.isolated` is true, add exactly these args: `--strict-mcp-config` (and no `--mcp-config`, so no MCP server starts), `--setting-sources project,local` (user-level settings, and the plugins and hooks they enable, are not loaded; the target repository's own project/local settings still apply), `--disable-slash-commands` (no skills or commands), `--exclude-dynamic-system-prompt-sections` (cwd, env info and git status move into the first user message, so the system prompt stays identical across worktrees and caches across dispatches); and this environment variable: `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`. `--dangerously-skip-permissions`, the base args, `--model`/`--effort` flag handling, env-mode routing (`ArgMode.env`) and `extra_env` (applied last, can still override) are unchanged. When `isolated` is false, args and env are byte-for-byte what they are today.
4. The router (`providers/router.py`) keeps the field when it rewrites the profile for Claude Code (`dataclasses.replace` keeps it; assert it in a test).
5. Under isolation, `claude-code/default` (no `--model`) means Claude Code's own default model, because the user's settings file is no longer read. Say so in the pilot guide.
6. Verify every flag and the env var against the installed CLI (`claude --help`) and the current official Claude Code CLI reference; cite both in the plan. State the minimum Claude Code version that has all of them in the pilot guide. No runtime flag detection: an older CLI that rejects a flag fails the dispatch with its own error, which already surfaces in `agent.result.error`.
7. This task changes the Claude Code path only. The opencode provider only receives the new profile field (it is used by a follow-up task); do not change its behaviour here.

TESTS (deterministic, no real CLI)
- `build_invocation`: isolated -> exact args (order: existing cli_flags, base args, isolation flags, then model/effort flags, or any fixed order you document) and env; not isolated -> identical to today's args/env; env-mode + isolated; `extra_env` overriding the isolation env var.
- `ClaudeCliProvider.run_agent`: monkeypatch `asyncio.create_subprocess_exec` to capture the argv and `env` actually passed; assert the flags and `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` for an isolated profile, their absence otherwise.
- Router: the Claude Code profile it builds keeps `isolated` (true and false).
- Config: built-in default true; a project delta of false resolves to false through `resolve_project_config`; global false + project unset -> false.
- Every dispatch path: with the fake provider recording the profile it receives (extend `tests/integration/fake_provider.py`), a task run and a planning-session turn dispatch with `isolated=True` by default, and with `False` when the project sets `execution.worker_isolation: false`.
- Frontend: `npx tsc --noEmit` and `npm run build` pass; the toggle reads and writes `execution.worker_isolation`.

DOCS (same change)
- `src/conclave/mcp/pilot_guide.md`: section 3 (the setting, what an isolated worker gets and does not get, `claude-code/default` under isolation, minimum CLI version) and section 11 (pitfall: a worker that needs an MCP server or a user plugin needs `worker_isolation: false` for that project). Keep `pilot_brief.md` consistent if it mentions engines.
- `AGENTS.md` "Critical conventions": workers run isolated by default (one bullet).

CONSTRAINTS
- Never weaken or skip a test. Never call the live daemon or its MCP tools. Keep everything not listed here unchanged.
- Quality gate (must stay green): `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit && npm run build`.
