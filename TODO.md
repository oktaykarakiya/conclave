# Conclave — TODO / Roadmap

Forward-looking roadmap. The autonomous engine, Agent-ception planning, the opencode-native
engine migration, and the **Autonomous Bug-Fixer mode** are shipped (**402 tests**, `ruff` +
`mypy --strict` clean). See `CHANGELOG.md` for detail.

## Current state
- `master` = MVP + Agent-ception planning + full AUDIT/MEDIUM hardening + **opencode-native engine**
  + **Autonomous Bug-Fixer mode**. Default engine is **opencode** (`CONCLAVE_ENGINE=claude` opts out);
  model/provider is configured in opencode itself (DeepSeek by default). Repo context comes from
  `AGENTS.md` (opencode `/init`).
- Security: single-user, intentionally unauthenticated, binds `0.0.0.0` for LAN use (see README).
- `archive/scale-adaptive` tag = the retired scale-adaptive (BMad L0–L4) branch (would have regressed
  the migration; rebuild on master if ever wanted).

## Remaining — none in the active roadmap.
The full product roadmap is cleared (see **Done** below). Only optional future enhancements remain.

## Phase 3 / later (optional future enhancements; not unfinished work)
- [ ] Podman container packaging (host process only today).
- [ ] Cost dashboards in the UI (usage recorded; `/usage` summary exists; dashboards not built).
- [ ] GitHub PR integration (`gh pr create` as an alternative to direct merge).
- [ ] Embeddings-based repo index for very large repos.
- (Superseded: a second `Provider` implementation — opencode now owns provider/model selection.)
- (Out of scope by design: multi-user auth / RBAC — Conclave is single-user.)

## Done (this cycle)
- **opencode-native migration**: `OpenCodeCliProvider` (NDJSON usage parsing; raw-chunk stdout read so
  large output can't overflow), `--dir` worktree isolation, `CONCLAVE_ENGINE` selection (opencode
  default), launcher autodiscovery. Backend teardown of the orphaned engine-profiles/secrets/
  repo-knowledge/onboarding layers (−1435 LOC); repo context now flows from `AGENTS.md`.
- **Autonomous Bug-Fixer mode**: candidate ledger + coverage data layer, hunter/repro/test-integrity
  components, the mode-controller (discover→reproduce→fix→transition), worker-loop wiring on
  `project.mode`, activation + ledger API. Live-validated end-to-end. Test-integrity hardening
  (modified/deleted-test detector, fails-on-old-code, spec-as-contract) shipped with it. Plus a
  **decline-consensus vote** (mandatory reviewers veto an unsafe auto-fix → `declined_needs_human`,
  config-gated `require_decline_consensus`, default on) and a **Bug-Fixer UI tab** (mode toggle +
  ledger + needs-human queue).
- Developer inner-loop now runs the exact green-gate. Post-mortem agent wired on failure;
  config-driven `NotificationSink` (webhook); unconditional events/baselines GC; planning-session +
  merge-lock leak fixes; final-attempt cancel → `cancelled`. All MEDIUM hardening + earlier MVP gaps
  (DELETE task, in-progress cancel, quarantine selective exclusion, `run_git` timeout).
- **Live token streaming** (`on_chunk`→bus→Live tab), **operator steer** (inject into the next
  dispatch), Config schema-form quick-settings, FE polish (`h-dvh`, dead-code removal, modal a11y),
  and direct `web/ws.py` test coverage.

## Run / quality reference
```bash
./conclave                                    # opencode engine by default; serves on 0.0.0.0:8700
ruff check src tests && mypy && pytest -q     # the quality gate (402 tests)
cd frontend && npm install && npm run build   # rebuild the SPA into src/conclave/web/static
```
