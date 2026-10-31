Quality-first: remove silent prompt caps and make every context limit an explicit, generous, configurable setting. Conclave is used in many repositories for very different purposes; no agent should ever lose task context to a hard-coded cut it cannot see.

1. New config section `context` on `ConclaveConfig` (so it appears in the UI Settings form via the JSON schema, in `conclave_get_config` / `conclave_patch_config` over MCP, and in the global defaults), with these fields, validated (>= 1000, <= 2_000_000 where they are character counts):
   - `compaction_enabled: bool = False` — replaces the per-agent `compact_before_start` default. Keep `AgentSettings.compact_before_start` but make its default `None` meaning "inherit `context.compaction_enabled`"; an explicit True/False still wins for that agent/node.
   - `compaction_max_chars: int = 60_000` — budget used ONLY when compaction is enabled (today hard-coded 6000 in `engine/runner.py`).
   - `gate_feedback_max_chars: int = 20_000` — tail of the failing gate/test output passed back to the developer (today `gate.output[-2000:]` in `engine/orchestrator.py`).
   - `previous_diff_max_chars: int = 200_000` — previous-attempt diff in retry prompts (today `_MAX_DIFF_CHARS = 40_000`).
   - `planner_brief_max_chars: int = 50_000` — non-JSON planner output forwarded to the developer (today `[:8000]`).
   - `review_reason_max_chars: int = 20_000` — reviewer reasons stored in checkpoints/events (today `[:1000]`).
2. Whenever any of these limits actually truncates text, the text must carry an explicit marker the agent can see (e.g. `[... truncated N of M chars — limit context.<field>]`) AND a `log` event (stage `context_truncated`, payload with field name, original and kept lengths, agent/node) is emitted so the operator sees it in the Live tab. No silent cuts anywhere in prompt assembly.
3. Store the full Agent-ception session prompt in the parent "session" task (`planning/session.py` uses `session.prompt[:500]`); titles may stay short.
4. Wire every call site to read the resolved project config (task-time config, so a settings change applies to the next dispatch). Do not leave any of the old literals in prompt-building code paths.
5. Tests: unit tests for the resolution rules (compact_before_start None/True/False vs context.compaction_enabled), each limit honoured with marker + event when exceeded, nothing truncated at defaults for realistic sizes (e.g. a 30k-char prompt is passed through unchanged), schema exposes the new section, and MCP `conclave_patch_config` can set `context.*`.
6. Update AGENTS.md (config section) and README briefly. Keep the quality gate green: `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit`.
