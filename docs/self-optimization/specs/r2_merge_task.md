Add a way for an operator (a human, or an AI pilot driving Conclave purely through MCP) to merge a finished-but-unmerged task into its target branch. Today, when `execution.auto_merge` is false, a done task keeps its committed branch but nothing in the API, MCP server or UI can merge it.

1. REST: `POST /api/tasks/{task_id}/merge` (no body).

2. MCP: a new tool `conclave_merge_task` with argument `task_id` (string, required). Its text content must be exactly the JSON object the REST endpoint returns on success; errors use `isError: true` like the existing tools. Listed by `tools/list` with `required: ["task_id"]`.

3. Success response (200 / tool JSON): `{"task_id": str, "merged": true, "target": <target branch>, "branch": <task branch>, "target_sha": <full 40-hex SHA of the target branch after the merge>}`.

4. Preconditions and errors (never 500 for bad input):
   - unknown task → 404
   - task state is not `done` → 409 with detail containing "not done"
   - task branch (`<execution.branch_prefix><task_id>` or the task's recorded `branch`) does not exist → 404 with detail containing "branch not found"
   - target branch already contains the task branch tip → 409 with detail containing "already merged"
   The target is always the project's configured `execution.target_branch`; there is no parameter to merge elsewhere.

5. Merge through the orchestrator's existing `Orchestrator._merge` (it already holds the per-target lock, does the guarded fast-forward / merge commit and conflict detection). Do not write a second merge implementation.
   - On success: emit a `task.merged` event (payload `{"target": ...}`, same shape as auto-merge), delete the task branch exactly as the auto-merge path does after a successful merge, and set the task's `result_summary` to note it was merged into the target by an operator. The task stays `done`.
   - On `MergeResult.conflict`: return 409 with detail containing "merge conflict"; the target branch must be unchanged, the task branch preserved, the task left `done`, and no merge left in progress anywhere (no MERGE_HEAD, clean status in the repo checkout).
   - On `MergeResult.error`: return 503 with a clear detail (transient; retryable).

6. Tests: integration tests for a successful merge via REST and via MCP (target contains the change, branch deleted, event emitted), not-done task, missing branch / second merge, unknown task (REST + MCP), conflict (target unchanged, branch kept, checkout clean), and the tool schema in `tools/list`.

7. Keep the quality gate green: `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit`. Follow AGENTS.md conventions.
