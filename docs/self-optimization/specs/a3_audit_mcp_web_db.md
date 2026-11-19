# A3 Audit: MCP server, web API and database layer (validation, parity, transactions, migrations, config layering)

Audit the MCP server, the web API and the database layer for defects and fix every one you can demonstrate with a failing test.

SCOPE: `src/conclave/mcp/` (server, tools, stdio bridge), `src/conclave/web/` (api, routes, WebSocket stream, MCP routes), `src/conclave/db/` (repositories, migrations, the shared connection and `transaction()`), `src/conclave/config/` (resolver, models), `src/conclave/observability/`. Specs to read: W4, W14, W15, W16 in `docs/self-optimization/specs/`.

Areas to check in particular:
- Input validation on every REST endpoint and MCP tool: malformed ids, wrong types, negative or huge limits and offsets, unknown enum values, oversized bodies; a 4xx or an `isError`, never a 500 or an internal error; every list bounded.
- REST and MCP parity after W15: the same state gives the same answer; the compact defaults never drop a field the pilot guide promises.
- The shared connection: a write outside `transaction()` interleaving with one inside it, a transaction left open by an exception, a long statement holding the connection (W16's batched pruning included).
- Migrations: the runner on a fresh database, on a database at every earlier `schema_version`, and on a partially applied one; `schema_version` recorded atomically.
- Config layering (W4): `resolve_project_config` and the save functions never store what the project inherits and never lose a project's delta, including for the `system` settings W16 added.
- The WebSocket stream: a slow or vanished client, a burst of events, reconnect with `after_id`.
- The stdio bridge after W14: it never opens the database, and its offline answers stay identical to the daemon's.
- Known and handled in a separate task, do not fix here (report anything new you find about them): `execution.review_rounds_max` and an `on_fail` edge's `max_iterations` are accepted and shown but never bound the retry loop; an isolated Claude Code worker's env can be overridden by the worktree's `.claude/settings(.local).json`; parallel reviewers share one worktree.

WHAT COUNTS AS A DEFECT
- Wrong behaviour against this repository's own contracts: `AGENTS.md`, `src/conclave/mcp/pilot_guide.md`, the docstrings, and the specs in `docs/self-optimization/specs/` (W8–W18 were implemented in the last two days; read the ones for your area).
- Unhandled error paths: an exception that escapes to a 500, kills a worker or the daemon, or leaves a task, a gate or a lock in a state nothing recovers.
- Races on the single shared SQLite connection or between concurrent tasks; check-then-act without a transaction.
- Leaks: subprocesses not reaped or killed on cancel and timeout, file handles, asyncio tasks never awaited or cancelled.
- Silent data loss or silent truncation of irreplaceable prompt content; counts and costs that are wrong.
- Security: anything that lets a worker reach outside its worktree, its isolation, or other sessions; unvalidated input reaching a shell, a path or SQL.
Not in scope: style, renames, refactors, new features, docstring polish, performance unless pathological (quadratic on realistic input, blocking the event loop for more than about 100 ms).

HOW TO WORK
1. Read the code and its tests first; reproduce before you believe. A suspected defect you cannot demonstrate is not fixed.
2. For each demonstrated defect: write a deterministic failing test first (FakeProvider, fake executables, temporary homes; no network, no real LLM, never the live daemon or its MCP tools), then the smallest fix that makes it pass. One test per defect at least, named after the behaviour.
3. In your final message, a FINDINGS list, one entry per defect: file:line, the failing scenario in one sentence, the fix, the test name. Then a SUSPECTED list: what you suspected but could not demonstrate, and why. Then NOT A DEFECT: what you checked and found correct (one line each), so the pilot knows the coverage.
4. If you find no defect, change nothing and say so: an empty diff is an acceptable result. Do not invent work.

CONSTRAINTS
- Never weaken, skip or delete an existing test; a test that asserts the defect itself may be corrected, with the reason in FINDINGS.
- Keep the conventions in `AGENTS.md` (config in SQLite, append-only migrations, 4xx not 500, never cut irreplaceable prompt content, pilot guide current).
- Quality gate (must stay green): `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit && npm run build` (rebuild and commit `src/conclave/web/static/` only if frontend source changed).
