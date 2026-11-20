# AGENTS.md

## Project

Conclave — a portable, web-driven autonomous AI coding team that drives the `opencode` CLI through a plan → develop → multi-agent review → green-gate → commit/merge loop. Attaches to any git repo via isolated worktrees. Controlled entirely from a web UI.

## Stack

- **Backend**: Python 3.12+, FastAPI, SQLite (aiosqlite), async
- **Frontend**: React 19, TypeScript (strict), Tailwind 4, Vite 6
- **Engines**: `opencode` headless (default) and the `claude` (Claude Code) CLI, routed per dispatch by `providers/router.py`: a `claude-code/<model>` agent model runs on Claude Code, anything else on the default engine (`CONCLAVE_ENGINE`, `opencode` unless `claude`). The built-in agent model is DeepSeek V4.1 Flash (`deepseek/deepseek-flash`).
- **Packaging**: hatchling, `pip install -e ".[dev]"`

## Quality gate (always run before declaring done)

```bash
ruff check src tests && mypy && pytest -q         # Python: 1520 tests (deterministic, zero LLM cost)
cd frontend && npx tsc --noEmit && npm run build  # TypeScript strict + rebuild SPA
cd frontend && npx vitest run                     # SPA unit tests (pure modules, no DOM)
```

- Tests use `tests/integration/fake_provider.py` — a deterministic provider test-double that inspects prompts to impersonate the correct agent role (planner, reviewer, developer, post-mortem, repo-analyst; `FakeProvider.role_of`). No real LLM calls.
- `mypy --strict` — everything must be fully typed. Only suppression: `aiosqlite` (no stubs).
- `ruff` line length 100, Python 3.12, rules: E, F, I, UP, B, ASYNC, RUF. Ignores: B008 (FastAPI `Depends`), ASYNC230/240 (pathlib/open in async is fine here).

## Commands

```bash
./conclave                         # auto-bootstraps venv, serves on 0.0.0.0:8700
CONCLAVE_ENGINE=opencode ./conclave # explicit (opencode is the default)
CONCLAVE_ENGINE=claude ./conclave  # Claude Code as the default engine

ruff check src tests               # lint only (no fix)
mypy                               # type-check (reads config from pyproject.toml)
pytest -q                          # all tests
pytest tests/unit/test_engine_logic.py -q         # single test file
pytest -k "test_planning" -q                      # filter by name

cd frontend && npm run dev         # Vite dev server with HMR (connects to a running backend)
cd frontend && npm run typecheck   # TS type-check only
cd frontend && npx vitest run      # SPA unit tests
cd frontend && npm run build       # build SPA into src/conclave/web/static/
```

## Architecture

| Directory | Purpose |
|---|---|
| `src/conclave/engine/` | Orchestrator loop (worktree → venv → baseline → plan → develop → review → verdict → gate → commit/merge), gate, reviewers, pipeline |
| `src/conclave/planning/` | Agent-ception multi-agent planning sessions |
| `src/conclave/db/` | SQLite layer — single shared async connection + lock + `transaction()`, append-only migrations, repositories. The one exception is operator SQL (`query_interaction_logs_readonly`): it runs on its own read-only connection in a worker thread, stopped after 10 s, with an authorizer that refuses the `secrets` table (and `sqlite_dbpage`) |
| `src/conclave/providers/` | `opencode` + `claude` CLI providers, per-dispatch engine router, engine-profile invocation |
| `src/conclave/web/` | FastAPI app, routes, WebSocket stream, served SPA |
| `src/conclave/runtime.py` | Per-project worker daemon |
| `frontend/src/` | React SPA — `ui.tsx` primitives, `pages/*` one per tab |

## Critical conventions

