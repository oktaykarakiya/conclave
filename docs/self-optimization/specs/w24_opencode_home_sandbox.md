# W24 Sandboxed opencode workers: their own opencode folders, only their provider's credential

An isolated opencode worker runs in a bubblewrap sandbox with its own opencode folders. It uses none of the operator's opencode sessions, config or credentials beyond the one its model needs, and the commands it runs see exactly what they see today.

CONTEXT: the owner's direction (2026-11-20) is that every agent runs in an isolated Claude Code or opencode session. A session must never attach to the owner's own sessions and gets only the information its task needs. W10 already disables MCP servers and `.claude`/`.agents` skills for isolated opencode workers (`providers/opencode_cli.py`, `execution.worker_isolation`). Claude Code workers are not changed by this task; a later task sandboxes them the same way.

PROBLEM (measured on this host, opencode 1.19.5):
- **Sessions.** opencode files a session under the project of the repo's root commit and keeps it in `<data>/opencode/opencode.db`. Workers therefore store their sessions in the owner's database, under the owner's own project. In one repo's session list, 1,357 of 1,755 sessions are Conclave workers.
- **Config.** Workers load the owner's global config (`<config>/opencode/opencode.jsonc`):
  - six providers;
  - the owner's default model (a local llama.cpp model);
  - the owner's permission rules;
  - the owner's MCP servers (W10 disables them, but each is discovered and named);
  - plus any global agents, commands, plugins or `<config>/opencode/AGENTS.md` the owner adds later.
- **Credentials.** Workers can use every credential in the owner's `<data>/opencode/auth.json` (six providers), though each needs one.
- **Discovery writes into the owner's folders.** W10's MCP discovery (`opencode debug config`) runs with the owner's folders. Each run writes `<data>/opencode/log/opencode.log`, opens `<data>/opencode/opencode.db` and takes locks in `<state>/opencode/locks/`.

FACTS (opencode 1.19.5, read from the installed binary and checked by running it; re-check what you rely on with `opencode debug paths` / `opencode debug config`, never with `opencode run`):
- **Folders.** opencode's folders come from xdg-basedir: `$XDG_CONFIG_HOME`, `$XDG_DATA_HOME`, `$XDG_STATE_HOME` and `$XDG_CACHE_HOME`, else `~/.config`, `~/.local/share`, `~/.local/state` and `~/.cache`, each followed by `/opencode`.
  - Config: `opencode.json(c)` and the `agent`, `command` and `plugin` folders.
  - Data: `auth.json`, `opencode.db` (sessions), `log/`, `snapshot/`.
  - State: locks, recent models, prompt history.
  - Cache: downloaded tools and `models.json`. It holds nothing of the owner's.
- **Credentials.** `Auth.all()` reads `OPENCODE_AUTH_CONTENT` when it is set and is valid JSON, else `<data>/opencode/auth.json`. Entry types are `api` (`key`), `wellknown` (`key`, `token`) and `oauth` (`refresh`, `access`, `expires`). opencode refreshes an `oauth` entry and writes it back.
- **Other variables.** `OPENCODE_DB` relocates the session database. `OPENCODE_CONFIG` (a file) and `OPENCODE_CONFIG_DIR` (a folder) add config sources.
- **Config folders.** opencode always reads these as config folders:
  - `<config>/opencode`;
  - `~/.opencode` (the install folder here: binary and plugin dependencies, no config);
  - the project's `.opencode` folders (skipped under `OPENCODE_DISABLE_PROJECT_CONFIG`);
  - `OPENCODE_CONFIG_DIR`.
- **Global instruction files.** `<config>/opencode/AGENTS.md` and, unless `OPENCODE_DISABLE_CLAUDE_CODE_PROMPT` is set, `~/.claude/CLAUDE.md`.
- **The worker's commands.** The bash tool runs every command with the opencode process's environment (`extendEnv`, plus `TERM=dumb`). Changing `XDG_*` or `HOME` for opencode would therefore move the paths of git, uv, pip, pnpm, Playwright and every other tool the worker runs. That is why this task uses a mount sandbox, not environment variables.
- **bubblewrap.** bubblewrap 0.12.0 is at `/usr/bin/bwrap`. The daemon's systemd user unit sets none of `NoNewPrivileges`, `PrivateUsers` or `RestrictNamespaces`.
  - `bwrap --dev-bind / / --bind <dir> ~/.config/opencode --die-with-parent -- <cmd>` shows `<dir>` at that path to `<cmd>` and its children only; the host is unchanged.
  - `opencode debug config` run with the owner's config folder mounted read-only (`--ro-bind`) and its data and state folders bound to other folders resolves the owner's full config. It writes only into the bound folders.

