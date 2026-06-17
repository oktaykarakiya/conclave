# Conclave Enterprise-Readiness Assessment (2026-06-17)

**Forward-looking map, not a work order.** This document answers "are we enterprise ready?"
and lays out what *would* be required to get there. There is no current plan to change
Conclave's scope — this exists for **future-proofing**, so the direction is understood if and
when that decision is made.

For security specifics, this defers to **[`AUDIT.md`](AUDIT.md)** as the source of truth and
reuses its finding IDs (`SEC-1..4`). It does **not** restate the engineering backlog.

---

## TL;DR — Verdict

**Not enterprise-ready, by design.** Conclave is a deliberately **single-user, unauthenticated,
trusted-LAN personal tool** (`v0.1.0`, Beta). The README (`README.md:81-84`), the daemon
entrypoint (`main.py:38-43`), and `TODO.md:25` all explicitly put multi-user auth/RBAC out of
scope. The gaps below are **conscious scope boundaries, not defects.**

The important nuance: the *engineering foundation* is already enterprise-**grade** (strict typing,
435+ deterministic tests, green CI, atomic DB, crash recovery, DoS/CSWSH hardening). What is
missing is the *product surface* of a multi-user/hosted system (auth, tenancy, encryption,
observability, deploy). You are not starting from a shaky base — you are choosing whether to
climb a maturity ladder.

### Safe today ✅
- A **single operator**, on their **own machine** or a **trusted LAN behind a firewall**.
- Best practice: `CONCLAVE_HOST=127.0.0.1` (loopback-only) when phone/LAN access isn't needed.
- Operating on **your own repositories** with **your own** model credentials.

### NOT safe today ❌ (without the work below)
- **Internet exposure** of the daemon — no auth + plaintext HTTP + host-level code execution.
- **Shared / multi-user** access — every caller has full control; no identity, no isolation.
- **Untrusted networks** — anyone who can reach `:8700` with `curl` controls the daemon.
- **Third-party or regulated data** — no encryption at rest, no audit trail, no tenancy.

> ⚠️ **How to read the severities below.** They are rated **"if the tool were exposed to
> enterprise / multi-user use."** In Conclave's *current* single-user trusted-LAN scope, every
> one of these is **accepted and is not a bug** — the operator running shell on their own box is
> the entire point of the product. Severity becomes real only if the deployment model changes.

---

## Scope today (what Conclave is)

A portable, web-driven autonomous AI coding team: a FastAPI + Uvicorn daemon that serves a React
SPA, orchestrates agents (plan → develop → review → grounded verdict → green-gate → merge) in
isolated git worktrees, and persists everything to a local SQLite database. Model/provider auth
is **delegated to the `opencode` CLI** — Conclave itself ships no model keys.

