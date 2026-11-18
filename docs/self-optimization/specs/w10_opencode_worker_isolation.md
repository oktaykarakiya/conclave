# W10 Isolate opencode worker dispatches (no MCP servers, no external skills) and pass the profile env

Isolate opencode worker dispatches under `execution.worker_isolation` (no MCP servers, no external skills), and pass the engine profile's environment to opencode.

CONTEXT: `execution.worker_isolation` (default true) and `ResolvedProfile.isolated` already exist and isolate Claude Code dispatches (`providers/profiles.py`, `providers/claude_cli.py`). The opencode provider (`providers/opencode_cli.py`) ignores both today.

PROBLEM (measured on this host, opencode 1.19.5, by capturing the exact LLM request a worker sends): an opencode worker inherits the operator's global opencode config and skills.
- Every MCP server in the resolved config starts and is offered to the model. Here that is Playwright: 25 tools, about 20k characters of tool schemas in every request, and a browser the worker can drive.
- 13 skills synced from `~/.claude/skills` are listed in the system prompt (about 12k characters). Two synced buckets share skill names and opencode picks a random copy per process, so every worker's system prompt differs (8 of 8 runs), which breaks the cross-session prompt cache.
- With MCP and external skills off, consecutive requests are byte-identical up to the per-task environment block, and the system+tools prefix halves.
- Separately: `OpenCodeCliProvider` passes no `env` to `asyncio.create_subprocess_exec` and ignores `profile.extra_env`, so an engine profile's environment never reaches opencode.
Measured facts the implementation can rely on (verify them again against the installed CLI):
- `opencode debug config` (run with the worker's cwd and environment) prints the fully resolved config as JSON, including `mcp` (global, `OPENCODE_CONFIG`/`OPENCODE_CONFIG_DIR`, and the repository's own config). It takes about 0.8 s.
- `OPENCODE_CONFIG_CONTENT` is merged over every other config source: `{"mcp":{"playwright":{"enabled":false}}}` validates and turns the server off while keeping its definition.
- `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1` and `OPENCODE_DISABLE_EXTERNAL_SKILLS=1` each reduce `opencode debug skill` to the single built-in skill on this host.
- There is no switch that disables all MCP servers at once.

REQUIRED BEHAVIOUR
1. When `profile.isolated` is true, an opencode dispatch runs with:
   - `OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1` and `OPENCODE_DISABLE_EXTERNAL_SKILLS=1`;
   - every MCP server of the resolved config disabled through `OPENCODE_CONFIG_CONTENT`, `{"mcp": {"<name>": {"enabled": false}, ...}}`, with the names discovered by running `opencode debug config` (same binary resolution as dispatches, same cwd, same environment minus the isolation overrides). Cache the discovered names per (binary, cwd) for the provider's lifetime, so a task pays the 0.8 s once, and bound the cache.
   - If the daemon's own environment already has `OPENCODE_CONFIG_CONTENT`, deep-merge into it (operator keys kept, the `enabled: false` entries win); if it is not a JSON object, leave it unchanged and log a warning.
   - If discovery fails (non-zero exit, timeout of 30 s, unparsable output), the dispatch still runs with skills disabled, and the provider logs a warning that names the failure. It must never hang or fail the dispatch because of discovery.
2. When `profile.isolated` is false, the opencode argv and the environment-variable overrides are exactly what they are today (the inherited environment).
3. In both modes, the subprocess environment is the daemon's environment plus `profile.extra_env` applied last (so a profile can still override any isolation variable), passed explicitly as `env=`. Document this in the provider docstring.
4. The retry/resume loop (`_MAX_DISPATCH_ATTEMPTS`) uses the same environment on every attempt; discovery runs at most once per dispatch.
5. No change to the Claude Code provider, to the argv flags, or to prompt assembly (`AGENTS.md` handling is a separate task).

TESTS (deterministic: no real opencode, no network)
- Monkeypatch `asyncio.create_subprocess_exec` (or the discovery helper) to capture argv and `env`:
  - isolated → both skill env vars set and `OPENCODE_CONFIG_CONTENT` disables exactly the discovered servers;
  - not isolated → no isolation vars, argv unchanged, and the env equals os.environ plus extra_env;
  - `extra_env` wins over an isolation var;
  - a pre-existing `OPENCODE_CONFIG_CONTENT` object is merged and kept;
  - a non-object pre-existing value is left unchanged with a warning;
  - discovery failure (non-zero exit, garbage output, timeout) still dispatches with the skill vars set and logs a warning;
  - the discovery cache is hit on the second dispatch for the same cwd and missed for a different cwd;
  - transient retry attempts reuse the same env and do not rediscover.
- A test with a fake `opencode` executable script on PATH (or through `CONCLAVE_OPENCODE_BIN`) that answers `debug config` with a fixed JSON and records the env of a `run` invocation, so the whole path is covered end to end without the real CLI. Keep it POSIX-only with a skip marker on Windows.

DOCS (same change)
- `src/conclave/mcp/pilot_guide.md` section 3: what an isolated opencode worker gets (built-in skill only, no MCP servers, the repository's own opencode config still applies), the discovery cost, and the pitfall that a project whose agents need an MCP server must set `worker_isolation: false`. Section 11 gets the pitfall line if it is not already covered by the Claude Code one.
- The §3 `worker_isolation` bullet currently ends "opencode workers are not affected": replace that with the opencode side.
- AGENTS.md's "Workers run isolated by default" bullet: add what an isolated opencode worker gets (skills env vars, MCP servers disabled through `OPENCODE_CONFIG_CONTENT` after discovery) and that `profile.extra_env` reaches both engines.

CONSTRAINTS
- Never weaken or skip an existing test. Never call the live daemon or its MCP tools, and never run the real `opencode` binary in tests.
- Quality gate (must stay green): `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit && npm run build`.