REQUIRED
1. **New setting `execution.worker_sandbox`.** Its values are `auto` (the built-in default), `required` and `off`. Its type is `WorkerSandbox`, a `StrEnum` in `config/models.py` exported from `conclave.config`, layered like every other setting.
   - It applies only to isolated dispatches. Carry it on `ResolvedProfile` as `sandbox: WorkerSandbox` (default `WorkerSandbox.auto`), set from the project's effective config wherever `isolated` is set (`engine/runner.py`, `planning/session.py`, and any other dispatch path).
   - **Availability.** The sandbox is available when the bubblewrap binary works: `CONCLAVE_BWRAP_BIN` if set (read at call time), else `bwrap` on `PATH`. A probe `<bwrap> --dev-bind / / -- true` must exit 0 within 10 s.
     - Put the probe in a new module `conclave/providers/sandbox.py`, as `sandbox_status()`, cached per binary path.
     - It returns whether the sandbox is available and, if not, why (no binary, or the probe's exit code and stderr).
   - **`auto`.** A dispatch uses the sandbox when it is available. Otherwise it runs exactly as it does today (W10), and the provider logs one warning per daemon and binary.
     - When isolation is on, `auto` is set and the sandbox is unavailable, each task and each planning session records one `log` event at its start. The event names the cause and the fix: install bubblewrap and allow unprivileged user namespaces, or set `worker_sandbox` to `off` to silence it.
   - **`required`.** An unavailable sandbox fails the dispatch before anything starts. The error names the cause and the two fixes.
   - **`off`.** Today's W10 behaviour.
2. **What a sandboxed opencode dispatch runs.** The same opencode argv inside `bwrap --dev-bind / / <binds> --die-with-parent -- ...`, with the same environment, except where the bullets below say otherwise.
   - **Paths.** Every path the worker or its commands use stays where it is: worktree, repository, `/tmp`, toolchains, caches, and the owner's other config, data and state entries.
   - **Environment.** No `XDG_*` or `HOME` change.
   - **Process lifetime.** Cancel and timeout still end every process of the dispatch (bwrap, opencode and the commands it started) as they do today. Exit codes and output parsing are unchanged.
3. **Conclave's opencode folders, one per provider.** These three folders are bind-mounted over the owner's opencode config, data and state folders, whose paths are resolved from the daemon's environment as opencode resolves them:
   - `$CONCLAVE_HOME/opencode-homes/<provider>/config`
   - `$CONCLAVE_HOME/opencode-homes/<provider>/data`
   - `$CONCLAVE_HOME/opencode-homes/<provider>/state`

   The cache folder is not bound. `<provider>` is the dispatch model's part before its first `/`, made safe for a path.
   - **Sharing.** Dispatches of different providers never share these folders. Dispatches of one provider share them, so sessions persist for transient retries and quota resumes (`--session`), which stay on the same provider.
   - **The config folder** holds `opencode.json` containing only `{"provider": {"<provider>": <the owner's block for it>}}`, and only when the owner's resolved config has such a block. Nothing else of the owner's config is copied: no `model`, `permission`, `mcp`, agents, commands, plugins, instructions or other providers.
   - **The data folder's `auth.json`** holds only the owner's credential for that provider, when it has one of type `api` or `wellknown`.
     - "The owner's credentials" means what the owner's opencode would use: the daemon's `OPENCODE_AUTH_CONTENT` when it is set and valid JSON, else the owner's `<data>/opencode/auth.json`. Read them at each dispatch, so a rotated key reaches the next dispatch.
     - A provider with no stored credential gets no entry; keys in the environment pass through as today.
     - An `oauth` credential is never copied, because opencode would refresh it and could invalidate the owner's login. The dispatch fails before anything starts, with an error that names the provider and the fixes: an API key, or `worker_sandbox: off` for the project.
   - **File writes.** Write both files atomically with mode 0600, and refresh them at each dispatch. Concurrent dispatches of one provider write the same content.
   - **Without a model.** A dispatch with no `provider/model` uses the owner's configured default `model`, passed as `--model`. If neither exists, it fails with an error that asks for a model to be set.
4. **Removed from the environment.** A sandboxed dispatch drops `OPENCODE_CONFIG`, `OPENCODE_CONFIG_DIR`, `OPENCODE_AUTH_CONTENT` and `OPENCODE_DB` from the daemon's environment, because they point back at the owner's setup. `profile.extra_env` is still applied last and may set any variable.
   - Keep W10's skill variables and its merge of `OPENCODE_CONFIG_CONTENT`.
   - The owner's `~/.claude/CLAUDE.md` must not exist for a sandboxed opencode process, which would load it as a global instruction. The project's own `CLAUDE.md`, `AGENTS.md` and `CONTEXT.md` handling stays as it is today, and `rules_for_dispatch` keeps matching what opencode really loads.
5. **Discovery inside the sandbox.**
   - **MCP names.** Discover them inside the same sandbox, with the same folders and environment as the dispatch (minus the disables). That way exactly the servers the worker would start are disabled: the repository's own and the daemon's `OPENCODE_CONFIG_CONTENT`'s. Cache per (binary, cwd, provider).
   - **The owner's provider blocks and default model.** Read them with `opencode debug config` run inside a sandbox where:
     - the owner's opencode config folder is mounted read-only;
     - its data and state folders are bound to `$CONCLAVE_HOME/opencode-homes/.discovery/{data,state}`;
     - the working directory is outside any repository.

     Cache the result per binary for the provider's lifetime; document that a change to the owner's opencode config reaches workers after a daemon restart. A failure is logged and not cached, and the dispatch runs without a provider block.
6. **The owner's folders stay untouched.** Nothing a sandboxed dispatch or its discovery runs creates or changes a file under the owner's opencode config, data or state folders. The owner's `auth.json` and `opencode.db` are never written, and the owner's session list gains no worker sessions.
7. **No sandbox, no change.** `off`, an unavailable sandbox under `auto`, or `isolated` false: argv and environment are exactly as today.
8. **UI and MCP.** The setting appears in Settings beside "Isolate workers", as a select with Auto, Required and Off and a one-line explanation. MCP config tools read and patch it like any other `execution` field.

TESTS (deterministic; never the real opencode CLI and never `opencode run`)
- **Fake opencode.** Extend the fake `opencode` in `tests/unit/test_opencode_isolation.py`, or add a new test file. The fake should:
  - resolve its folders the way opencode does (XDG, else `HOME`);
  - give `OPENCODE_AUTH_CONTENT` precedence over `auth.json`;
  - like opencode, write a session file and a log line into its data folder;
  - report what it sees: environment, resolved config, credentials, and the files in its folders;
  - run one shell command the way the bash tool does, reporting what that command sees.
- **Temporary homes.** The owner's `HOME` and `XDG_*` folders are temporary, with two `api` credentials, one `oauth` credential, a global config with several providers, a default model, a permission rule and an MCP server. `CONCLAVE_HOME` is inside the temporary data folder (as with the default `~/.local/share/conclave`).
- **Sandbox tests** need a working bubblewrap: skip them when `sandbox_status()` says unavailable (CI). They cover:
  - the owner's opencode files are byte-identical after sandboxed dispatches, discovery included, and the session is stored under `$CONCLAVE_HOME`;
  - a second dispatch of the same provider sees the first one's session;
  - concurrent dispatches of two providers each see only their own credential;
  - the worker sees only its provider's block and credential: no owner model, permission rule or enabled MCP server, and no other provider;
  - a local provider gets its block and no credential;
  - `oauth` is refused before anything starts;
  - without a model, the owner's default model is used;
  - the daemon's `OPENCODE_CONFIG`, `OPENCODE_CONFIG_DIR`, `OPENCODE_AUTH_CONTENT` and `OPENCODE_DB` are dropped, and `extra_env` can set them;
  - a command the worker runs sees the same `XDG_*` values and the owner's other entries (for example a git config marker);
  - cancelling a dispatch whose fake started a long-running child leaves no process behind.
- **Tests without bubblewrap.** Point `CONCLAVE_BWRAP_BIN` at a missing file:
  - `required` fails with the error and starts nothing;
  - `auto` runs exactly as W10, with the warning;
  - `off` and `isolated` false run as today.
- **Pure unit tests** for the bwrap argv builder and the folder resolution, so CI covers the logic without bubblewrap.
- **Event test.** Under `auto`, with isolation on and the sandbox unavailable, a task records one `log` event naming the cause.
- **Existing W10 tests** whose discovery-environment assumptions change are updated to the new behaviour, never deleted or weakened.

DOCS (same change)
- **AGENTS.md**, the "Workers run isolated by default" bullet: what a sandboxed opencode worker gets (its own opencode folders per provider, only its provider's credential and block, the owner's sessions, config and instructions untouched), `execution.worker_sandbox` and its fallback, and that Claude Code workers are not sandboxed yet.
- **`src/conclave/mcp/pilot_guide.md`.** Section 3 explains the setting. The pitfalls section covers:
  - bubblewrap must be installed, and Ubuntu 24.04's AppArmor restricts unprivileged user namespaces;
  - `oauth` providers cannot be sandboxed;
  - opencode always reads `~/.opencode/` as a config folder, so keep no config there;
  - a change to the owner's opencode config reaches workers after a daemon restart.

  Update `pilot_brief.md` if it describes isolation.
- **The `worker_isolation` description** in `config/models.py` and the Settings help text mention the sandbox.

CONSTRAINTS
- Never weaken or skip an existing test. Never call the live daemon or its MCP tools, and never run `opencode run`.
- Linux only. On a host without bubblewrap nothing changes except the `auto` warning and the `required` failure; Windows and macOS need no new support.
- Rebuild the SPA after the Settings change.
- Quality gate (must stay green): `ruff check src tests && mypy && pytest -q`, `cd frontend && npx tsc --noEmit && npm run build`, and `cd frontend && npx vitest run`.
