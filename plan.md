# PLAN-3: Per-Session Lock for Planning Turns

## Problem
`_agent_turn` can be called concurrently on the same session from two paths:
1. The `_run_discussion` loop (reviewer rounds → planner refinement)
2. `add_human_message` spawning a background planner turn

Both paths call `_apply_task_changes` which does a read-modify-write cycle
(read all nodes → build `seen_titles` set → write adds/updates/deletes).
Without serialization this causes:
- **Dedupe defeat**: both reads see the same pre-existing titles, both try to
  add the same node, and the in-memory `seen_titles` guard can't catch the
  duplicate across concurrent calls.
- **sort_order collision**: both compute `sort_order = len(siblings)` from the
  same stale read, producing duplicate ordering values.

## Approach
Add a per-session `asyncio.Lock` dict on `PlanningOrchestrator`, acquire it
around the full `_agent_turn` body, and clean it up when the session ends.
The lock is keyed by `session_id` so different sessions still run in parallel.
Cancellation is safe because `asyncio.Lock` releases on `__aexit__` if held,
and if the task is cancelled while *waiting* for the lock it never acquired
it so `_run_discussion`'s existing `CancelledError` handler catches it
normally — no deadlock.

Concretely:
1. Add `self._turn_locks: dict[str, asyncio.Lock]` in `__init__`.
2. Add a `_get_session_lock(session_id) -> asyncio.Lock` helper that lazily
   creates the per-session lock (dict `.get` + fallback assignment).
3. In `_agent_turn`, wrap everything after the docstring (the session-get
   through return) in `async with self._get_session_lock(session_id):`.
4. Clean up the lock entry from `_turn_locks` at every session exit point:
   - `_run_discussion` finally block (normal return, CancelledError, Exception)
   - `approve_session` after task materialisation
   - `cancel_session` after cancellation
   - `shutdown` — clear the whole dict

## Files to Touch
- `src/conclave/planning/session.py` — add lock dict, helper, acquire in
  `_agent_turn`, cleanup in exit paths
- `tests/unit/test_planning_orchestrator.py` — add test for concurrent turn
  serialization

## Files to NOT Touch
- `src/conclave/planning/prompts.py` — no prompt changes needed
- `src/conclave/db/repositories.py` — locking is application-level, not DB
- `tests/integration/fake_provider.py` — test uses inline stub provider

## Tests to Add
1. **`test_concurrent_turns_on_same_session_are_serialized`**: Use a gated
   provider stub whose first `run_agent` call blocks on an `asyncio.Event`.
   Start a session (Round 0 planner blocks holding the lock), then call
   `add_human_message` (spawns a bg `_agent_turn` that waits on the lock).
   Assert the provider's `run_agent` has only been entered once. Open the
   gate, await completion, assert `run_agent` was entered exactly twice
   (serially), and assert task_changes were applied without duplicate nodes
   or sort_order collisions.

## Risks
- **CancelledError + asyncio.Lock interaction**: If `_agent_turn` is cancelled
  while waiting for the lock, `__aexit__` may call `release()` on an
  un-acquired lock → RuntimeError masking CancelledError. Mitigation: test
  that `_run_discussion` cancellation still works cleanly with the lock held
  (the existing `test_planning_orchestrator_shutdown_awaits_active_session`
  already covers this path). We can also use `async with` — CPython 3.12+
  handles this correctly.
- **Lock leak if cleanup is missed at an exit path**: If a future code path
  exits without popping the lock, the dict grows without bound.
  Mitigation: centralize cleanup in `_run_discussion`'s `finally` (covers
  the main loop) and in `approve_session`/`cancel_session` (cover the
  operator-driven exits). `shutdown` clears everything.

## Acceptance Criteria
1. Two turns on the SAME session cannot run concurrently → `_agent_turn`
   body is guarded by `async with per-session-lock:`.
2. A human interjection during a loop turn is serialized, not interleaved →
   the `add_human_message`-spawned `_agent_turn` waits for the lock.
3. Test `test_concurrent_turns_on_same_session_are_serialized` passes,
   proving serial execution.
4. Green-gate: `.venv/bin/ruff check src tests && .venv/bin/mypy && .venv/bin/pytest -q` all pass.
