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
- 37 defects found and fixed by the audits A1 (providers, 9), A2 (engine, 13) and A3 (MCP, web,
  database, 15), each shown first by a test that fails on the old code (`RESULTS.md`, run 3
  part 2).
- Isolation variables also passed as `--settings`, so no repository settings file can turn them
  back off; reviewers told the worktree is shared, and `review.worktree_modified` when one changed
  it; a reset reported more than 8 days ahead ignored; `review_rounds_max` and an edge's
  `max_iterations` enforced; the newest rejected diff sent once; operator SQL on its own
  read-only connection with a deadline and no access to secrets; unknown attach presets refused;
  the stdio bridge never answers a notification (W19).

Open:

1. [engine] Persona-first prompt order: only helps engines with prefix caching; Claude Code sends
   the prompt as one block. Needs an A/B on a funded DeepSeek account.
2. [engine] Staggered parallel reviewers (to spread a usage window): needs a measurement first.
3. [engine] An operator node override posted while the node is not paused is stored without an
   attempt, so it can apply to a later pause of that node (A2, suspected).
4. [engine] Reviewers never get the baseline preamble, although `baseline.py` says the snapshot
   exists for them: a prompt decision (A2).
5. [engine] Swarm double-claim window: an approve landing between the waiting state and the
   waiter's registration could let a parallel tick claim the task (A2, not reproduced).
6. [web] Several REST endpoints answer 200 with an empty or false result for an unknown id where
   MCP answers `isError` (tasks, quarantine, events, project delete, pause/resume) (A3).
7. [web] Whole-graph writers stay last-writer-wins: MCP graph edits against REST
   `PUT /workflow` and the evolver's compare-and-apply (A3).
8. [web] The WebSocket stream has no `after_id` backfill and ignores the subscriber's gap flag; no
   contract promises replay (A3).
9. [web] The cancel fallback for a task waiting for approval writes its state unconditionally, the
   shape A3 fixed for pending tasks (A3, not demonstrated).
