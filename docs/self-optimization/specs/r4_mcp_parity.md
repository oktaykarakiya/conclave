Close the remaining gaps between the REST API and the MCP server, so an AI pilot can operate Conclave entirely through MCP. Every new tool returns exactly the JSON the matching REST endpoint returns (as its text content), reports errors with `isError: true`, validates inputs (never a 500 for bad input), bounds list sizes, and is listed in `tools/list` with an accurate `inputSchema` (`required` set correctly).

New MCP tools (REST endpoint each mirrors in parentheses):
1. `conclave_get_global_config` (GET /api/config/global) and `conclave_patch_global_config` with args `config` (object, required) and `apply_to_all_projects` (boolean, default false) (PATCH /api/config/global).
2. `conclave_get_config_schema` (GET /api/config/schema) — so a client can discover valid config keys.
3. `conclave_list_models` (GET /api/models) — includes the `engines` block (default engine, availability, `claude-code/` prefix).
4. `conclave_get_task_usage` (GET /api/tasks/{task_id}/usage) and `conclave_get_project_usage` (GET /api/projects/{project_id}/usage).
5. `conclave_get_hitl_state` (GET /api/tasks/{task_id}/hitl) — what a task is waiting on, which checkpoints are armed.
6. `conclave_get_quarantine_integrity` (GET /api/projects/{project_id}/quarantine/integrity).
7. `conclave_list_needs_human` (GET /api/projects/{project_id}/needs-human).

Fix: `conclave_attach_project` on a path that is already attached currently updates that project silently, including RENAMING it to the new `name` and deep-merging config. Change it to: return the existing project unchanged with an added top-level `"already_attached": true` field (new attaches return `"already_attached": false`); never rename or modify an existing project from attach (use `conclave_patch_config` for that). Document this in the tool description.

Implementation: share logic with the REST handlers instead of duplicating it (extract helpers where needed). Add integration tests that call each tool over `/mcp` (JSON-RPC `tools/call`) and assert equality with the REST response, plus error cases (unknown ids → isError) and the attach behaviour above. Keep the quality gate green: `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit`.
