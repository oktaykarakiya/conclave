# Changelog

All notable changes to Conclave are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

### Added
- **Agent-ception** — multi-agent planning page: agents discuss and decompose a big goal into a
  reviewed, collapsible task tree with human interjection; approved tasks land in the queue.
- **Per-worktree venv provisioning** via a configurable `setup_command`, so reviewers and the
  green-gate share the project toolchain.
- **Token-usage capture** (input / output / cache-read / cache-write, turns, agents) surfaced per
  task in the UI (replacing the cost readout).
- **Dependency-ordered tasks** — parent/child task trees; a failed parent blocks its descendants;
  claims only run a child once its parent is `done`.
- A ready-to-use **DeepSeek engine profile** alongside the opus-4-8-max default.

### Changed
- **UI redesign** — dark/zinc + indigo design system, dedicated page per menu tab, each page fits the
  viewport (internal panel scroll, no page exceeds the screen), mobile-responsive (drawer + reflow),
  collapsible long text everywhere. Agent-ception is the default landing page.
- Default host is `0.0.0.0` (personal/LAN use; see the Security model in the README).

### Hardened (engine + data + web)
- **Atomic SQLite layer** — a connection lock + `Database.transaction()`; multi-statement writes
  (lifecycle transitions, `block_descendants`, planning turns) are atomic. Migrations are atomic.
- **Crash-safe lifecycle** — `process_task` never strands a task `in_progress` or leaks a worktree;
  recovery re-blocks failed parents' descendants.
- **Gate integrity** — never merge without ≥1 grounded passing review; `decline` blocks like
  `fail`/`block`; reviewer dispatch retries on a genuinely missing verdict; the gate distinguishes
  infra failures (timeout 124 / missing-command 127) from real test failures.
- **Merge safety** — conflicts no longer silently report success; per-task merge worktrees; guarded
  `update-ref`.
- **Robust subprocess + shutdown** — concurrent stdin/stdout (no deadlock), process-group kill on
  timeout, background tasks awaited before the DB closes.
- **Defensive parsing** — corrupt enum/JSON rows no longer 500 the API or stall the worker; cycle-safe
  task-graph traversals; greedy planner-JSON extraction (nested objects no longer truncated).
- **Web hardening** — pagination/caps on list endpoints, request-body-size limit, input validation
  (no 500s on bad input), `approve_task` can't double-run a running task.

### Security
- Multi-user auth/RBAC is intentionally out of scope: Conclave is a single-user, trusted-network
  tool (see the README's Security model).

## [0.1.0] — MVP
- Autonomous worktree-based loop (plan → develop → multi-agent review → grounded verdict →
  green-gate → commit/merge), FastAPI daemon + WebSocket stream, React SPA, engine profiles
  (Claude / DeepSeek / system-default) with a Test button, repo onboarding, expiry-enforced
  quarantine, typed SQLite-backed config.
