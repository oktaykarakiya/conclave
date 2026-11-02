# Pilot backlog (observed while piloting; candidates for Conclave tasks)

Resolved in run 2 (see `README.md`): attach upsert (W7), cancellation retries (W7), hot-reload
watcher on by default (W5), gate passing without a test command (W3), dead test-integrity guard
(W3), workflow evolver without off switch or history (W6), blanket 6k prompt compaction (W2).
The router's enum-identity bug after hot reload was fixed in 062367a; commit rc in R3.

Open:

1. [MCP] parity gaps: config schema, models/engines, usage, HITL state, quarantine integrity,
   needs-human (→ spec r4).
2. [hot-reload] With the watcher off by default this rarely matters, but a manual reload still
   re-creates enum classes under long-lived objects: audit remaining `is <Enum>.member` checks on
   long-lived state.
3. [MCP] conclave_get_task wraps as {"task": ...}; conclave_get_task_events pages results (needs an
   after_id loop) — documented in the pilot guide; also put it in the tool descriptions.
4. [mcp stdio] `conclave-mcp` falls back to an in-process daemon on the same DB when HTTP is
   unreachable — possible second worker on the same data dir (bypasses the daemon flock?). Verify.
5. [merge] R2: `target_sha` is read after the merge lock is released (minor race).
6. [storage] W2 keeps test output whole: the baseline cache row (`baselines` table) now stores the
   full baseline failure output (it used to keep the last 200 lines). Pruned per task, but bound
   the stored size (e.g. to `context.bulk_max_chars`, still recoverable by re-running the command).
7. [engine] W3: the early `no_test_command` return skips the task's `finally` housekeeping (event
   and baseline pruning); removing the test command mid-task only logs (`gate_skipped_no_tests`)
   instead of failing.
8. [engine] W3: the test-integrity JS scan is superlinear with thousands of `.only`/focus markers in
   one file (about 10 s for 6k) — adversarial only.
9. [engine] W7: the bug-fixer's repro-gate infra retry (`engine/repro_gate.py`) has no cancel
   plumbing, so a cancel during that gate run is only seen after it.