- **Single-user / unauthenticated** — "it can configure and run shell commands on the host
  (that's the point)" (`README.md:81-82`).
- **Trusted-LAN** — binds `0.0.0.0:8700` by default for phone/LAN access (`main.py:41`,
  `README.md:82-83`).
- **One operator, many projects** — multiple repos can be attached, but there is no notion of
  users, tenants, organizations, or ownership.

---

## Readiness scorecard

| Dimension | Status | Note |
|---|:---:|---|
| Authentication | ❌ | None, by design — no login, sessions, tokens, or API keys (`SEC-4`). |
| Authorization / RBAC | ❌ | No roles or permission checks; every caller has full power. |
| Multi-tenancy / ownership | ❌ | Flat global namespace; no `user_id`/`owner`/tenant in the schema. |
| Secrets & encryption at rest | ❌ | `secrets.value` is plaintext (`migrations/__init__.py:33`); SQLite file unencrypted. |
| TLS / in-transit encryption | ❌ | Plain HTTP only; no TLS config (`main.py:43`). |
| Audit trail | ⚠️ | Rich event log exists, but events carry **no actor identity** — no "who did what". |
| Agent-execution sandboxing | ❌ | Host-level; worktree is isolation, **not** a sandbox (`TODO.md` Podman item). |
| Supply-chain / SCA | ⚠️ | Lockfile for FE; no Python lockfile, no Dependabot, no `pip-audit`/`bandit`. |
| Observability | ⚠️ | `logging` throughout + `/api/health`, but plain-text logs, **no** metrics/tracing. |
| Containerization & deploy | ❌ | No Dockerfile, compose, k8s, IaC, or systemd unit; manual `./conclave` bootstrap. |
| Backup / DR | ❌ | Single SQLite file; no replication, snapshots, or point-in-time recovery. |
| CSRF / CSWSH | ✅ | `_OriginGuardMiddleware` blocks cross-origin mutating + WS requests (`web/app.py:140`). |
| DoS hardening | ✅ | 2 MiB body cap, pagination bounds, bounded log/event buffers (`web/app.py:39`). |
| Testing | ✅ | 435+ deterministic tests (fake provider, zero LLM cost), unit + integration. |
| Code quality | ✅ | `mypy --strict`, `ruff`, TypeScript strict — all green in CI on every PR. |
| Crash safety | ✅ | Atomic SQLite (write-lock + `transaction()`), task recovery, graceful shutdown. |
| Documentation | ✅ | README, AGENTS.md, CONTRIBUTING, and a real `AUDIT.md`; missing ops runbooks. |

---

## Gap register (severity ranked — *conditional on a scope change*)

Security findings (`SEC-*`) are carried over from `AUDIT.md`, where they are marked
**DE-SCOPED — deliberate** for the personal trusted-LAN model. They are reproduced here as
"what must be fixed *if* the control surface is exposed to more than one trusted operator."

### Critical *(if exposed to multi-user / network use)*

- **SEC-4 / ENT-1 — No authentication or authorization on the control surface.**
  Every REST + WebSocket endpoint is open; any reachable client can attach repos, run tasks,
  read all data, and edit secrets/config. *(Effort: L)*
  → Fix: an authn + authz layer (sessions/OIDC + roles). The **CSWSH portion** of `SEC-4`
  (`/ws/stream` had no `Origin` check) is **now resolved** by `_OriginGuardMiddleware`
  (`web/app.py:140`) — but that guard stops *browser* cross-origin abuse, **not** a direct
  `curl` from anyone on the network. It is not a substitute for auth.

- **SEC-1 — Unauthenticated RCE via shell config.** `POST /api/projects` + `PATCH
  /api/projects/{id}/config` let any caller set `setup_command` / `baseline_test_command`, which
  the orchestrator runs as the daemon user (`engine/gitio.py`). *(Effort: M)*
  → Fix: auth on mutating routes; allowlist attachable paths (`CONCLAVE_ALLOWED_ROOTS`); treat
  shell-command config as privileged.

- **SEC-2 — Unauthenticated RCE via `extra_env`.** `engine_profiles.extra_env_json`
  (`migrations/__init__.py:47`) is merged into the agent subprocess env unvalidated; setting
  `LD_PRELOAD`/`PATH`/`BASH_ENV` = code execution. *(Effort: S)*
  → Fix: allowlist keys to `ANTHROPIC_*`/`CLAUDE_CODE_*`; reject `LD_*`, `PATH`, `*PRELOAD`,
  `BASH_ENV`, `NODE_OPTIONS`, `PYTHON*`.

- **ENT-4 — No multi-tenancy or ownership.** No tenant/owner/user columns anywhere; all projects,
  tasks, secrets, and events share one global namespace and one SQLite file. Hard blocker for any
  hosted/SaaS model. *(Effort: L)*
  → Fix: tenant model (row-level scoping or DB-per-tenant) + per-resource ownership + access checks.

### High *(if exposed)*

- **SEC-3 — Stored-secret exfiltration + SSRF via `test_profile`.** `POST /api/profiles/test` can
  pair a stored secret with a caller-supplied `base_url`, shipping live credentials to an arbitrary
  URL and enabling blind SSRF to internal/metadata hosts. *(Effort: S)*
  → Fix: never pair a stored secret with a caller-supplied `base_url`; pin/allowlist `base_url`.

- **ENT-2 — Secrets stored in plaintext at rest.** `secrets.value` is `TEXT NOT NULL`
  (`migrations/__init__.py:33`); anyone with read access to the SQLite file (default umask, no
  encryption) recovers API tokens. *(Effort: S–M)*
  → Fix: encrypt secret values (app-level envelope encryption or SQLCipher); harden file perms.

- **ENT-3 — No TLS / plaintext transport.** Daemon speaks plain HTTP (`main.py:43`); credentials
  and code stream in the clear. Acceptable on loopback, not on a shared/exposed network.
  *(Effort: S behind a reverse proxy; M native)*
  → Fix: TLS termination (reverse proxy or native), HSTS, secure-cookie posture once auth lands.

- **ENT-5 — No audit trail with identity.** The `events` table records task lifecycle richly but
  has no actor field — you cannot answer "who approved/merged this." *(Effort: M, depends on ENT-1)*
  → Fix: attach authenticated user identity to mutating events; consider an append-only audit log.

- **ENT-6 — Agent execution is not sandboxed.** Agents run shell with full host-user permissions;
  the git worktree isolates the *working tree*, not the *process* (no namespaces/containers;
  acknowledged Podman item in `TODO.md`). *(Effort: L)*
  → Fix: containerized/namespaced execution with resource + filesystem + network limits.

### Medium *(if exposed / for scale)*

- **ENT-7 — No containerization, IaC, or deploy automation.** No Dockerfile/compose/k8s/systemd;
  startup is the `./conclave` venv-bootstrap script. *(Effort: M)*
  → Fix: Docker image + compose, a systemd unit, and a documented upgrade path.
- **ENT-8 — No observability.** Plain-text `logging` and `/api/health` only; no structured/JSON
  logs, metrics, or tracing. *(Effort: M)* → Fix: JSON logs, `/metrics`, readiness probe, optional OTel.
- **ENT-9 — No supply-chain scanning.** No Python lockfile, no Dependabot, no `pip-audit`/`bandit`
  in CI. *(Effort: S)* → Fix: pin/lock deps, enable Dependabot + an SCA/SAST step in `ci.yml`.
- **ENT-10 — No backup / DR.** The SQLite file is the only copy. *(Effort: S)*
  → Fix: scheduled backup/restore + WAL checkpointing; document RPO/RTO.
- **ENT-11 — Single-process scale ceiling.** One SQLite writer, one worker per project, no HA or
  horizontal scale. *(Effort: L)* → Fix (SaaS only): Postgres + a job queue + multiple workers.

---

## Foundation already in place (what you would *not* rebuild)

Per `AUDIT.md` ("INFO / verified-good") and verified in this pass:

- **Quality bar:** `mypy --strict`, `ruff` (E/F/I/UP/B/ASYNC/RUF), TypeScript strict — enforced in
  CI (`.github/workflows/ci.yml`) on every push/PR.
- **Test discipline:** 435+ deterministic tests via a fake provider (no live LLM, reproducible).
- **Data integrity:** atomic SQLite (single-writer `asyncio.Lock` + `transaction()`, `CON-1` done),
  crash recovery of in-progress tasks, per-target merge serialization, graceful shutdown.
- **Web hardening:** body-size cap, pagination bounds, and the CSRF/CSWSH origin guard
  (`web/app.py`); no XSS sinks (React escapes); SQL uses only `col = ?` fragments (no injection).
- **Boundaries:** protected-path enforcement keeps agents out of `.env*`, `.git`, lockfiles.

This is why the climb is feature work, not a rewrite.

---

## Roadmap by tier

Each rung is additive. Pick the rung that matches the deployment you actually want.

### Tier 1 — Harden the personal tool *(stays single-user, trusted-network)*
Production-grade robustness without changing the security model. Closes the *operational* gaps.
- Dockerfile + compose; a systemd unit; documented upgrade path **(ENT-7)**
- Structured/JSON logging, `/metrics`, readiness probe **(ENT-8)**
- Backup/restore + WAL checkpointing **(ENT-10)**
- Encrypt the `secrets` table; harden DB file perms **(ENT-2)**
- Dependabot + `pip-audit`/`bandit` in CI; pin/lock Python deps **(ENT-9)**

### Tier 2 — Team-ready *(one trusted organization, shared instance)*
Tier 1 **plus** the multi-user essentials. Resolves `SEC-1..4`.
- OIDC/SSO login + sessions; roles (admin / operator / viewer) **(ENT-1 / SEC-4)**
- Per-project ownership + access checks; lock down shell-config + `extra_env` + `test_profile`
  **(SEC-1, SEC-2, SEC-3)**
- Audit log with authenticated user identity **(ENT-5)**
- TLS termination **(ENT-3)**

### Tier 3 — Full multi-tenant SaaS *(host it / sell it)*
Tier 2 **plus** isolation, scale, and compliance groundwork.
- Tenant isolation (row-level or DB-per-tenant) **(ENT-4)**; SAML/SCIM + fine-grained RBAC
- Sandboxed agent execution — containers/namespaces with resource + network limits **(ENT-6)**
- Postgres + job queue + horizontal workers; SLOs **(ENT-11)**
- Full observability (traces/metrics/logs); SOC2/GDPR controls + data residency

---

## Compliance note (future, brief)

No active regime today (driver: future-proofing). When one arrives, SOC2 / GDPR / ISO 27001 build
directly on **Tier 2/3**: the audit trail (`ENT-5`), encryption at rest + in transit
(`ENT-2`, `ENT-3`), access reviews (`ENT-1`), and data retention/DPA + deletion flows. Treat this
as a pointer, not a program — scope it when a specific framework is actually required.

---

## Sources

Grounded in the working tree at `2026-06-17`:
`docs/AUDIT.md` · `README.md:79-94` · `src/conclave/main.py:38-43` ·
`src/conclave/db/migrations/__init__.py:30-51` · `src/conclave/web/app.py` (origin guard / body cap) ·
`TODO.md:12,25` · `pyproject.toml` · `.github/workflows/ci.yml`.
