# Conclave Hardening Audit (swarm, 2026-06-14)

Source of truth for the dogfood backlog. Produced by 6 parallel analysis agents over the
working tree (MVP + agent-ception + the harness fixes listed below). Each finding has an ID,
severity, location, problem, and fix. Feed CRITICAL/HIGH into Conclave first.

## Decisions (2026-06-14)
- **Scope**: harden first (AUDIT CRITICAL→HIGH→MEDIUM), THEN the TODO roadmap.
- **Auth: none, by design.** Personal/single-user tool on a trusted home LAN, reached from the
  owner's phone → **all SEC-1..4 are DE-SCOPED** (they reduce to "the operator can run shell on
  their own box", which is the intended capability). Host default changed to **0.0.0.0** (`main.py`).
- **ENG-3 + decline-blocking: DONE** (fixed by Opus in the harness, not dogfooded — it's the gate-
  integrity gate that makes the unattended batch trustworthy; circular to let the holed gate fix
  its own hole). `_review` now fails an attempt if reviewers were derived but none returned a
  usable PASS (all-`unknown`), and `decline` now blocks like `fail`/`block`. Fake provider reordered
  so code-review dispatches aren't shadowed by planning-persona name overlap.
- **Branch model**: project `target_branch` WAS `conclave-selftest` (has 11 scale-adaptive commits
  but NOT the harness fixes). New model → daemon runs **`master`** (working tree committed); Conclave
  targets a fresh **`conclave-work`** branched from master (clean base WITH all harness fixes, no
  scale-adaptive). `conclave-selftest` is parked for the roadmap phase (merge scale-adaptive then).
  Target ≠ checked-out branch on purpose (avoids merge-into-checked-out-branch corruption).
- **Models**: opus-4-8 @ max (profile already set); restart clean (no `/tmp/deepseek-env`, backed up
  to `.bak`) to leave DeepSeek. DeepSeek is the manual fallback if opus hard rate-limits mid-batch.

## Runtime / repo state at audit time
- **Daemon**: `CONCLAVE_HOME=/tmp/conclave-fix`, port 8700, currently running with the host
  **DeepSeek env active** (`ANTHROPIC_BASE_URL=…deepseek`). To run on opus-4-8 max: `rm /tmp/deepseek-env` + restart clean.
- **Working tree (master, UNCOMMITTED)** contains, on top of `de59778`: the agent-ception
  planning module; harness fixes — per-worktree **venv provisioning** (`setup_command`), fixed
  `claim_next_approved` precedence, 900s planner timeout, planner id-exposure + title dedupe,
  `cancel_session` await, reviewer dispatch-retry (→ non-blocking `unknown`), **token usage**
  capture (migration v5) replacing cost in the UI, `block_descendants`, `logger` fix, analyst
  None-guard; bootstrap default now **opus-4-8 @ max (flag)**; ws `/ws/planning` session check;
  loopback host default.
- **Branch `conclave-selftest`** = `de59778` + baseline-task fix + **11 scale-adaptive commits**
  (the completed feature). Does NOT yet contain the working-tree harness fixes.
- **Pending restart** activates: token display, reviewer-retry, opus-4-8 default, ws/loopback.
- ⚠️ Before dogfooding: commit the working tree to the branch Conclave targets, so fixes apply
  to the *current* code (not stale `de59778`). Decide whether to also merge `conclave-selftest`.

---

## GLOBAL UI/UX requirements (user, 2026-06-14) — apply everywhere
1. **Single page only**: collapse the 7-tab nav into ONE scrollable page — each former tab becomes a
   collapsible section (accordion). No tab-switching. (Lazy-load/stream heavy sections like Live &
   Agent-ception only when expanded, for performance.)
2. **Mobile friendly**: mobile-first responsive — layouts reflow/stack on narrow screens, touch-sized
   targets, no horizontal overflow (user reaches it from their phone via 0.0.0.0).
3. **Agent-ception huge text**: long transcript messages / task descriptions must be COLLAPSIBLE
   (truncated with expand toggles), collapsed by default — currently it's a wall of text.
4. **Page order**: Agent-ception must be FIRST/leftmost (and the default landing view); it's last today.
   Applies to both the current tab nav and the single-page section order.

## PENDING — Second UI/UX swarm (user-requested 2026-06-14, gated)
Run ONLY after BOTH complete: (a) the current 7-agent FE polish swarm is integrated+built+committed,
and (b) Conclave's last backend task (PLAN-4) is done + backend wrap-up (ff/gate/restart). Then:
- **Step 1 — identify**: one dedicated agent PER PAGE audits its page for ALL possible UI/UX improvements
  (produces a findings list). 
