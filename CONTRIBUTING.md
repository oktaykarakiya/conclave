# Contributing to Conclave

Thanks for your interest! Conclave is a personal/self-hostable tool; contributions and issues are
welcome.

## Licensing of contributions

Conclave is source-available under the [Business Source License 1.1](./LICENSE), with commercial
licenses offered by the Licensor. By submitting a contribution (a pull request, patch or any other
code or documentation), you confirm that you wrote it or otherwise have the right to submit it, and
you grant the Licensor, Oktay Karakiya, a perpetual, worldwide, irrevocable, royalty-free license to
use, modify, sublicense and relicense it under any terms, including the Business Source License,
commercial licenses and the Change License named in `LICENSE`.
Contributions are accepted only on these terms; if you do not agree, please do not submit one.

## Dev setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cd frontend && npm install && cd ..
```

## The quality gate (must pass before merge)

This is exactly what Conclave's own green-gate enforces on every task:

```bash
ruff check src tests && mypy && pytest -q   # Python: lint + strict types + 222 tests
cd frontend && npx tsc --noEmit             # TypeScript: strict
cd frontend && npm run build                # rebuild the SPA into src/conclave/web/static
```

- Tests are deterministic and use a fake provider — **no LLM cost** to run the suite.
- Python is `mypy --strict`; keep new code fully typed. Line length 100, `ruff` rules in
  `pyproject.toml`.
- The frontend gate is **TypeScript-only** (the Python gate doesn't build the SPA) — run `tsc` +
  `npm run build` and visually check the page before committing frontend changes.

## Architecture (where things live)

- `src/conclave/engine/` — the orchestrator loop (worktree → setup/venv → baseline → plan → develop →
  review → grounded verdict → green-gate → commit/merge), gate, verdicts, pipeline.
- `src/conclave/planning/` — Agent-ception multi-agent planning sessions.
- `src/conclave/db/` — SQLite (single shared async connection + a lock/`transaction()`), append-only
  migrations, repositories, models.
- `src/conclave/providers/` — the `claude` CLI provider + engine-profile invocation.
- `src/conclave/web/` — FastAPI app, routes, WebSocket stream, served SPA.
- `src/conclave/runtime.py` — the per-project worker daemon.
- `frontend/src/` — React + TS + Tailwind SPA (`ui.tsx` primitives, `pages/*` one per tab).

## Conventions

- Config is **SQLite-backed and edited via the UI** — never hand-edit YAML or introduce config files.
- DB migrations are **append-only**: add a new entry to the `MIGRATIONS` list in
  `src/conclave/db/migrations/` (the runner records `schema_version` atomically); never edit an
  already-applied migration.
- Keep the web API resilient: validate inputs (return 4xx, never 500 on bad input) and bound list
  endpoints.
