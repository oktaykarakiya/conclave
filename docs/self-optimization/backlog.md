# Pilot backlog (observed while piloting; candidates for Conclave tasks)

1. [MCP] conclave_attach_project upserts by path: renames + merges config of the existing project (→ spec r4).
2. [MCP] parity gaps: global config, config schema, models/engines, usage, HITL state, quarantine integrity, needs-human (→ spec r4).
3. [engine] Cancelling a task mid-review: reviewer was retried 3x with error "cancelled", and a new `attempt.started` fired after the cancel. Retries/attempts should stop immediately on cancel.
4. [hot-reload] Any edit in the daemon's checkout hot-reloads all modules. Long-lived objects keep old-class enum members → identity checks break (fixed for the router in 062367a). Audit other `is <Enum>.member` checks on long-lived state; consider disabling the file watcher by default / running the daemon from a separate checkout.
5. [MCP] conclave_get_task wraps as {"task": ...}; conclave_get_task_events pages results (needs after_id loop) — document both in tool descriptions.
6. [engine] Gate passes with no test command (3 of 4 old projects had none) — consider requiring an explicit choice.
7. [engine] Test-integrity guard is dead code (A3), commit rc ignored (r3), workflow evolver has no off switch / history (A1).
8. [prompt] Blanket 6k-char prompt compaction (now disabled per-project via compact_before_start=false) — B1.
9. [mcp stdio] `conclave-mcp` falls back to an in-process daemon on the same DB when HTTP is unreachable — possible second worker on the same data dir (bypasses the daemon flock?). Verify.