- **Step 2 — apply**: one dedicated agent PER PAGE applies the improvements that genuinely help UX.
- I (Opus) am the boss: vet Step-1 lists, decide what's worth doing, drive Step 2, integrate, build, verify.

## PENDING — Final verification swarm (user-requested 2026-06-14, LAST step)
After ALL the above is done, a final QA pass: verify every page is nice, simple, intuitive, slick.
Use as many agents/sessions as needed; check EVERY page. Approach: if a headless browser
(chromium + playwright/puppeteer) is available, screenshot each page at desktop + mobile widths and
actually look (Opus can Read images); otherwise code-level design review + build/type checks +
rendered-HTML structure. Fix anything that isn't tidy/cohesive before declaring done.

## Frontend pass (Opus, end-of-run — gate is Python-only, so FE can't be dogfooded)
Do these myself after the autonomous waves: edit tsx, `cd frontend && npm run build`, verify the page loads.
- **Tasks list UX (messy → clean)**: state filter (All / Active / Done / Failed / Blocked), text search,
  server-side pagination via WEB-1's `limit`/`offset`, newest-first sort, collapse/By-default-hide done,
  keep the tree view for parented tasks. (User: "pagination, filtering, etc. currently it is messy".)
- **#1 wrong target in header**: shows `default_branch` (conclave-selftest); show `execution.target_branch`
  (conclave-work = real merge target; merges verified correct, label only).
- **#2 started_at**: Task has only created_at/updated_at → "start" = queue time, durations include queue
  wait (DATA-1 "118m" was mostly waiting). Add `started_at` (set on claim; small backend+migration) and
  show real run-time (or both).
- **#3 usage stale in list**: just-completed task shows 0 turns/agents though data exists (PLAN-2 had 144
  turns). Refetch usage on state change, or batch usage endpoint.
- **#5 verdicts**: panel "No verdicts recorded yet" though verdicts exist — surface per-row / auto-select.
- **#6 in-progress visibility**: running task shows only "(running)"; add live stage/agent badge from events.
- **FE-2** RepoKnowledge `{}` guard + remove dead cost-shaped `api.usage()`; **FE-1** WebSocket reconnect
  with backoff (live log dies silently on daemon restart).

## CRITICAL

> **SEC-1..4 DE-SCOPED (2026-06-14)** — no-auth is a deliberate choice for this personal,
> single-user, trusted-LAN tool. They reduce to "the operator can configure shell commands /
> env / URLs on their own machine", which is the intended capability. Kept below for the record;
> NOT queued into Conclave. The only live CRITICAL is **CON-1**.

**SEC-1 — Unauthenticated RCE via shell config.** `POST /api/projects` accepts any `path` that
merely contains a `.git` dir; `PATCH /api/projects/{id}/config` sets `execution.setup_command` /
`baseline_test_command` to arbitrary strings; the orchestrator runs them via
`asyncio.create_subprocess_shell` (`engine/gitio.py:41`) as the daemon user. → `web/api.py:86,163`.
Fix: require auth on mutating routes; restrict attachable paths to an allowlist
(`CONCLAVE_ALLOWED_ROOTS`); treat shell-command config as privileged.

**SEC-2 — Unauthenticated RCE via `extra_env`.** `ProfileInput.extra_env` is stored unvalidated
and merged into the agent subprocess env (`providers/claude_cli.py:39`, `profiles.py:76`). Setting
`LD_PRELOAD`/`PATH`/`BASH_ENV` = code execution. → `web/api.py:366`, `web/schemas.py:38`. Fix:
allowlist keys to `ANTHROPIC_*` / `CLAUDE_CODE_*`; reject `LD_*`, `PATH`, `*PRELOAD`, `BASH_ENV`, `NODE_OPTIONS`, `PYTHON*`.

**SEC-3 — Stored-secret exfiltration + SSRF via `test_profile`.** `POST /api/profiles/test` with no
`auth_token` resolves the stored secret, then dispatches against an attacker-supplied `base_url`
→ live credential shipped to any URL; blind SSRF to internal/metadata hosts. → `web/api.py:398-418`.
Fix: never pair a stored secret with a caller-supplied `base_url`; pin/allowlist `base_url`.

**SEC-4 — No authentication on the entire control surface.** Every read leaks task/repo data;
every write tampers with secrets/agents/config. Umbrella fix: auth+authz layer. Also `/ws/stream`
has no `Origin` check (CSWSH) — `web/ws.py:17`.

