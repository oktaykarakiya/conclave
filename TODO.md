# Conclave — TODO / Roadmap

Forward-looking roadmap. The autonomous engine, Agent-ception planning, the opencode-native
engine migration, and the **Autonomous Bug-Fixer mode** are shipped (**367 tests**, `ruff` +
`mypy --strict` clean). See `CHANGELOG.md` for detail.

## Current state
- `master` = MVP + Agent-ception planning + full AUDIT/MEDIUM hardening + **opencode-native engine**
  + **Autonomous Bug-Fixer mode**. Default engine is **opencode** (`CONCLAVE_ENGINE=claude` opts out);
  model/provider is configured in opencode itself (DeepSeek by default). Repo context comes from
  `AGENTS.md` (opencode `/init`).
- Security: single-user, intentionally unauthenticated, binds `0.0.0.0` for LAN use (see README).
- `archive/scale-adaptive` tag = the retired scale-adaptive (BMad L0–L4) branch (would have regressed
  the migration; rebuild on master if ever wanted).

## Remaining
### Bug-Fixer follow-ups
- [ ] **Consensus decline/escalate round** — before trusting an auto-fix, run a `DeclineConsensus`
      vote (mandatory reviewers) → route risky candidates to `declined_needs_human`
      (`TODO(bug-fixer-consensus)` in `engine/bug_fixer.py`). The reproduction gate already handles
      its own `declined` route; this adds a pre-fix safety vote.
- [ ] **Bug-Fixer UI tab** — mode toggle + ledger/coverage dashboard + needs-human queue. The backend
      API exists (`/projects/{id}/mode`, `/bug-candidates`, `/needs-human`); the UI does not.

### Engine / backend
- [ ] True token streaming to the Live tab — `on_chunk` is implemented in both providers but the
      orchestrator never passes a callback; wire it through to the bus/Live tab.
- [ ] Operator **steer** — inject guidance into the next dispatch of an in-progress task
      (in-progress **cancel** is done).

### Frontend
- [ ] Config UI: schema-driven forms (`/api/config/schema` exists; currently a raw JSON editor).
- [ ] Nits: `h-screen`→`h-dvh` (mobile chrome); remove the now-unused shared `Section`; modal/drawer
      Escape + focus-trap; surface errors from fire-and-forget Resume/Pause.

### Tests
- [ ] Direct coverage for the WebSocket handlers (`web/ws.py`).

## Phase 3 / later (out of current scope)
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
  (modified/deleted-test detector, fails-on-old-code, spec-as-contract) shipped with it.
- Developer inner-loop now runs the exact green-gate. Post-mortem agent wired on failure;
  config-driven `NotificationSink` (webhook); unconditional events/baselines GC; planning-session +
  merge-lock leak fixes; final-attempt cancel → `cancelled`. All MEDIUM hardening + earlier MVP gaps
  (DELETE task, in-progress cancel, quarantine selective exclusion, `run_git` timeout).

## Run / quality reference
```bash
./conclave                                    # opencode engine by default; serves on 0.0.0.0:8700
ruff check src tests && mypy && pytest -q     # the quality gate (367 tests)
cd frontend && npm install && npm run build   # rebuild the SPA into src/conclave/web/static
```
