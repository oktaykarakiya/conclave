# AGENTS.md

## Project

Conclave — a portable, web-driven autonomous AI coding team that drives the `opencode` CLI through a plan → develop → multi-agent review → green-gate → commit/merge loop. Attaches to any git repo via isolated worktrees. Controlled entirely from a web UI.

## Stack

- **Backend**: Python 3.12+, FastAPI, SQLite (aiosqlite), async
- **Frontend**: React 19, TypeScript (strict), Tailwind 4, Vite 6
- **Engine**: drives `opencode` headless (default); `claude` CLI available as legacy fallback (`CONCLAVE_ENGINE=claude`). Model/provider selection lives in opencode — Conclave inherits it. DeepSeek is the intended default.
- **Packaging**: hatchling, `pip install -e ".[dev]"`

## Quality gate (always run before declaring done)

```bash
ruff check src tests && mypy && pytest -q         # Python: 402 tests (deterministic, zero LLM cost)
cd frontend && npx tsc --noEmit && npm run build  # TypeScript strict + rebuild SPA
```

- Tests use `tests/integration/fake_provider.py` — a deterministic provider test-double that inspects prompts to impersonate the correct agent role (planner, reviewer, developer, repo-analyst). No real LLM calls.
- `mypy --strict` — everything must be fully typed. Only suppression: `aiosqlite` (no stubs).
- `ruff` line length 100, Python 3.12, rules: E, F, I, UP, B, ASYNC, RUF. Ignores: B008 (FastAPI `Depends`), ASYNC230/240 (pathlib/open in async is fine here).

## Commands

```bash
./conclave                         # auto-bootstraps venv, serves on 0.0.0.0:8700
CONCLAVE_ENGINE=opencode ./conclave # explicit (opencode is the default)
CONCLAVE_ENGINE=claude ./conclave  # legacy claude-CLI fallback

ruff check src tests               # lint only (no fix)
mypy                               # type-check (reads config from pyproject.toml)
pytest -q                          # all tests
pytest tests/unit/test_engine_logic.py -q         # single test file
pytest -k "test_planning" -q                      # filter by name

cd frontend && npm run dev         # Vite dev server with HMR (connects to a running backend)
cd frontend && npm run typecheck   # TS type-check only
cd frontend && npm run build       # build SPA into src/conclave/web/static/
```

## Architecture

| Directory | Purpose |
|---|---|
| `src/conclave/engine/` | Orchestrator loop (worktree → venv → baseline → plan → develop → review → verdict → gate → commit/merge), gate, reviewers, pipeline |
| `src/conclave/planning/` | Agent-ception multi-agent planning sessions |
| `src/conclave/db/` | SQLite layer — single shared async connection + lock + `transaction()`, append-only migrations, repositories |
| `src/conclave/providers/` | `claude` CLI provider + `opencode` CLI provider + engine-profile invocation |
| `src/conclave/web/` | FastAPI app, routes, WebSocket stream, served SPA |
| `src/conclave/runtime.py` | Per-project worker daemon |
| `frontend/src/` | React SPA — `ui.tsx` primitives, `pages/*` one per tab |

## Critical conventions

- **Config is SQLite-backed and edited via the UI** — never introduce YAML/config files.
- **DB migrations are append-only**: add a new entry to the `MIGRATIONS` list in `src/conclave/db/migrations/`. Never edit an already-applied migration. The runner records `schema_version` atomically.
- **Validate API inputs** — return 4xx, never 500 on bad input. Bound list endpoints.
- **The built SPA** (`src/conclave/web/static/`) is committed so the daemon serves the UI without Node at runtime. Always rebuild after frontend changes.
- **Security**: single-user, intentionally unauthenticated. Binds `0.0.0.0` by default for LAN use. Set `CONCLAVE_HOST=127.0.0.1` for loopback-only. Multi-user auth is out of scope.
- **Env vars**: `CONCLAVE_HOST` (default `0.0.0.0`), `CONCLAVE_PORT` (default `8700`), `CONCLAVE_HOME` (default `~/.local/share/conclave`), `CONCLAVE_ENGINE` (default `opencode`; set `claude` for legacy), `PYTHON` (override interpreter for venv bootstrap).
- **Branch `conclave-selftest`** exists unmerged — contains BMad scale-adaptive planning. Do not accidentally work on it unless that feature is explicitly requested.

## Testing

- `tests/conftest.py` provides a `db` fixture (async, per-test SQLite in `tmp_path`).
- Integration tests use `FakeProvider` from `tests/integration/fake_provider.py` — it returns deterministic JSON based on prompt keywords (e.g., `"reviewer"` → pass verdict, `"planner"` → plan JSON).
- `pytest-asyncio` mode is `auto`; `PYTHONPATH=src` (set in pyproject.toml).
- Filterwarnings: errors on all warnings except `PytestUnraisableExceptionWarning` (benign asyncio subprocess GC race in per-test event loops).
