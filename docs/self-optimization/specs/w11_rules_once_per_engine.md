# W11 Send each project rule file once, per engine (task dispatches and planning turns)

Send each project rule file to an agent exactly once, per engine, for task dispatches and planning-session turns.

CONTEXT
- `engine/orchestrator.py::_read_project_rules(worktree, repo_path)` builds the PROJECT RULES block from two sources:
  - the workspace-level `AGENTS.md` in `repo_path.parent`;
  - the first file that exists among `AGENTS.md`, `CONCLAVE.md`, `CLAUDE.md` (`_PROJECT_RULE_FILES`) at the worktree root.
  It is computed once per task (orchestrator, around `rules = _read_project_rules(...)`) and passed into every dispatch's prompt (`engine/runner.py::assemble_prompt`). `planning/session.py` builds it again from the project checkout for every planning turn.
- Both engines also load rule files natively, so the same text reaches the model twice. Measured on this host:
  - opencode 1.19.5 loads the first of `AGENTS.md`, `CLAUDE.md` (unless `OPENCODE_DISABLE_CLAUDE_CODE_PROMPT` is set), `CONTEXT.md`, searching from its `--dir` up to the git root of that directory. It also loads the user's global `~/.config/opencode/AGENTS.md` and `~/.claude/CLAUDE.md`. A captured request carried this repository's 6.2 kB `AGENTS.md` twice: once natively in the system prompt, once in PROJECT RULES.
  - Claude Code 2.1.326 loads `CLAUDE.md` files natively, from the working directory and its ancestors, with `@path` imports, plus `~/.claude/CLAUDE.md`. It also loads `AGENTS.md` through a built-in plugin, and that plugin stays active under the worker-isolation flags (the live init of an isolated worker lists the built-in plugin `cc-plugin-agents-md`). `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` turns off both, the `CLAUDE.md` loading and the built-in `AGENTS.md` loader.
- Task worktrees live under `$CONCLAVE_HOME/projects/<id>/worktrees/<task>`, each its own git root, so native loading never reaches the workspace-level `AGENTS.md` beside the main checkout.
- `execution.worker_isolation` / `ResolvedProfile.isolated` (default true) exists. Since W10 an opencode dispatch's environment is the daemon's environment, the isolation variables, then `profile.extra_env` (`providers/opencode_cli.py::OpenCodeCliProvider._dispatch_env`): read `OPENCODE_DISABLE_CLAUDE_CODE_PROMPT` from that same daemon environment + `extra_env`. Since W17 the Claude Code isolation environment is the `_ISOLATION_ENV` mapping in `providers/profiles.py`. The engine of each dispatch is `providers/router.py::resolve_engine(profile.model, provider default)`, and a task can mix engines between agents.

REQUIRED BEHAVIOUR
1. The rule files an agent sees depend on the engine of that dispatch, so PROJECT RULES is decided per dispatch, not once per task. Read the files once per task (keep the I/O where it is) and choose per dispatch.
2. opencode dispatches: PROJECT RULES contains the workspace-level `AGENTS.md` (as today) and the selected worktree-root rule file, except a file that opencode loads natively itself. That is opencode's own first match among `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md` at the worktree root; treat `CLAUDE.md` as not loaded when the dispatch environment sets `OPENCODE_DISABLE_CLAUDE_CODE_PROMPT` truthy. `CONCLAVE.md` is never loaded natively, so it always stays.
3. Claude Code dispatches with `isolated=True`: add `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` to `_ISOLATION_ENV` in `providers/profiles.py`, so Conclave is the only source. PROJECT RULES then contains:
   - the workspace-level `AGENTS.md`;
   - the selected worktree-root rule file;
   - when the selected file is not `CLAUDE.md`, the worktree-root `CLAUDE.md` if it exists, because these workers loaded it natively until now and must not lose it.
   For every `CLAUDE.md` Conclave sends, resolve `@path` imports the way Claude Code does:
   - only outside code spans and fenced code blocks;
   - relative to the importing file, recursion depth at most 5, cycles ignored;
   - only files inside the worktree (an absolute path, `~`, or a path escaping the worktree is left as literal text);
   - each imported file appended once after the importing file, under a `# <relative path>` heading.
   Deduplicate the final sections by exact stripped text, as today.
4. Claude Code dispatches with `isolated=False`: native loading stays on, so Conclave omits `CLAUDE.md` (Claude loads it natively). It keeps `AGENTS.md`, because whether Claude Code loads it natively depends on the CLI version; document that duplicate as accepted when isolation is off.
5. Planning-session turns (`planning/session.py`) apply the same per-engine selection, from the project checkout, through the same helper. Move the rule-file logic out of `orchestrator.py` into a small module (for example `engine/project_rules.py`). `planning/session.py` stops importing a private function from the orchestrator.
6. Irreplaceable prompt content must never be cut (AGENTS.md "Critical conventions"). This change only stops sending a second copy of text the engine already receives; it never truncates anything.

TESTS (deterministic; no real CLI)
- Unit tests of the selection helper over a temporary worktree, for each combination of `AGENTS.md` / `CONCLAVE.md` / `CLAUDE.md` / `CONTEXT.md` present, for: opencode (with and without `OPENCODE_DISABLE_CLAUDE_CODE_PROMPT`), Claude Code isolated, and Claude Code not isolated. Assert the exact sections.
- `@import` resolution: nested imports, depth limit, cycle, a missing file, imports inside inline code and inside fenced blocks (not resolved), an absolute path, `~` and a `..` escape (all left literal), and the same file imported twice (sent once).
- `build_invocation`: isolated Claude Code env contains `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`; not isolated does not; `extra_env` can still override it.
- Integration with the fake provider, which records the prompts it receives: a task whose developer runs on `claude-code/...` and whose reviewers run on an opencode model gets the right PROJECT RULES in each dispatch's prompt, with the rule text appearing exactly once. Same for a planning-session turn.

DOCS (same change)
- `src/conclave/mcp/pilot_guide.md`: where project rule files come from per engine, and that `CONCLAVE.md` is the Conclave-only file. `AGENTS.md` "Critical conventions": one bullet.

CONSTRAINTS
- Never weaken or skip an existing test. Never call the live daemon or its MCP tools.
- Quality gate (must stay green): `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit && npm run build`.
