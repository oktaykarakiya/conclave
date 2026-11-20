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

Resolved in the backlog follow-up after run 3 (the former open items 3-7 and 9, by their old
numbers; each shown first by a test that fails on the old code):
- 3: an operator node completion applies only to the node's pass in the attempt it was posted
  for (the REST body's `attempt`, else the node's latest checkpoint attempt); one whose pass has
  run is dropped by the next attempt's pass with a `log` event (`operator_resolution_dropped`).
  Before, an operator pass meant for attempt 1's rejected review also passed attempt 2's.
- 4: every reviewer prompt (parallel, sequential, any reviewer node, re-runs) carries the baseline
  preamble when the baseline has failures, fitted per reviewer like the developer's.
- 5: the approval waiter is registered before the task reads `waiting_for_approval`, so an
  approve while the wait is announced signals the run instead of storing `approved`, which a
  `max_parallel_tasks` > 1 tick claimed and ran a second time.
- 6: REST answers 404 for an unknown project or task id on the tasks, quarantine, events and
  verdicts lists, project delete and pause/resume, and emits nothing (the SPA's Detach treats a
  404 as already detached).
- 7: every writer of a workflow graph (REST `PUT /workflow`, config and global writes, the MCP
  graph tools, the evolver's compare-and-apply) runs under one `graph_edit_lock` per database,
  so a race ends in one of the serial outcomes.
- 9: the cancel fallback for a task waiting for approval is a conditional write; an approval that
  lands first keeps the task approved (REST `409` naming the state, MCP `cancelled: false` with
  `state`).

Open:

1. [engine] Persona-first prompt order: only helps engines with prefix caching; Claude Code sends
   the prompt as one block. Needs an A/B on a funded DeepSeek account.
2. [engine] Staggered parallel reviewers (to spread a usage window): needs a measurement first.
3. [web] The WebSocket stream has no `after_id` backfill and ignores the subscriber's gap flag; no
   contract promises replay (A3).
4. [engine] Another double-claim window, found while fixing the former 5: completing a node other
   than the one a running task waits at (REST or MCP complete-node) signals no waiter and falls
   back to `approve_task`, so the task reads `approved` until the wait polls (up to 0.25 s) and a
   `max_parallel_tasks` > 1 tick can claim it (shown by a throwaway probe, not fixed: it changes
   complete-node semantics). A likely fix: when a run is waiting, release it by signalling its
   waiters (as an approve does) instead of writing `approved`.
