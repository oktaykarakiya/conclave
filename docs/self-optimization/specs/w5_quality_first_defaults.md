Quality-first built-in defaults. Conclave is installed in many places for very different purposes; out of the box it must favour correctness over speed and cost, and every such default must stay settable (Settings UI, `conclave_patch_config`, global defaults). Settings layering (built-in -> global -> project) already exists, so changed built-ins reach every project that does not override them.

1. `execution.auto_merge` defaults to `false` (`config/models.py`, today `True`). A green task ends `done` on its committed branch and a human (or pilot) merges it. Update the field description. Built-in config presets that set `auto_merge` explicitly (`bootstrap.py`) keep their explicit value.

2. Effort: "highest sensible per engine", never failing a dispatch.
   - `AgentSettings.effort` stays `Effort | None = None`, but `None` now means "auto". At dispatch, resolve auto to the highest sensible level for the engine/model:
     - Claude Code (`claude-code/...`): `xhigh` (the CLI accepts low, medium, high, xhigh, max; `max` is deliberately not the automatic choice);
     - opencode: the highest variant the model reports. `opencode models <provider> --verbose` prints per-model JSON with a `variants` object (for example `deepseek/deepseek-flash` reports `low`, `high`, `max`). Read it once per model with a timeout, cache it in-process, and never block or fail a dispatch on it. If no variants are known, pass no variant flag.
   - An explicit effort the model does not support is clamped to the nearest supported level, preferring the next lower one (for an opencode model whose variants are known). Emit one `log` event (stage `effort_clamped`, payload requested/used/model/agent) instead of failing.
   - The resolved effort is visible in the `agent.dispatched` payload (`effort`).
   - Put the resolution in one place (`providers/`), shared by both engines. Do not duplicate engine detection; reuse `providers/router.py` (`resolve_engine`, `CLAUDE_CODE_PREFIX`).

3. Planning by default: `experimental.auto_planner_char_threshold` defaults to `0` (the planner runs for every task; today 400). Update the built-in personas in `DEFAULT_PERSONAS` for `planner`, `architect-as-planner`, `pm` and `test-architect` to be research-first: read the relevant code before planning; verify current official documentation for every library, API, CLI or service the plan relies on (use web search/fetch tools when available); cite the sources (URL or file path) in the plan; mark anything unverified as an assumption. Keep each persona's existing role and output contract (the planner's JSON plan format must not change). Existing installs keep personas already stored in their DB (seeding only inserts missing ones); say so in the pilot guide.

4. Hot reload off by default. The background file watcher (`Daemon._watch_reload_loop` in `runtime.py`) must only auto-reload when a daemon-wide setting enables it, default off. Put the setting in the global layer (for example a `system.hot_reload_watch: bool = False` section, or an equivalent that fits the existing models), editable in the global Settings UI and over MCP `conclave_patch_config(project_id="global", ...)`, and read live by the loop (no restart needed; a project-level value is ignored). Manual reload (`conclave_reload_system`, `POST` reload endpoint, UI button) and `conclave_get_reload_status` keep working, and the status reports whether the watcher is enabled.

5. `GET /api/models` stops hard-coding the default model and the offered list (`_DEFAULT_MODEL`, `_RECOMMENDED_MODELS` in `web/api.py`):
   - Optional query `project_id`. `default_model` comes from the resolved config's `agent_defaults.model` (that project's, or global when omitted).
   - The offered list is configurable: a new config field (for example `ui.model_choices: list[str] = []`). When non-empty, use it. When empty, fall back to the built-in list, filtered to engines available on this host (`engine_status`). Always include every model referenced in the resolved config (`agent_defaults`, `agent_overrides`, workflow nodes) so the picker never hides a configured model.
   - Keep the response shape and the existing keys (including `engines`). The frontend model pickers send `project_id` when they have one.
   - Expose the field in the Settings UI.

6. Settings UI: every new or changed field above appears with a one-line help text (project Settings, and global Settings for the daemon-wide setting). Rebuild the SPA (`cd frontend && npm run build`) and commit `src/conclave/web/static/`.

7. Docs in the same change: `src/conclave/mcp/pilot_guide.md` (golden rules, section 3 defaults/engines/effort, section 6 the `effort` field in `agent.dispatched`, section 10 hot reload) and `pilot_brief.md` if affected; AGENTS.md / README.md where they state these defaults.

Tests (deterministic, no LLM; stub the opencode variants lookup):
- defaults: `ConclaveConfig()` has `auto_merge` false and threshold 0; the planner runs for a short request;
- effort auto: claude-code resolves to `xhigh`; an opencode model with variants `low`/`high`/`max` resolves to `max`; unknown variants give no flag; an explicit unsupported level is clamped with one `effort_clamped` event; a lookup failure or timeout never fails a dispatch;
- personas: the built-in planning personas contain the research-first instructions, and the planner JSON contract tests still pass;
- hot reload: the watcher does not reload when the setting is off; it reloads when turned on at runtime; a manual reload works with the setting off;
- `/api/models`: default from global and from a project override; configured list honoured; configured models always included; unavailable engines filtered from the fallback list;
- schema and MCP round trip for every new field.

Constraints: config stays SQLite-backed; no test skipped or weakened; keep behaviour not listed here.

Quality gate (must stay green): `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit && npm run build`.
