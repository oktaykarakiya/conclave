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

## Status

Early MVP under active construction. See `~/.claude/plans/` for the design, and `tests/` for the
behavior contract.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                 # tests
ruff check . && mypy   # lint + types
```

## License

MIT
