# Conclave — TODO / Roadmap

The MVP is complete on `master` (65 tests, ruff + mypy --strict clean). This file lists
everything still to build, in rough priority order.

## Current state snapshot
- `master` = MVP (the autonomous loop, web daemon, UI, engine profiles, repo onboarding, quarantine).
- `conclave-auto` = `master` + **`DELETE /api/tasks/{id}`** (built & merged by the team in the first
  live self-run; **awaiting your review → merge to `master`**).
- Daemon inbox (state at handoff: `CONCLAVE_HOME=/tmp/conclave-live`, port 8765) has one **unapproved**
  task queued: *"Bug Fixer 1/N: candidate ledger + coverage data layer + bug-hunter persona"*.
- Self-improvement loop: review `conclave-auto` → merge to `master` → **restart** the daemon
  (no live self-reload; editable install, so no `pip install` needed).

---

## Phase 2 — Autonomous Bug Fixer mode (headline feature; only its DB/verdict scaffolding exists)
- [ ] **1. Candidate ledger + coverage data layer + bug-hunter persona** — *queued in inbox*
- [ ] 2. Bug-hunter discovery agent + region selection (pick ONE least-recently-examined region; emit at most one *falsifiable* candidate `{file, symbol, claim, severity}`)
- [ ] 3. Reproduction gate — write a test that **currently FAILS** to prove the bug; mark `dismissed_false_positive` if behaviour is actually correct
- [ ] 4. Mode controller loop — continuous select → reproduce → fix → green-gate → commit → merge → next; Start/Pause/Stop; per-session caps; `deferred` on wall-clock budget
- [ ] 5. **Consensus-based decline/escalate** — `decline` verdict + consensus round (refusal needs team agreement) → `declined_needs_human` → "Needs human decision" queue; if consensus NOT reached, proceed with the raised edge cases as extra tests
- [ ] 6. Bug Fixer UI tab — Start/Pause/Stop + caps; ledger/coverage dashboard (status counts, oldest-examined heatmap, false-positive list); Needs-human-decision queue
- [ ] 7. Wire project `mode` (task_queue vs autonomous_bug_fixer) into the worker loop

## Test-integrity hardening (so the green-gate can't be gamed by editing tests)
- [ ] Flag test mutations: when a diff modifies/deletes EXISTING tests (vs adds new), route to mandatory extra reviewer scrutiny + require justification
- [ ] "Fails-on-old-code" check: a bug's reproduction test must fail on the pre-change code (a test that passes on both old and new code proves nothing)
- [ ] Spec-as-contract: reviewer/tester reconcile against stated acceptance criteria, not just "tests pass"

## Scale-adaptive planning (BMad L0–L4; today only L0/L1 = single one-shot planner)
- [ ] Level router (classify task complexity → 0–4)
- [ ] L2 (acceptance criteria + risks), L3 (brief → PRD-lite → arch note → stories), L4 (epic → child tasks re-queued)
- [ ] Planning personas: PM, architect-as-planner, test-architect

## Findings from the first live self-run (small, high value)
- [ ] `baseline.snapshot` event should carry `task_id` (currently project-only → invisible in the task event view)
- [ ] In-worktree agents need venv-aware tool commands — the reviewers couldn't run `mypy` (they invoked bare `mypy`, not on PATH in a fresh worktree). Surface `.venv/bin/...` via repo-knowledge, or put the worktree venv on PATH for agents.
- [ ] Worktree dependency provisioning for real-world repos: add a configurable per-project **setup command** run once per worktree (Python needs a venv; JS needs `node_modules`) instead of the ad-hoc test-command bootstrap used for self-hosting.
- [ ] `DELETE /api/tasks/{id}` emits no bus event and leaves orphaned `events` rows (no FK cascade) — decide intended behaviour (append-only log vs cleanup) and document/implement.

## MVP gaps to close
- [ ] **Post-mortem agent** — `experimental.post_mortem_enabled` config flag exists, but the agent that rewrites a failed task spec is NOT wired into the orchestrator (team-ai had it; not ported)
- [ ] **Notifications** — Telegram/webhook sinks from the design are not built (only the WS/UI stream exists); de-hardcode and add `NotificationSink`s
- [ ] **True token streaming** to the Live tab — `on_chunk` plumbing exists but is unused; wire `--output-format stream-json` instead of the single JSON envelope for live "thinking"
- [ ] **Steering** — pause/resume exist at the worker level; add in-progress task **cancel** (currently returns a note) and operator **steer** (inject an instruction into the next dispatch)
- [ ] **Quarantine selective exclusion** — governance/expiry/integrity are done; the framework-specific test-exclusion (jest `--testPathIgnorePatterns`, pytest `--deselect`) is not wired (gate is full-green only)
- [ ] **Config UI: schema-driven forms** — currently a JSON editor; `/api/config/schema` exists, but the auto-rendered form UI does not

## Phase 3 / later
- [ ] Second provider (OpenAI / Anthropic SDK) behind the existing `Provider` seam
- [ ] Multi-user auth + RBAC (single local admin today)
- [ ] Podman container packaging (host process only today)
- [ ] Cost dashboards in the UI (usage is recorded; `/usage` summary exists; dashboards not built)
- [ ] GitHub PR integration (revive the `gh pr create` flow as an alternative to direct merge)
- [ ] Embeddings-based repo index for very large repos

## Run / quality reference
```bash
./conclave                         # bootstraps venv, serves on :8700
CONCLAVE_HOME=/tmp/conclave-live CONCLAVE_PORT=8765 .venv/bin/python -m conclave.main   # the self-host run
ruff check src tests && mypy && pytest -q     # the quality gate (65 tests on master)
cd frontend && npm install && npm run build   # rebuild the SPA into src/conclave/web/static
```