**CON-1 — No DB transaction isolation across coroutines.** One shared `aiosqlite` connection;
`Database.execute` does execute-then-commit as two queued ops; zero locks anywhere. Empirically
reproduced: one coroutine's `commit()` flushes another's half-written INSERT. → `db/database.py:67`,
all repos. Fix: add an `asyncio.Lock` in `Database`; provide `async with db.transaction()` and wrap
every multi-statement write (claim, block_descendants, increment_planning_turn, lifecycle transitions).

---

## HIGH

**ENG-1 — `process_task` has no `try/finally`.** Any mid-task exception after worktree creation
leaks the worktree AND leaves the task `in_progress` forever (`recover()` only runs at worker
start; the worker loop just logs+sleeps). → `engine/orchestrator.py` whole `process_task`,
`runtime.py:56`. Fix: wrap in try/except → `_fail_early`/cleanup + reset state; have the worker
loop reset its in-progress task on crash.

**ENG-2 — Reviewers run with write access; commit/gate use post-review tree, grounding uses
pre-review diff.** A reviewer (`--dangerously-skip-permissions`) can mutate the worktree;
`current_diff` is captured before review and never refreshed, so unreviewed reviewer edits get
gated and committed. → `engine/orchestrator.py` `_review`/`_finish_success`, `profiles.py:23`.
Fix: run reviewers read-only (restrict tools / throwaway checkout), or re-stage+recompute diff
before the gate and never commit unreviewed changes.

**ENG-3 — [DONE — fixed in harness by Opus, 2026-06-14] All-reviewers-unavailable can merge with zero passing reviews.** The new
reviewer-retry path downgrades a persistently-failing reviewer to non-blocking `unknown`; if the
provider is degraded, every reviewer → `unknown`, `_review` returns "no blocker", and if the gate
is also skipped the task auto-merges unverified. → `engine/orchestrator.py:368-419` (a nuance on a
recent fix). Fix: require ≥1 grounded PASS to proceed, or treat all-`unknown` as a hard attempt fail.

**ENG-4 — Retry trigger is the wrong signal.** `_dispatch_reviewer` retries on `not result.ok`,
but the provider sets `ok=True` on any output matching `_SUCCESS_HINT`; a terse valid verdict that
exits non-zero gets re-dispatched 3×, while a hint-matching error passes. Fix: parse first, retry
only when no verdict is extractable (genuine empty/timeout/CLI-missing).

**ENG-5 — `_merge` silently swallows conflicts; races on a fixed worktree path; lost-update on
`update-ref`.** On a real merge conflict the task is already `done` and only `merged=False` in the
summary signals it; two tasks merging the same target collide on a deterministic worktree path;
`update-ref` overwrites with no old-value guard. → `engine/orchestrator.py` `_merge`. Fix:
serialize merges per target; keep task in a `conflict`/`needs_merge` state (not `done`) on failure;
pass expected old value to `update-ref`; unique merge-worktree path per task.

