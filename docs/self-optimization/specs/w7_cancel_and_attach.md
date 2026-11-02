Two correctness fixes observed while piloting.

A. Cancellation must stop work immediately. Observed: cancelling a task during review led to the reviewer being retried 3 times with the error "cancelled", and a new `attempt.started` event fired after the cancel. A cancelled task kept spending tokens and produced confusing events.
1. Every retry or continuation point checks cancellation before it acts, and a dispatch that ended because of cancellation is never retried:
   - the reviewer dispatch retries (`_REVIEWER_DISPATCH_RETRIES` in `engine/orchestrator.py`);
   - the develop/review attempt loop (no new `attempt.started` after cancellation);
   - transient-error retries inside the providers (`providers/claude_cli.py`, `providers/opencode_cli.py`);
   - workflow-graph node execution;
   - any other retry loop you find in `engine/` and `providers/`.
2. Once a task is cancelled, no `agent.dispatched`, `attempt.started` or retry events are emitted for it. The task ends `cancelled` through the existing `_finish_cancelled` path (worktree cleanup unchanged).
3. Make "this result ended because of cancellation" explicit (for example a flag on the agent result, or a check of the cancel event) rather than matching the error text "cancelled".
Tests: with the fake provider, cancel during a reviewer dispatch: no retry dispatch happens, no `attempt.started` after the cancel event, final state `cancelled`. Same for cancel during the developer dispatch. A provider-level test: a cancelled run is not retried even if its output looks transient.

B. Attaching an already-attached path must not modify the existing project. Today `POST /api/projects` (`web/api.py`, the `get_project_by_path` branch) and MCP `conclave_attach_project` (`mcp/tools.py`) treat it as an upsert: they rename the existing project and deep-merge the new config into it. Observed: attaching a second configuration to the same repository silently renamed the main project and changed its config.
1. When the path (resolved, as the code already compares it) is already attached, return 409 (REST) / `isError` (MCP). Do not change anything. The message names the existing project's id and name and says how to change it: `conclave_patch_config` (config, `name`) or `PATCH /api/projects/{id}/config`, or attach a separate clone to run another configuration.
2. Remove the upsert code paths. Keep normal attach behaviour unchanged for new paths.
3. Update `src/conclave/mcp/pilot_guide.md` (section 3 step 1 and section 11, which today document the upsert) and the tool description of `conclave_attach_project`.
Tests: REST and MCP attach of an already-attached path returns 409 / `isError` with the existing id, and the existing project's name and config are byte-for-byte unchanged; attaching a new path still works; frontend attach handles the 409 by showing the message (check the attach form in `frontend/src`; if it needs a change, rebuild the SPA and commit `src/conclave/web/static/`).

Constraints: no test skipped or weakened; keep behaviour not listed here.

Quality gate (must stay green): `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit && npm run build`.
