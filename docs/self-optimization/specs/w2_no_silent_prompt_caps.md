Quality-first prompt context: no agent may lose task context to a cut it cannot see, and irreplaceable content is never cut. Today every dispatch whose task context exceeds 6,000 characters is compacted to about 6,000 (`engine/runner.py`, the `compact_prompt_section("TASK CONTEXT", prompt, max_chars=6000)` call; `AgentSettings.compact_before_start` defaults to `True`). On a real install this hit 30% of dispatches, and an 11.5k-character spec reached the developer and every reviewer with most of it removed, so reviewers passed work against a spec they never saw. Models now have very large context windows and prompts are piped via stdin; these cuts save almost nothing and cost correctness.

Rules:
A. Irreplaceable content is never cut, compacted or summarised before it reaches an agent: the task request, the plan (JSON or the planner's free-text brief), reviewer reasons and feedback, operator steer and HITL messages, the Agent-ception session prompt.
B. Recoverable bulk (the current diff shown to reviewers, the previous-attempt diff in retry prompts, gate/test output and setup output fed back to an agent) gets one generous safety ceiling. Compaction is opt-in and only ever applies to this bulk.
C. Every cut is visible: the agent sees a marker saying how much was omitted, which setting limited it, and exactly how to get the rest; the operator sees a Live-tab event.

1. Config: add a section `context: ContextSettings` to `ConclaveConfig` in `config/models.py` (it then appears in `GET /api/config/schema`, `conclave_get_config`, `conclave_patch_config` and the global defaults). Fields; character counts validated with `ge=1_000, le=2_000_000`:
   - `compaction_enabled: bool = False` (opt-in for small-context setups);
   - `compaction_max_chars: int = 60_000` (per-block budget for recoverable bulk when compaction resolves on for the dispatching agent);
   - `bulk_max_chars: int = 200_000` (per-block safety ceiling for recoverable bulk when compaction is off).
   `AgentSettings.compact_before_start` becomes `bool | None = None`, where `None` means "inherit `context.compaction_enabled`". An explicit `True`/`False` still wins, with precedence workflow node (`compact_before_start` on the node, already `bool | None`) > `agent_overrides.<agent>` > `agent_defaults` > `context.compaction_enabled`. Stored configs containing `true` stay valid and now mean "compact this agent's recoverable bulk to `compaction_max_chars`". Update the field descriptions accordingly, and the MCP tool schemas/descriptions that mention `compact_before_start` (`mcp/tools.py`).

2. Remove the whole-prompt compaction from `engine/runner.py` (the 6000 literal and the `compact_prompt_section` call). The runner sends the prompt it is given, unchanged.

3. Add one module, `src/conclave/engine/context_limits.py`, with a pure, typed helper that fits one bulk block to a limit. It keeps the head and the tail (about 40% head, 60% tail, cut on line boundaries where possible) and puts this marker in place of the omitted middle:
   `[... {omitted} of {total} chars omitted (limit: context.{field} = {limit}). {how_to_get_rest} ...]`
   `how_to_get_rest` is specific to the block:
   - current diff: the exact `git diff ...` command, with the same refs the orchestrator used, that reproduces the full diff in the agent's working directory;
   - gate/test output: "Re-run `<the exact command>` in your working directory for the full output.";
   - setup output: the same with the setup command;
   - previous-attempt diff: how to obtain it if it is still recoverable, otherwise say plainly that the rest is not available.
   The helper returns the fitted text plus truncation info. The caller emits exactly one `log` event per actual cut for the task, with payload `{"stage": "context_truncated", "field": ..., "block": "diff" | "previous_diff" | "gate_output" | "setup_output", "original_chars": ..., "kept_chars": ..., "agent": ..., "node_id": ...}`. No event when nothing is cut.

4. Replace every bulk cut with that helper, using the limit resolved for the dispatching agent/node (`compaction_max_chars` if compaction resolves on for it, else `bulk_max_chars`), in both the linear pipeline and the workflow-graph path of `engine/orchestrator.py`:
   - `_MAX_DIFF_CHARS` / `_truncate_diff` (reviewer diffs and the retry prompt's previous diff). Delete `_MAX_DIFF_CHARS` and update the module docstring of `engine/test_integrity.py` that refers to it;
   - `gate.output[-2000:]` (both places), so the developer sees both the first failure and the final summary;
   - the setup-failure output `out[-1000:]`, wherever it reaches an agent or the task's failure reason.
   The limit and compaction setting come from the task-time resolved project config, so a settings change applies to the next dispatch.

5. Remove the cuts on irreplaceable content:
   - `result.text.strip()[:8000]` (planner brief in `_maybe_plan`);
   - `session.prompt[:500]` in `planning/session.py`: store the full prompt in the session task's request (the title may stay short);
   - `verdict.reason[:1000]` in the node checkpoint artifact.
   Check `pg_result.text[:2000]` (orchestrator, checkpoint artifacts around the pass-through review path) and every other checkpoint artifact: if its content is ever read back into a later prompt or used as feedback, store it in full. Display-only previews (event payload `preview` / `request` snippets, log lines, observability rows) may stay short.

6. Manual compaction (`compact_task_context` in `engine/compactor.py`, behind `POST /api/tasks/{id}/compact` and `conclave_compact_session`) must never rewrite `task.plan`: remove the `update_task_fields(..., plan=compacted_plan)` write. It keeps recording its summary checkpoint and statistics.

7. Settings UI: expose the three `context` fields on the project Settings page next to the existing execution/experimental fields, following the existing curated-field pattern (`frontend/src/pages/ConfigPanel.tsx` / `SettingsView.tsx`), each with a one-line help text. Rebuild the SPA (`cd frontend && npm run build`) and commit `src/conclave/web/static/`.

8. Docs in the same change: `src/conclave/mcp/pilot_guide.md` (section 3 key fields: `context.*` and what `compact_before_start` now means; section 11 if relevant), `pilot_brief.md` only if it mentions compaction, and one line each in AGENTS.md / README.md where they describe compaction or config sections.

Tests (deterministic, fake provider, no LLM):
- resolution: workflow node vs `agent_overrides` vs `agent_defaults` vs `context.compaction_enabled`, including `None` inheritance and a stored `true`;
- the helper: unchanged at or below the limit; head + tail + marker above it; the marker names the field, the limit and the recovery instruction; output length bounded by the limit plus the marker;
- integration: with default config, a task whose request is 30,000 characters reaches the developer and every reviewer byte-for-byte (assert on the prompts the fake provider received); a diff larger than a small configured `bulk_max_chars` reaches the reviewer with the marker and exactly one `context_truncated` event; gate output larger than the limit keeps both its first and its last line in the developer's feedback; with compaction enabled for one agent only, only that agent's bulk uses `compaction_max_chars`;
- a guard test that no literal cut of 6000, 8000, 40_000, 2000, 1000 or 500 characters remains in prompt-building code (runner, orchestrator prompt assembly, planning session task creation);
- `compact_task_context` leaves `task.plan` unchanged;
- `GET /api/config/schema` exposes `context`; MCP `conclave_patch_config` with `config={"context": {...}}` round-trips through `conclave_get_config`; out-of-range values return 422 / `isError`.

Constraints: no YAML/config files; keep the public behaviour of everything not listed here; no test skipped or weakened.

Quality gate (must stay green): `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit && npm run build`.
