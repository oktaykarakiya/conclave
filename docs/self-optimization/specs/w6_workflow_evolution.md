Workflow evolution must be suggest-only by default, and every workflow change must be versioned with a reason and reversible. Today `evolve_project_workflow` (`engine/workflow_evolver.py`) runs after every task (`orchestrator.py`, in the task's `finally` path) and silently rewrites the project's persisted workflow graph (grow: add/promote reviewers, enable the planner, raise `max_iterations`; shrink: demote reviewers after a clean streak). There is no off switch, no history and no undo, so a project's pipeline can drift without anyone deciding it.

1. Setting: `experimental.workflow_evolution: Literal["off", "suggest", "auto"] = "suggest"`, in the Settings UI with a one-line help text, and settable over MCP `conclave_patch_config`.
   - `off`: the automatic post-task evolution does nothing (no computation, no proposal, no event).
   - `suggest`: the evolver computes the change it would make. If the graph would change, it stores a proposal (the proposed graph, the current graph it was computed from, the reason: action, promoted/demoted agents and the evidence such as the task id and catching reviewers). It emits `workflow.evolution_proposed`. It never modifies the live workflow. While a proposal is pending, an identical one is not stored again.
   - `auto`: applies the change as today, and records it as a workflow version (below).

2. Versioning: every change to a project's persisted workflow creates a version row (id, project_id, created_at, source: `evolver` | `operator` | `template` | `revert` | `proposal`, reason text, the graph after, the graph before). Covered paths: the evolver in `auto`; `PUT /api/projects/{id}/workflow`; `POST /api/projects/{id}/workflow/evolve`; MCP `conclave_set_workflow`, `conclave_apply_workflow_template`, `conclave_workflow_upsert_node`, `conclave_workflow_remove_node`, `conclave_workflow_upsert_edge`, `conclave_workflow_remove_edge`, `conclave_evolve_workflow`; config patches that change `workflow`. Route them through one function so no path can skip versioning. Optional `reason` parameters on the operator-facing endpoints and tools are recorded when given. Add the storage with a new append-only migration in `db/migrations/__init__.py` and typed repository functions in `db/repositories`.

3. Operations, REST and MCP (keep both in parity):
   - list versions: `GET /api/projects/{id}/workflow/versions?limit=` (bounded, newest first) and `conclave_list_workflow_versions(project_id, limit?)`;
   - revert: `POST /api/projects/{id}/workflow/versions/{version_id}/revert` and `conclave_revert_workflow(project_id, version_id, reason?)`. This restores that version's graph and records a new `revert` version; it never deletes history. Reverting to the built-in derived pipeline (no custom workflow) must be possible: a version may hold `null`;
   - proposals: `GET /api/projects/{id}/workflow/proposals?state=` and `conclave_list_workflow_proposals(project_id, state?)`; decide with `POST /api/projects/{id}/workflow/proposals/{proposal_id}/approve|reject` and `conclave_decide_workflow_proposal(project_id, proposal_id, decision: "approve"|"reject", reason?)`. Approving applies the proposed graph (source `proposal`). Applying is refused with a clear 409 / `isError` if the live workflow has changed since the proposal was computed, telling the operator to re-run evolution. Rejecting records the decision.
   - `conclave_evolve_workflow` / `POST .../workflow/evolve` (manual): in `suggest` mode they create a proposal and return it. An explicit `apply: true` argument applies immediately with a version (an operator decision).
   - Bad ids return 404 / `isError`, bad input 4xx; never 500.

4. UI: on the workflow page, show pending proposals (reason, a readable summary of what changes, approve/reject) and a version history (source, reason, time, revert). Follow existing components in `frontend/src/ui.tsx` and the workflow page's patterns. Rebuild the SPA (`cd frontend && npm run build`) and commit `src/conclave/web/static/`.

5. Docs in the same change: `src/conclave/mcp/pilot_guide.md` (mental model: workflow evolution modes; a short section on reviewing proposals and reverting; the tool index with the new tools; pitfalls). The drift tests require every new tool in the index. Add one line to `pilot_brief.md` if it lists rules about workflows. Add a line to AGENTS.md if it describes the evolver.

Tests (deterministic, fake provider, no LLM):
- `off`: a task that would trigger growth leaves the workflow unchanged, with no proposal and no event;
- `suggest` (default): same task, workflow unchanged, exactly one pending proposal with a reason and a `workflow.evolution_proposed` event; a second identical trigger does not duplicate it; approve applies it and creates a `proposal` version; reject leaves the workflow unchanged; approve after the live workflow changed returns 409 / `isError`;
- `auto`: the change is applied and a version with source `evolver` is recorded;
- every mutation path listed in item 2 creates a version (parametrised test over REST and MCP);
- revert restores an earlier graph (including `null`) and adds a `revert` version;
- list endpoints are bounded and newest first; bad ids give 404 / `isError`;
- MCP tool list and pilot guide drift tests pass with the new tools.

Constraints: append-only migrations; config stays SQLite-backed; no test skipped or weakened; keep behaviour not listed here.

Quality gate (must stay green): `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit && npm run build`.
