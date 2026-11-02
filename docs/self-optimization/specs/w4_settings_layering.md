Settings layering: an installation's preferences and Conclave's built-in defaults must keep flowing to projects, instead of being frozen into each project the first time it is attached or saved. Conclave is used in many repositories with different needs: global settings are the installation's preferences, and project settings should hold only what that project really changes.

How it works today (verify each point before changing it):
- `global-defaults` (a `config_preset` store item) is deep-merged into a project's stored config only once, at attach time (`create_project` in `web/api.py`, `conclave_attach_project` in `mcp/tools.py`). At runtime the config is built-in defaults + `project.config` only (`load_project_config(project.config)` in `config/resolver.py`, called from the orchestrator, runtime, task_diff, task_merge, workflow_evolver, planning session, REST and MCP).
- The project Settings page loads the fully resolved document and sends all of it back on save (`frontend/src/pages/SettingsView.tsx`, structured and raw JSON). `patch_config` in `web/api.py` then sees `is_full_doc` and stores the whole resolved document, which freezes every built-in default into the project, so later default improvements never reach it.
- Patching global config with `apply_to_all_projects` copies the patch into every project's stored config (REST `PATCH /api/config/global`, MCP `conclave_patch_config` with `project_id: "global"`, where the parameter defaults to `true`).

Target model:
1. Resolution order is built-in defaults -> global (`global-defaults` payload) -> project (stored delta) -> per-agent overrides (`agent_overrides.<agent>` onto `agent_defaults`, as `resolve_agent` does today; this order applies within each layer). Add one resolution entry point in `config/resolver.py` (for example `async def resolve_project_config(db, project) -> ConclaveConfig`, plus a pure `load_layered_config(global_overrides, project_overrides)` for tests) and use it at every runtime, REST and MCP site that currently calls `load_project_config(project.config)`. A change to global settings applies to every project's next task without touching project rows. After the change, no direct `load_project_config(project.config)` remains outside the resolver (a test greps for it).
2. A project stores only its delta: keys whose value differs from what it would inherit (built-in + global). Enforce this on the server for every write of a project config (REST PATCH/PUT, MCP `conclave_patch_config`, `conclave_attach_project`, presets and templates applied at attach, Agent Studio saves): resolve the merged result, then store the minimal delta. Dicts are compared key by key; lists, `workflow` and other non-dict values are compared whole. Setting a key to the inherited value removes the override, so "unset" now works, and the pilot guide's "deep-merge cannot delete keys" note must be updated. Remove the `is_full_doc` wholesale-replace branch: a full document is just a patch whose unchanged keys reduce to nothing.
3. Attach no longer copies `global-defaults` into the project. It stores only what the caller chose explicitly: `target_branch`/default branch, a named `config_preset`, a `workflow_template`, `max_parallel_tasks`, and any explicit config.
4. The Settings page saves only changed fields (its diff between the loaded and the edited document, for both the structured form and the raw JSON editor). Because the server minimises anyway, correctness does not depend on the client. The page shows the effective (resolved) values as today.
5. `apply_to_all_projects` gets the meaning that fits layering: when true, a global patch also removes the patched keys from every project's stored delta, so every project inherits the new global value. Its default becomes `false` (REST and MCP). Update its tool and parameter descriptions.
6. Inspecting layers: `conclave_get_config` gains an optional `include_layers: bool` (default false). When true it returns `{"effective": ..., "global": <global payload>, "project": <stored delta>}`. Mirror it as `GET /api/projects/{id}/config?layers=true`. Update the MCP tool schema and the pilot guide (tool index included).
7. Existing data: add a one-time, idempotent normalisation that rewrites every existing project's stored config to its minimal delta against built-in + global. The effective config of every project must be identical before and after (a test asserts this on several shapes, including a frozen full document). Run it once on startup through the migration mechanism (`db/migrations/__init__.py`; if it only supports SQL, add a small, append-only way to run a Python data migration recorded in `schema_version`) and log how many projects changed.

Constraints:
- Config stays SQLite-backed (no YAML/config files); migrations are append-only.
- Validate inputs: bad patches return 4xx / `isError`, never 500.
- Behaviour for a project with no stored overrides must equal built-in + global.
- Keep `global-defaults` as the storage location for global settings.

Docs in the same change: `src/conclave/mcp/pilot_guide.md` (section 3: layers, deltas, how to unset, what `apply_to_all_projects` does now, `include_layers`; section 11 pitfalls; tool index if tool arguments changed), `pilot_brief.md` only if affected, and AGENTS.md "Critical conventions" (one line about layered config and delta storage).

Tests (deterministic, no LLM):
- resolution order, including agent overrides at both global and project layers;
- a global change reaches an existing project's next resolution without touching the project row; a project override still wins;
- saving the full resolved document stores an empty delta; changing one field stores exactly that field; setting it back to the inherited value removes it;
- attach stores no copy of `global-defaults`;
- `apply_to_all_projects: true` clears the patched keys from every project; the default is false;
- normalisation: effective config identical before and after for a frozen full document, a partial delta, agent overrides, and a custom workflow; it is idempotent;
- `include_layers` over MCP and REST;
- a grep guard: no `load_project_config(project.config)` outside the resolver;
- frontend: `npx tsc --noEmit` passes; rebuild the SPA and commit `src/conclave/web/static/`.

Quality gate (must stay green): `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit && npm run build`.
