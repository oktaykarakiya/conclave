# W22 No operator action writes approved for a live run

No operator action may write `approved` for a task whose run is still live (backlog Open 4, found by W20).

Four operator paths fall back to `repo.approve_task` when `resolve_node` signalled no waiter and the task, read at the start of the handler, was `waiting_for_approval`:
- REST `POST /api/tasks/{id}/approve` (`web/api.py` `approve_task`);
- REST `complete_task_node` (`web/api.py`, `if not signaled and task.state == TaskState.waiting_for_approval`);
- MCP `conclave_approve_task` (`mcp/tools.py`);
- MCP `conclave_complete_node` / `conclave_approve_node` (`mcp/tools.py`).

The fallback is right for a task whose run is gone (the daemon restarted while it waited): `approved` lets a worker tick resume it from its checkpoints. It is wrong while the run is still live in this daemon. `Daemon._run_claimed_task` registers the run's cancel event in `Orchestrator._cancel_events[task_id]` for the run's whole duration. The fallback then writes `approved` for a running task:
- Completing a node other than the one the run waits at signals no waiter. The stored state reads `approved` until `_await_human_approval`'s loop next polls the state (up to 0.25 s), and that loop treats `approved` as its release.
- An approve that lands after a wait's waiters are removed (its `finally`) and before the run writes `in_progress` does the same.

In that window a worker tick with `max_parallel_tasks` > 1 can claim the task and run it a second time beside the live run.

REQUIRED
1. When the task's run is live in this daemon (its cancel event is registered), none of the four fallbacks writes `approved`.
   - Instead the fallback signals every approval waiter the run has at that moment. It stores no resolution for them, so each waiting node takes its own stored resolution (else completes), exactly as the release by polling did before. Each action releases the same waits it released before; the release is now a signal instead of a state write.
   - When the run has no waiter (it is between waits), the fallback writes and signals nothing. A resolution `resolve_node` stored applies when its node's pass takes it, as W20 made it.
   - When the run is not live, the fallback stays as it is (`repo.approve_task`, which only matches an approvable state).
   - A run is live when its cancel event is registered. `Daemon._run_claimed_task` and `engine/bug_fixer.py` register it for the run's whole duration. A run started by calling `process_task` directly without registering one (as many tests do) counts as not live, as today.
   - Put the decision in one orchestrator or daemon method all four call. Their answers and emitted events stay the same.
2. Tests (deterministic, FakeProvider, temporary homes):
   - Run the task the way the daemon does, with its cancel event registered, either through `Daemon._run_claimed_task` or by registering it as that does.
   - A run waits at a HITL node, and a different node is completed through REST and through MCP. Right after each call the stored state is not `approved`, and `repo.claim_next_approved` returns nothing. The run is then released and the task finishes once (each agent dispatched once for the attempt, one `task.done`).
   - The approve or complete-node lands after the waiters are removed and before `in_progress` is written. Patch to make it deterministic: the stored state never reads `approved`, and the run continues.
   - The run is not live: a task stored `waiting_for_approval` in a daemon that is not running it. Approve and complete-node still write `approved`, and it is claimable (keep or extend the existing tests for this).
   - Show the first test fails on the current code before the fix.

CONSTRAINTS
- Never weaken or skip an existing test.
- Keep `src/conclave/mcp/pilot_guide.md` (§7, HITL) and `pilot_brief.md` current if they describe this fallback.
- In `docs/self-optimization/backlog.md`, move Open item 4 to the resolved list.
- Quality gate (must stay green): `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit && npm run build`.
