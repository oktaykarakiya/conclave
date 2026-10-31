Add a read-only way to see a task's code change over BOTH the REST API and the MCP server, so an operator (a human, or an AI pilot driving Conclave purely through MCP) can review work without shell access.

1. REST: `GET /api/tasks/{task_id}/diff` with query params `stat_only: bool = false` and `max_bytes: int = 200000`. Validate 1 <= max_bytes <= 2_000_000, otherwise 422.

2. MCP: a new tool `conclave_get_task_diff` with arguments `task_id` (string, required), `stat_only` (boolean, default false), `max_bytes` (integer, default 200000). Its text content must be exactly the same JSON object the REST endpoint returns. Errors are reported with `isError: true`, like the existing tools. It must be listed by `tools/list` with that input schema (`required: ["task_id"]`).

3. Response object (REST body == MCP tool JSON):
   - `task_id` (str)
   - `source`: `"worktree"` or `"branch"`
   - `base`: full 40-hex SHA the change is compared against
   - `head`: full SHA of the task branch tip when `source == "branch"`; `null` when diffing a live worktree
   - `stat`: `git diff --stat` output for the same comparison
   - `diff`: unified diff text (empty string when `stat_only`)
   - `truncated`: true when `diff` was cut to fit `max_bytes`; the returned `diff` must then be at most `max_bytes` bytes when UTF-8 encoded (never split a multi-byte character)

4. Source resolution, in this order:
   a. The task's worktree exists (the orchestrator's per-project worktree root, i.e. `WorktreeManager(...).path_for(task_id)` under `<home>/projects/<project_id>/worktrees`) → `source="worktree"`. Compare the worktree's CURRENT state, including untracked files, against the merge-base of the task branch and the project's target branch. Skip the transient cache dirs the orchestrator already ignores (reuse `_TRANSIENT_CACHE_DIRS`; do not duplicate the list) and anything `.gitignore` excludes.
   b. Else the task branch exists (`<execution.branch_prefix><task_id>`, or the task's recorded `branch`) → `source="branch"`, comparing `merge-base(target_branch, task_branch)..task_branch`.
   c. Else → 404 with detail `"no diff available for this task"` (not started yet, or merged and its branch deleted).

5. Unknown `task_id` → 404 (REST) / `isError: true` (MCP). Never return 500 for bad input.

6. Strictly read-only: never modify the repository, the worktree, or the worktree's real index. To include untracked files, build the comparison in a temporary index (`GIT_INDEX_FILE` pointing at a temp file seeded from the worktree's index) — do not run `git add` against the real index, and do not delete files.

7. Tests: integration tests for the branch source (REST and MCP return identical JSON), `stat_only`, truncation, `max_bytes` validation, unknown task, a task with no diff available, the worktree source (untracked files included, cache dirs excluded, worktree left untouched), and the tool's schema in `tools/list`.

8. Keep the quality gate green: `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit`. Follow AGENTS.md conventions (typed, validated inputs, 4xx not 500).
