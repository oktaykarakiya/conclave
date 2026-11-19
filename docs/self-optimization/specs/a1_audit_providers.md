# A1 Audit: engine providers (stream parsing, usage limits, resume and cost, subprocess lifecycle, isolation)

Audit the engine providers for defects and fix every one you can demonstrate with a failing test.

SCOPE: `src/conclave/providers/` — `claude_cli.py`, `opencode_cli.py`, `quota.py`, `profiles.py`, `router.py`, `effort.py`, `base.py`, and their callers' contract (`engine/runner.py::AgentRunner.run`, the planning turn in `planning/session.py`). Specs to read: W8, W9, W10, W13 (items on cost), W17 in `docs/self-optimization/specs/`.

Areas to check in particular:
- Stream parsing of both CLIs: truncated or interleaved JSON lines, a `result` event missing or duplicated, non-UTF-8 output, very long lines, a process that exits without output; what `AgentResult` comes back in each case (ok, error, cost, turns, session id, `model_reported`).
- Usage-limit detection: false positives (a run that was served, overage in use, a limit message quoted in normal output or in the repository's own text) and false negatives; `resetsAt` missing, in the past, or absurdly far; the opencode balance/402 path.
- `dispatch_with_quota`: the resume path (session id missing, resume refused by the CLI, the resumed run hitting the limit again, cancel during the wait), cost accounting across resumes (`_own_cost`), and the gate shared by concurrent dispatches.
- Subprocess lifecycle: timeout, cancel and the refusal grace period kill the whole process group and reap it; no zombie, no leaked pipe, no orphaned `claude`/`opencode` child.
- Isolation: every flag and env variable W8/W10/W17 require is present on every dispatch path that sets `isolated` (retries and resumes included), `extra_env` wins, and nothing in the daemon's environment can re-enable what isolation turns off.

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
