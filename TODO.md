# Conclave — TODO / Roadmap

Forward-looking roadmap. The autonomous engine, Agent-ception planning, web UI, and the swarm
hardening pass are complete (**146 tests**, `ruff` + `mypy --strict` clean). See `CHANGELOG.md` for
what shipped and `docs/AUDIT.md` for the hardening backlog detail.

## Current state
- `master` = MVP + Agent-ception planning + the full AUDIT hardening (CRITICAL + HIGH) + the
  single-page-per-tab responsive UI. Self-hosting target branch: `conclave-work`.
- `conclave-selftest` = a **built-but-unmerged** scale-adaptive planning (BMad L0–L4) feature —
  integrate or retire it (see below).
- Engine profiles: `system-default` = opus-4-8 @ max; a ready `deepseek` (env) profile to switch.
- Security: single-user, intentionally unauthenticated, binds `0.0.0.0` for LAN use (see README).

## In progress — MEDIUM hardening (dogfooded)
Smaller robustness/perf items from `docs/AUDIT.md` MEDIUM, being cleared as gate-verified tasks:
- [ ] WEB input validation & error mapping (no 500s on bad input; planning 404s; date validation)
- [ ] DATA retention + index coverage (events/usage GC; hot-query indexes)
- [ ] PLAN task_changes safety (no KeyError; session-scoped update/remove; validated list)
- [ ] ENG safety (hard wall-clock cap; grounding path-traversal guard; capped prompt diffs)
- [ ] run_shell child-group kill; event-bus backpressure resync; idle-worker poll backoff
- [ ] `detach_project` cleanup (no orphaned worktree/worker); robust async onboarding on create

## Phase 2 — Autonomous Bug-Fixer mode (headline feature; only DB/verdict scaffolding exists)
- [ ] Candidate ledger + coverage data layer + bug-hunter persona
- [ ] Bug-hunter discovery agent + region selection (one falsifiable `{file,symbol,claim,severity}`)
- [ ] Reproduction gate — a test that currently FAILS to prove the bug; `dismissed_false_positive`
      when behaviour is actually correct
- [ ] Mode controller loop — select → reproduce → fix → green-gate → commit → merge → next;
      Start/Pause/Stop; per-session caps; `deferred` on wall-clock budget
- [ ] Consensus decline/escalate — `decline` + consensus round → `declined_needs_human` queue
- [ ] Bug-Fixer UI tab — controls + ledger/coverage dashboard + needs-human queue
- [ ] Wire project `mode` (task_queue vs autonomous_bug_fixer) into the worker loop

## Test-integrity hardening (so the green-gate can't be gamed by editing tests)
- [ ] Flag test mutations (modified/deleted existing tests) → mandatory extra reviewer scrutiny
- [ ] "Fails-on-old-code" check for reproduction tests
- [ ] Spec-as-contract: reviewer/tester reconcile against stated acceptance criteria

## Scale-adaptive planning (BMad L0–L4)
Built on `conclave-selftest` (Level router, L2–L4, planning personas) — **decide: merge into `master`
or retire**. If merging, reconcile with the Agent-ception planning module.

## MVP gaps to close
- [ ] Post-mortem agent (`experimental.post_mortem_enabled` exists; not wired into the orchestrator)
- [ ] Notifications — Telegram/webhook `NotificationSink`s (only the WS/UI stream exists today)
- [ ] True token streaming to the Live tab (`--output-format stream-json`; `on_chunk` plumbing unused)
- [ ] Steering — in-progress task **cancel** + operator **steer** (inject into the next dispatch)
- [ ] Quarantine selective exclusion (jest `--testPathIgnorePatterns` / pytest `--deselect`)
- [ ] Config UI: schema-driven forms (`/api/config/schema` exists; currently a JSON editor)
- [ ] `DELETE /api/tasks/{id}` — emit a bus event + decide cascade/cleanup for orphaned `events`

## Phase 3 / later
- [ ] Second provider (OpenAI / Anthropic SDK) behind the `Provider` seam
- [ ] Podman container packaging (host process only today)
- [ ] Cost dashboards in the UI (usage recorded; `/usage` summary exists; dashboards not built)
- [ ] GitHub PR integration (revive `gh pr create` as an alternative to direct merge)
- [ ] Embeddings-based repo index for very large repos
- [ ] (Out of scope by design: multi-user auth / RBAC — Conclave is single-user.)

## Done (recent)
- AUDIT hardening: CON-1 + ENG-1–7 + DATA-1–4 + CON-2–4 + WEB-1–2 + PLAN-1–4 (+ SEC de-scoped).
- First-self-run findings: per-worktree venv + `setup_command`; `baseline.snapshot` carries `task_id`.
- UI: dark/zinc+indigo redesign, dedicated viewport-fit pages, mobile, collapsible text.

## Run / quality reference
```bash
./conclave                                    # bootstraps venv, serves on 0.0.0.0:8700
ruff check src tests && mypy && pytest -q     # the quality gate (146 tests)
cd frontend && npm install && npm run build   # rebuild the SPA into src/conclave/web/static
```
