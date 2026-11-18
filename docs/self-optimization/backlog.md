# Pilot backlog (observed while piloting; candidates for Conclave tasks)

Resolved in run 2 (see `README.md`): attach upsert (W7), cancellation retries (W7), hot-reload
watcher on by default (W5), gate passing without a test command (W3), dead test-integrity guard
(W3), workflow evolver without off switch or history (W6), blanket 6k prompt compaction (W2).
The router's enum-identity bug after hot reload was fixed in 062367a; commit rc in R3.

Resolved in run 3 (see `README.md`, `RESULTS.md`):
- MCP parity gaps: config schema, models and engines, usage, HITL state, quarantine integrity,
  needs-human (W15), plus compact answers for project lists, `conclave_get_task` and workflow
  decisions (W15).
- Enum identity after a hot reload: every `is <Enum>.member` comparison in `src/` replaced by
  `==`, with an AST guard test (W14).
- `conclave_get_task`'s `{"task": ...}` shape and the event paging are stated in the tool
  descriptions (W14).
- The stdio bridge no longer falls back to an in-process daemon on the live database; it answers
  `initialize`, `ping`, `tools/list` and the guide offline and refuses tool calls (W14).
- `GET /mcp` answers 405, so MCP clients stop re-polling it every second (W14).
- R2's `target_sha` read after the merge lock is released; concurrent merges (W13).
- The stored baseline failure output is bounded (W13).
- The early `no_test_command` return skips housekeeping; a test command removed mid-task fails
  the attempt (W13).
- The superlinear JS focus-marker scan (W13).
- The repro gate's infra retry ignores cancel (W13).
- Interaction logs never pruned, the archive rewritten after every dispatch, a JSONL file named
  `.parquet`, a copy in the working directory, a silent 10,000-row cap (W16).
- The evolver re-proposing a rejected change after every task, and learning from configuration
  and infra failures (W18).
- A resumed Claude Code session whose running total restarted is charged in full (W18).
- Operator rejections at a HITL checkpoint emit `attempt.failed` with a reason; the UI shows the
  reason (W18).

Open:

1. [engine] Persona-first prompt order: only helps engines with prefix caching; Claude Code sends
   the prompt as one block. Needs an A/B on a funded DeepSeek account.
2. [engine] Staggered parallel reviewers (to spread a usage window): needs a measurement first.