**ENG-6 — venv provisioning is misleading + fragile.** The injected "use `.venv/bin/pytest|mypy|
ruff`" rules are hard-coded and unrelated to the actual `test_command` (wrong on non-pytest repos);
`git clean -fd -e .venv` preservation actually depends on `.gitignore`, not `-e`; setup timeout is
a hard-coded 900s magic number. → `engine/orchestrator.py` setup block. Fix: derive verification
commands from `test_command`/knowledge; gate the wording on an actually-provisioned venv; make
setup timeout configurable; consider a per-task venv outside the worktree.

**ENG-7 — Gate can't distinguish real failure from timeout(124)/missing-cmd(127).** All become
"TEST GATE is not green" fed to the developer, who thrashes on non-existent test failures. →
`engine/gate.py` + orchestrator gate handling. Fix: surface skipped/timed_out distinctly; treat
124/127 as infra (retry gate or fail-early), not developer feedback.

**CON-2 — Shutdown closes the DB while background tasks run.** `Daemon.shutdown()` stops only
workers; `_bg_tasks` (AI backfill) and the entire `PlanningOrchestrator` keep running, then
`db.close()` → "no active connection" tracebacks/races. → `runtime.py:129`, `web/app.py:42`. Fix:
add `PlanningOrchestrator.shutdown()`; cancel+await `_bg_tasks` and planning tasks before close.

**CON-3 — Subprocess stdin/stdout deadlock on large I/O.** `drive()` fully drains stdin before
reading stdout; a chatty child fills its stdout pipe, blocks, stops reading stdin → deadlock until
the timeout fires (looks like spurious timeouts). → `providers/claude_cli.py:53-69`. Fix: write
stdin and read stdout concurrently.

**CON-4 — Orphaned/un-awaited tasks; killed subprocess leaves orphan children.** Planning
discussion tasks aren't cancelled on shutdown; `approve_session` cancels-without-await (races its
own writes); `proc.kill()` doesn't kill the child's descendants (subagents) — no process group. →
`planning/session.py:88,210`, `claude_cli.py:71`, `gitio.py:53`. Fix: track+cancel+await all bg
tasks; `start_new_session=True` + `os.killpg` on timeout.

**DATA-1 — `claim_next_approved` parent-gating incomplete.** Only `failed`/`blocked` parents are
excluded; children of *cancelled* or not-yet-`done` parents are claimed and run out of order
(dependency ordering not actually enforced). *Reproduced.* → `db/repositories.py:191`. Fix: gate on
`parent_task_id IS NULL OR parent IN (SELECT id WHERE state='done')` (decide the real model).

**DATA-2 — `from_row` crashes on bad enum / corrupt JSON.** Unknown `state`/`origin`/`mode`/
`status` → unhandled `ValueError`; corrupt `*_json` → `JSONDecodeError`. One bad row 500s the task
list and can stall the worker's claim loop. *Reproduced.* → `db/models.py:61,95,100`,
`planning_models.py`, `_loads`. Fix: parse enums/JSON defensively with fallbacks.

**DATA-3 — Non-atomic migrations.** Each migration runs via `executescript` (implicit COMMIT per
statement) and `schema_version` is bumped only after; a mid-migration failure commits partial DDL
but doesn't advance the version → next boot replays and dies on duplicate-column. *Reproduced.* →
`db/database.py:58`. Fix: wrap each migration body in an explicit BEGIN…COMMIT.

**DATA-4 — Non-atomic task-lifecycle transitions + recovery gap.** `set_task_state` then
`update_task_fields` then `block_descendants` are separate transactions; a crash between leaves a
`failed` parent with unblocked (claimable) descendants, and `recover_in_progress` doesn't re-block.
→ `engine/orchestrator.py` `_finish_*`, `repositories.py:235`. Fix: one transaction per transition;
re-block failed parents' descendants on recovery.

**PLAN-1 — Non-greedy JSON-block regex truncates nested JSON.** `r"```json\s*(\{.*?\})\s*```"`
stops at the first `}`; nested `task_changes` objects → invalid fragment → silently drops all task
changes and `ready`. Works in tests only because FakeProvider is flat. → `planning/session.py:286,382`.
Fix: greedy capture or `json.JSONDecoder().raw_decode` from the first `{`.

**PLAN-2 — `approve_session` has no status guard.** Callable on `active`/`completed`/`cancelled`;
approving mid-discussion double-creates tasks and races the loop; re-approving `completed`
duplicates every task. → `planning/session.py:131`, `web/api.py:511`. Fix: require `stable`; make
idempotent; await the cancelled loop (like `cancel_session`).

**PLAN-3 — Human interjection runs a concurrent `_agent_turn` on the same session.** Parallel
read-modify-write in `_apply_task_changes` defeats the title-dedupe guard and races sort_order /
transcript order. → `planning/session.py:122`. Fix: serialize a session's turns behind a per-session lock.

**PLAN-4 — Cycle/graph blindness.** `_render_task_tree` recursion, `approve_session` parent-linking
(order-dependent, silently reparents orphans to root), `cascade_approve_task` BFS, `block_descendants`
BFS, and the frontend `buildTaskTree` all assume an acyclic tree; a cyclic `parent_task_id` →
infinite loop / vanished nodes. → `planning/session.py:478,157`, `web/api.py:286`, `repositories.py:210`,
`frontend/src/panels.tsx:251`. Fix: validate/topo-sort the node graph, add visited-sets, cap depth.

**WEB-1 — DoS: unbounded lists, no body cap, no subscriber cap.** `list_tasks/projects/quarantine/
verdicts`, `get_task_usage`, `list_planning_messages` have no limit; no request-body-size middleware;
WS subscriber set is uncapped (each = 1000-elem queue; `emit` fans out to all). → `web/api.py`,
`web/app.py`, `events/bus.py:99`. Fix: pagination + caps; SQL-side SUM for usage; body-size middleware.

**WEB-2 — approve-while-running double-runs a task.** `approve_task` unconditionally sets any state
→ `approved`, including `in_progress` → a second claim runs it twice (duplicate branches/merges). →
`web/api.py:261`. Fix: only `inbox`(/`failed`)→`approved` via conditional UPDATE; 409 otherwise.

**FE-1 — WebSocket hooks never reconnect.** `useStream`/`usePlanningStream` only close on cleanup;
no `onclose` reconnect → Live log + agent-ception stream die silently on any daemon restart/blip. →
`frontend/src/useStream.ts:9`, `panels.tsx:869`. Fix: reconnect with backoff guarded by a
cleanup flag; surface a "reconnecting" state.

**FE-2 — `RepoKnowledge {}` crashes KnowledgePanel.** Endpoint returns `{}` pre-onboarding; panel
reads `knowledge.languages.length` → `undefined.length` throws (the `if (!knowledge)` guard doesn't
catch `{}`). → `web/api.py:129`, `frontend/src/panels.tsx:783`. Fix: return null/404 or treat empty
object as "no knowledge"; optional fields with `?? []`.

---

## MEDIUM (condensed)
- ENG: wall-clock budget isn't a hard cap (checked only at attempt top); `run_shell` timeout kills
  the shell but not its child group; verdict regex/`VERDICT:` substring false-positives; grounding
  uses naive substring containment (shared, duplicated in orchestrator); evidence path-traversal in
  grounding on-disk check; redundant `git add -A`/full-diff recomputation; uncapped diff into prompts.
- CON: multi-statement read-modify-write races (block_descendants, turn/message sequences); event-bus
  drops oldest under backpressure with no client resync signal; idle workers busy-poll every 2s.
- DATA: `block_descendants` no visited-set (cycle relies on side effect) + clobbers `in_progress`;
  inconsistent `"col" in row.keys()` guarding; `state`-only subquery does a full scan;
  `list_tasks` ORDER BY not index-covered; events/usage tables grow unbounded (no GC).
- PLAN: `ready` parsed twice; `update`/`remove` use `change["id"]` (KeyError) and don't scope by
  session_id (cross-session delete risk); `task_changes` not validated as list; max-rounds auto-stable
  with no reason persisted; empty-plan approval creates a phantom parent and reports success;
  `max_rounds` unbounded; round-0 prompt lacks the JSON contract; `_build_context` rebuilt per turn
  (O(rounds²·messages)), messages fetched-then-sliced in Python; `planning_task_proposed` emitted
  with `project_id=None`.
- WEB: `?state=foo` → 500 (use enum query param); planning endpoints raise bare `ValueError`→500
  (need `_require_planning_session`→404); `patch_config` is replace-not-patch (drops unspecified
  overrides); `until` quarantine date unvalidated; `create_project` runs onboarding inline (slow/
  partial-failure); `detach_project` mid-run orphans worktree+task.
- FE: verdicts refetch every 3s (`tasks` in deps); per-task usage fetched once, stale for
  in-progress, N requests + flat-list shows none (add batch endpoint); autoscroll fights manual
  scroll; several panels `setState` after await w/o unmount guard; `ConfigPanel`/`QuarantinePanel`
  load errors unhandled (blank editor can overwrite real config); FastAPI 422 detail rendered as raw
  JSON; 3s poll runs while tab hidden; `EventRow` TS type missing top-level `planning_session_id`;
  `STATE_COLORS` missing `blocked`; dead `api.usage()` cost-shaped method.

## INFO / verified-good
- No XSS sinks in the frontend (React escapes; no dangerouslySetInnerHTML/eval).
- `claim_next_approved` precedence bug is FIXED and atomicity is sound (single UPDATE…RETURNING).
- Dynamic SQL builds only `col = ?` fragments — no injection.
- Log buffers bounded (400/200); WS subscriber cleanup on disconnect is correct.

---

## Dogfood plan (revised 2026-06-14)
0. ✅ Harness prep (Opus): ENG-3 + decline-blocking + fake-provider reorder + host=0.0.0.0; full gate green (79 tests).
1. Commit working tree → `master`. Create `conclave-work` from master. Retarget project to `conclave-work`
   (PATCH the FULL execution config — patch_config is replace-not-patch / WEB-MEDIUM).
2. Back up + remove `/tmp/deepseek-env`; restart daemon clean → opus-4-8 max, host 0.0.0.0, ENG-3 live.
3. Queue **CON-1 first** (DB transaction isolation — the foundation). Let it merge into conclave-work.
   `git merge --ff-only conclave-work` on master + restart daemon so the rest of the batch runs on the
   fixed DB layer. (Bounds the buggy-layer exposure to CON-1's own task.)
4. Bulk-create the remaining HIGH tasks (dependency-ordered, FIFO = exec order on the single worker),
   each small/single-concern with explicit acceptance criteria; green-gate enforces tests. Babysit.
5. Final `ff master ← conclave-work` + restart. Then MEDIUM, then the TODO roadmap (reconcile first:
   scale-adaptive + venv/baseline-task_id self-run items are already done).