- **Config is SQLite-backed and edited via the UI** — never introduce YAML/config files.
- **Config is layered; projects store deltas**: effective = built-in → global (`global-defaults` preset) → project branch (`default_branch` as `execution.target_branch`) → project delta → `agent_overrides`. Resolve with `resolve_project_config` and write with `save_project_config` / `create_project_with_config` / `save_global_config` (`config/resolver.py`), which store only what differs from what the project inherits; never resolve config from `project.config` alone, write it directly, or copy global settings into a project. Every write of a workflow graph runs under `graph_edit_lock(db)` (`config/resolver.py`, lock order: graph, then config, then database): the project and global config writers take it themselves, and code that reads a graph and saves a change of it (the MCP graph tools, the evolver's compare-and-apply) holds it across both and saves with `save_project_workflow(..., graph_locked=True)`, since the lock is not re-entrant.
- **Never cut irreplaceable prompt content** (task request, plan, reviewer feedback, steer, planning prompt). Only recoverable bulk (diffs, gate/test and setup output) is fitted, via `engine/context_limits.py` under the `context.*` limits, with a visible marker and a `context_truncated` event.
- **DB migrations are append-only**: add a new entry to the `MIGRATIONS` list in `src/conclave/db/migrations/`. Never edit an already-applied migration. The runner records `schema_version` atomically.
- **Validate API inputs** — return 4xx, never 500 on bad input. Bound list endpoints. An unknown project or task id answers 404 (as the MCP tool's `isError`) and changes and emits nothing.
- **The built SPA** (`src/conclave/web/static/`) is committed so the daemon serves the UI without Node at runtime. Always rebuild after frontend changes.
- **Security**: single-user, intentionally unauthenticated. Binds `0.0.0.0` by default for LAN use. Set `CONCLAVE_HOST=127.0.0.1` for loopback-only. Multi-user auth is out of scope.
- **Workers run isolated by default** (`execution.worker_isolation`, built-in `true`): every dispatch carries it as `ResolvedProfile.isolated`, and `build_invocation` (`providers/profiles.py`) then gives a Claude Code worker `--strict-mcp-config --setting-sources project,local --disable-slash-commands --exclude-dynamic-system-prompt-sections`, the `--tools` allowlist of built-in tools (`ISOLATED_WORKER_TOOLS`, each excluded tool documented there: no messaging other sessions, scheduling, notifications, workflows or leaving the worktree) and `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`, `CLAUDE_CODE_DISABLE_CRON=1`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`, both in its environment and as inline command-line settings (`--settings '{"env": {...}}'`, no file), because a settings file's `env` block applies over the inherited environment and `--settings` outranks the project and local files (in a git worktree Claude Code reads `settings.local.json` from the main checkout's root, where a worker could write one); those files otherwise still apply (permissions, hooks): no MCP servers (Conclave's own included), no user settings (nor the plugins and hooks they enable), no skills, no auto-memory, no `CLAUDE.md` or `AGENTS.md` loading (Conclave sends the rule files, see the next bullet), and no background tasks or cron jobs that wake a session for another turn after its answer. So long commands still run in the foreground, `ClaudeCliProvider.run_agent` gives an isolated dispatch `BASH_MAX_TIMEOUT_MS` (its timeout) and `BASH_DEFAULT_TIMEOUT_MS` (20 minutes at most); the profile's `extra_env` wins over all of these (in the `--settings` env too). An isolated opencode worker (`OpenCodeCliProvider`, `providers/opencode_cli.py`) gets `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1` and `OPENCODE_DISABLE_EXTERNAL_SKILLS=1` (no `.claude`/`.agents` skills; opencode's own skill folders and built-in skill still load) and every MCP server of its resolved config disabled through `OPENCODE_CONFIG_CONTENT` (`{"mcp": {"<name>": {"enabled": false}}}`, deep-merged into the daemon's own value when that is a JSON object). opencode has no switch for all servers, so the names come from `opencode debug config`, run in the dispatch's cwd (30 s timeout) and cached per (binary, cwd) for the provider's lifetime; a failed discovery logs a warning, is not cached, and that dispatch keeps its MCP servers. Like the Claude Code provider, it passes the subprocess an explicit `env` (the daemon's environment, the isolation overrides, then `profile.extra_env` last, the same on every retry and resume), so `extra_env` reaches both engines and wins over every isolation variable. A new dispatch path must set `isolated` from the project's effective config.
- **Project rule files reach each agent once, per engine** (`engine/project_rules.py`): PROJECT RULES holds the workspace-level `AGENTS.md` (beside the attached checkout) and the root's first of `AGENTS.md`, `CONCLAVE.md`, `CLAUDE.md`. `read_project_rules` reads them once per task (orchestrator) or per planning turn; `rules_for_dispatch` chooses per dispatch, from the engine `resolve_engine` picks and the env that engine's provider passes (`AgentRunner.run`, the planning turn). A file the engine loads natively is left out, never cut: opencode loads the root `AGENTS.md`, else `CLAUDE.md` (not under `OPENCODE_DISABLE_CLAUDE_CODE(_PROMPT)`, nothing under `OPENCODE_DISABLE_PROJECT_CONFIG`); an isolated Claude Code worker loads none, so it also gets the root `CLAUDE.md` and its `@path` imports, resolved as Claude Code does but only inside the worktree; a worker that is not isolated loads `CLAUDE.md` itself and still gets `AGENTS.md` (an accepted duplicate). `CONCLAVE.md` is Conclave-only and reaches every engine. A new dispatch path passes `ProjectRules` through `AgentRunner.run` (or `rules_for_dispatch`), never a pre-built rules string.
- **Usage limits never fail work**: a provider reports an engine's usage-limit or balance refusal as `AgentResult.quota`, and `dispatch_with_quota` (`providers/quota.py`) waits on the daemon-wide `QuotaGate` (`Daemon.quota_gate`) until the reset, then resumes the session. `AgentRunner.run` and the planning session's turn are the only callers of `provider.run_agent` outside `providers/`: a new dispatch path goes through one of them (or `dispatch_with_quota`) with the daemon's gate.
- **Env vars**: `CONCLAVE_HOST` (default `0.0.0.0`), `CONCLAVE_PORT` (default `8700`), `CONCLAVE_HOME` (default `~/.local/share/conclave`), `CONCLAVE_ENGINE` (default `opencode`; `claude` for Claude Code), `CONCLAVE_OPENCODE_BIN` / `CONCLAVE_CLAUDE_BIN` (binary overrides; the launcher autodiscovers), `PYTHON` (override interpreter for venv bootstrap).
- **Branch `conclave-selftest`** exists unmerged — contains BMad scale-adaptive planning. Do not accidentally work on it unless that feature is explicitly requested.
- **The live event stream** (`/ws/stream`, SSE `/api/events/stream`; `events/stream.py`) replays the persisted events after the client's `after_id` before the live ones and sends a gap frame, `{"type": "stream.gap", "reason": ...}` without an `id`, where events were lost. In the SPA, react to events with `useStreamListener` (`useStream.ts`): it calls `onEvent` once per event in arrival order, and `onResync` on a gap frame or a reconnect that cannot resume, where the component refetches what it derives from events (coalesce refetches with `useCoalesced`). Never react through an effect over React state holding the latest event: events that arrive before a render are skipped.
- **Keep the MCP pilot docs current**: when MCP tools or pilot-relevant behaviour/defaults change, update `src/conclave/mcp/pilot_guide.md` (and `pilot_brief.md`) in the same change; the drift tests enforce tool coverage.

## Testing

- `tests/conftest.py` provides a `db` fixture (async, per-test SQLite in `tmp_path`).
- Integration tests use `FakeProvider` from `tests/integration/fake_provider.py` — it returns deterministic JSON based on prompt keywords (e.g., `"reviewer"` → pass verdict, `"planner"` → plan JSON).
- A test project without a test command fails its tasks with `no_test_command` before any dispatch: give it one (e.g. `execution.baseline_test_command: "echo ok"`), or `execution.allow_no_test_command: true` when the test is about something else.
- `pytest-asyncio` mode is `auto`; `PYTHONPATH=src` (set in pyproject.toml).
- Filterwarnings: errors on all warnings except `PytestUnraisableExceptionWarning` (benign asyncio subprocess GC race in per-test event loops).
- SPA logic that runs without React or the DOM lives in pure modules with a `*.test.ts` beside them (e.g. `frontend/src/eventStream.ts`), run by `npx vitest run` in the node environment (no DOM).
