# W15 MCP parity: config schema, models, usage, HITL, quarantine integrity, needs-human; compact responses

Close the remaining REST-only gaps so a pilot can operate Conclave entirely over MCP (rest of `docs/self-optimization/specs/r4_mcp_parity.md`).

Already done, do not redo: global config over MCP (`conclave_get_config` / `conclave_patch_config` with `project_id` omitted or `"global"`), and the attach fix (W7).

New MCP tools in `src/conclave/mcp/tools.py`, each mirroring one REST endpoint in `src/conclave/web/api.py`:
1. `conclave_get_config_schema` → `GET /api/config/schema` (valid config keys, types, defaults, descriptions).
2. `conclave_list_models` → `GET /api/models` (model choices plus the `engines` block: default engine, availability, `claude-code/` prefix).
3. `conclave_get_task_usage` (`task_id`) → `GET /api/tasks/{task_id}/usage`.
4. `conclave_get_project_usage` (`project_id`) → `GET /api/projects/{project_id}/usage`.
5. `conclave_get_hitl_state` (`task_id`) → `GET /api/tasks/{task_id}/hitl`: what the task waits on and which checkpoints are armed.
6. `conclave_get_quarantine_integrity` (`project_id`) → `GET /api/projects/{project_id}/quarantine/integrity`.
7. `conclave_list_needs_human` (`project_id`) → `GET /api/projects/{project_id}/needs-human`.

8. Compact responses for pilot context (MCP only; REST unchanged).
   - `conclave_list_projects` returns each project's full stored config, including its whole workflow graph: about 10k tokens per project. By default, return `id`, `name`, `path`, `default_branch`, `mode` and `created_at`; add `include_config: boolean` (default false) for the full objects.
   - `conclave_decide_workflow_proposal` echoes both the proposed and the base graph (about 20k tokens) for a one-line decision. By default, return the decision, the proposal id, state, reason summary/changes and the created version id; add `include_graphs: boolean` (default false).
   - `conclave_get_task` returns the task with every checkpoint artifact and every verdict's full evidence: 110,280 characters for W9 (task 31.5k, checkpoints 48.9k, verdicts 25.3k), too large for a pilot's tool-result limit. By default, return the task (request and plan included), a checkpoint summary (node, attempt, status, tree sha) and verdicts without evidence; add `include_checkpoints` and `include_evidence` booleans (default false) for the full payloads. Keep the response shape W14 introduced (`{"task", "checkpoints", "verdicts", "usage", "hitl"}`, verdicts at most 100) and update the tool description it wrote to say what the default leaves out.
   - Check the other tools for responses that embed a whole workflow graph or config without being asked, and apply the same pattern where it fits. List each change in the tool descriptions and the pilot guide.
   - Tests: default shapes, and the flags returning today's full payloads.

Requirements for each tool:
- It returns exactly the JSON the matching REST endpoint returns, as its text content. Share the logic with the REST handler: extract a helper both call; never duplicate queries.
- Errors come back as a tool result with `isError: true`: an unknown id, a malformed id or argument. A bad input never raises a 500 or an internal error.
- List results are bounded, with the bound stated in the description. Where the REST endpoint takes a `limit`, the tool takes the same argument with the same bounds.
- `tools/list` shows each tool with an accurate `inputSchema` (`required` set correctly) and a description that says what it returns.
- `src/conclave/mcp/pilot_guide.md` lists each tool in the right section. The drift tests (`tests/unit/test_mcp_pilot_guide.py`, `tests/unit/test_mcp_completeness.py`) enforce coverage; extend `test_mcp_completeness.py` if it keeps a REST↔MCP map.

TESTS: integration tests that call each tool over `/mcp` (JSON-RPC `tools/call`) and assert equality with the REST response for the same state (a project with a finished fake task, usage rows, an armed HITL checkpoint, quarantine entries, a needs-human candidate), plus the error cases (unknown ids → `isError`).

CONSTRAINTS
- Never weaken or skip an existing test. Never call the live daemon or its MCP tools.
- Quality gate (must stay green): `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit && npm run build`.
