# A2 Audit: task engine (exit paths, review completeness, gate, merging, evolver, project rules)

Audit the task engine for defects and fix every one you can demonstrate with a failing test.

SCOPE: `src/conclave/engine/` — `orchestrator.py`, `workflow_evolver.py`, `gate.py`, `task_merge.py`, `project_rules.py`, `context_limits.py`, `test_integrity.py`, `pipeline.py`, `verdict.py`, `baseline.py`, `worktree.py`, `gitio.py`, `graph_executor.py`, `runner.py`, and `runtime.py`'s worker loop. Specs to read: W2, W3, W11, W12, W13, W18 in `docs/self-optimization/specs/`.

Areas to check in particular:
- Every exit path of `process_task` (success, each failure reason, cancel at each stage, an exception in each stage, a usage-limit wait at each stage): the task ends in a terminal state with a reason, the worktree, branch and locks are released or deliberately preserved, `_task_housekeeping` runs, and no event or checkpoint claims something that did not happen.
- Review completeness: a mandatory reviewer that never answers, answers twice, answers for another attempt, or is disabled mid-task; parallel reviewers and the retry loop's `max_iterations`.
- The gate and the test-integrity guard: exit codes and outcomes (`timed_out`, `missing_command`, `cancelled`), quarantine patterns, a baseline that is red, output fitting under `context.*` with its marker and event.
- Merging: `merge_lock`, a target branch that moved, a conflict, a dirty main checkout, a branch deleted by hand.
- The workflow evolver after W18: rejected-proposal suppression, the quality-failure-only signals, the clean streak.
- Project rules (W11): which rule file reaches which engine, `@path` imports outside the worktree, symlinks.
- Reported by the providers audit (A1), not yet checked: the parallel reviewers' `asyncio.gather` in `orchestrator.py` (around line 2528) has no `return_exceptions`, so one raising dispatch leaves its siblings running unawaited and the task ends as an unhandled exception; and spawn failures other than `FileNotFoundError` (`PermissionError`, `ENOEXEC`) escape `run_agent` to the orchestrator's catch-all.
- Known and handled in a separate task, do not fix here (report anything new you find about them): parallel reviewers share one worktree, so one reviewer's temporary file swap can disturb another's test run (the commit is safe: the reviewed tree is restored before the gate); and an isolated Claude Code worker's env can be overridden by the worktree's `.claude/settings(.local).json`.

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
