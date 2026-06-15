# Conclave

A portable, web-driven **autonomous AI coding team** you can attach to any git repository.

Conclave drives the `claude` CLI through a proven loop —
**plan → develop → multi-agent review → grounded verdict → green-gate → commit & merge** —
fully autonomously, controlled entirely from a web UI (you never edit files in the repo, not
even config).

## Highlights

- **Attach to any repo** — runs as a host daemon and operates via isolated git *worktrees*, so your
  working tree is never touched and the repo is never polluted.
- **Agent-ception planning** — a dedicated page where AI agents discuss and decompose a big goal into
  a reviewed, collapsible task tree; you can interject, and on approval the tasks land in the queue.
- **Web UI is the only control surface** — projects, tasks, live logs, profiles, quarantine, and repo
  knowledge, all in the browser. Config lives in SQLite, not YAML files. Single-page-per-tab,
  responsive (works from your phone).
- **Engine Profiles** — keep Claude Code as the engine but point agents at the system default,
  Anthropic-direct, or **DeepSeek** (any Anthropic-compatible endpoint) via a free-text model name,
  effort picker, and a one-click **Test** button per profile.
- **Repo intelligence** — on attach, the team analyzes the repo (languages, frameworks, test/build
  commands, conventions) and keeps that knowledge current.
- **Grounded verification + green-gate** — reviewer/security/tester verdicts must cite evidence that
  exists in the actual diff (hallucinated findings are downgraded), and `ruff + mypy + pytest` must be
  green before anything merges. Per-worktree venv provisioning so reviewers and the gate share the
  toolchain.
- **Hardened engine** — atomic SQLite transactions, dependency-ordered task claims, crash-safe task
  lifecycle, reviewer-retry with a "no merge without a real review" guard, and bounded resource use.

## Prerequisites

- **`claude` CLI** installed and authenticated (on your `PATH`, logged in) — Conclave drives it as the
  engine. Alternatively/additionally, a DeepSeek or Anthropic-compatible API key for an Engine Profile.
- **Python ≥ 3.12** and **git**. (Node ≥ 18 is only needed for frontend development.)

## Quick start

```bash
./conclave                      # bootstraps a venv on first run, then starts the daemon
# open http://127.0.0.1:8700  (or http://<your-LAN-ip>:8700 from another device)
```

Then, entirely in the web UI:

1. **Attach project** — give it a name and the absolute path to a git repo. Conclave analyzes the
   repo automatically.
2. **Engine profiles** — keep the system default (your logged-in Claude / opus-4-8), or add a
   **DeepSeek** profile (`arg_mode = env`, base URL `https://api.deepseek.com/anthropic`, model
   `deepseek-v4-pro[1m]` — or whichever model your DeepSeek account exposes — and your API key), then
   hit **Test** to verify it end-to-end.
3. **Plan or create a task** — use *Agent-ception* to decompose a big goal, or create a task directly;
   approve it (or auto-approve) and watch the team on the **Live** tab: develop → grounded review →
   green-gate → commit → merge into your target branch.

Conclave never touches your working tree: each task runs in an isolated git *worktree* under
`~/.local/share/conclave/` (override with `CONCLAVE_HOME`). Host/port via `CONCLAVE_HOST` /
`CONCLAVE_PORT`.

## Security model

Conclave is a **single-user, personal tool** and is **intentionally unauthenticated** — it can
configure and run shell commands on the host (that's the point). It defaults to binding `0.0.0.0`
so you can reach the UI from your phone on your home network. **Run it only on a trusted network.**
Set `CONCLAVE_HOST=127.0.0.1` to restrict it to loopback. Multi-user auth/RBAC is out of scope.

## Status

The autonomous task-queue engine, Agent-ception planning, web UI, engine profiles, repo onboarding,
and quarantine are complete and hardened (**222 tests**, `ruff` + `mypy --strict` clean). On the
roadmap (see `TODO.md`): the continuous **Autonomous Bug-Fixer** mode, BMad-style scale-adaptive
planning, post-mortem agent, notifications, and true token streaming.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest -q                    # 222 tests (deterministic; a fake provider — no LLM cost)
ruff check src tests && mypy # lint + strict types

cd frontend && npm install && npm run build   # rebuild the SPA into the daemon's static dir
npx tsc --noEmit                              # strict TS
```

## License

[MIT](./LICENSE) © Oktay Karakiya
