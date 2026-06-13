# Conclave

A portable, web-driven **autonomous AI coding team** you can attach to any git repository.

Conclave drives the `claude` CLI through a proven loop —
**plan → develop → multi-agent review → grounded verdict → green-gate → commit & merge** —
fully autonomously, and is controlled entirely from a web UI (you never edit files in the repo,
not even config).

## Highlights

- **Attach to any repo** — runs as a host daemon, operates via isolated git *worktrees* so your
  working tree is never touched and the repo is never polluted.
- **Web UI is the only control surface** — config, tasks, live logs, and agent steering all live
  in the browser. Config is stored in SQLite, not YAML files.
- **Engine Profiles** — keep Claude Code as the engine, but point any agent at the system default,
  Anthropic-direct, or **DeepSeek** (or any Anthropic-compatible endpoint). Free-text model name,
  effort picker, and a one-click **Test** button per profile.
- **Repo intelligence** — on attach, the team analyzes the repo (languages, frameworks, test/build
  commands, conventions) and keeps that knowledge current.
- **Grounded verification** — reviewer/security/tester verdicts must cite evidence that exists in
  the actual diff; hallucinated findings are downgraded. The test suite is the default gate.
- **Two operating modes** — an operator-fed *task queue*, and a continuous **Autonomous Bug Fixer**
  that hunts one bug at a time, proves it, fixes it (or declines by team consensus), and merges.

## Quick start

```bash
./conclave                      # bootstraps a venv on first run, then starts the daemon
# open http://127.0.0.1:8700
```

Then, entirely in the web UI:

1. **Attach project** — give it a name and the absolute path to a git repo. Conclave analyzes
   the repo (languages, test/build commands, conventions) automatically.
2. **Engine profiles** — keep the system default (your logged-in Claude), or add a **DeepSeek**
   profile: `arg_mode = env`, base URL `https://api.deepseek.com/anthropic`, model
   `deepseek-v4-pro`, your API key, then hit **Test** to verify it end-to-end.
3. **Create a task**, approve it (or auto-approve), and watch the team work it on the **Live** tab
   — develop → grounded review → green-gate → commit → merge into your target branch.

Conclave never touches your working tree: each task runs in an isolated git *worktree* under
`~/.local/share/conclave/` (override with `CONCLAVE_HOME`). Port via `CONCLAVE_PORT`.

## Status

MVP complete: typed config, SQLite persistence, the autonomous worktree-based loop (grounded
verdicts + green-gate), engine profiles (Claude / DeepSeek / system-default) with a Test button,
repo onboarding, expiry-enforced quarantine, the FastAPI daemon + live WebSocket stream, and the
React UI. Next (Phase 2): the continuous **Autonomous Bug Fixer** mode and the full BMad-style
scale-adaptive planning ladder.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                       # 65 tests (deterministic; a fake provider — no LLM cost)
ruff check . && mypy         # lint + strict types

cd frontend && npm install && npm run build   # rebuild the SPA into the daemon's static dir
npm run typecheck                             # strict TS
```

## License

MIT
