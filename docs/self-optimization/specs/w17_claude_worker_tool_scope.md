# W17 Claude Code worker tool scope, no background wake-ups, full usage, limits never become timeouts

Scope isolated Claude Code workers to the tools a coding task needs, stop post-answer wake-ups from background tasks, count a dispatch's full usage, and never let a usage limit turn into a timeout.

MEASURED on the live daemon (Claude Code 2.1.326, worker isolation from W8 on). The `system/init` event of a planner dispatch listed these tools:
`Task, Bash, CronCreate, CronDelete, CronList, DesignSync, Edit, EnterWorktree, ExitWorktree, ListAgents, Monitor, NotebookEdit, PushNotification, Read, RemoteTrigger, ReportFindings, ScheduleWakeup, SendMessage, TaskStop, ToolSearch, WebFetch, WebSearch, Workflow, Write`.
W8 removed MCP servers, user settings, skills, slash commands and auto-memory, but not these built-in tools. A headless worker inside a task worktree can therefore:
- message other Claude sessions on the machine, the operator's own included (`ListAgents`, `SendMessage`);
- create scheduled or remote agents on the account (`CronCreate`, `RemoteTrigger`, `ScheduleWakeup`);
- push notifications to the owner (`PushNotification`);
- launch multi-agent workflows (`Workflow`);
- leave its worktree (`EnterWorktree`, `ExitWorktree`).
None of this belongs in a Conclave dispatch.

The same planner also ran `pytest` with `run_in_background`. The background task finished after the planner's final answer, and Claude Code woke the session for a second turn: a second `system/init`, about 60 s and $0.41, repeating the same answer. The stream then had two `result` events. `claude_cli._scan_stream` keeps only the last one. `total_cost_usd` is cumulative across the session, so cost was right. But `num_turns` and the `usage` tokens are per turn: Conclave recorded 1 turn and 8,620 output tokens instead of 103 turns and 129,115 output tokens.

REQUIRED
1. Tool allowlist for isolated workers (`ResolvedProfile.isolated`, `providers/profiles.py` `build_invocation`).
   - Pass `--tools` with an explicit allowlist of built-in tools: `Task, Bash, Read, Edit, Write, NotebookEdit, WebFetch, WebSearch, ToolSearch`, plus any read/search/todo tool the installed CLI has for code work (check `claude --help` and the init `tools` list of the installed CLI, and record what you checked in the plan's evidence).
   - The allowlist is a module constant, documented with the reason for each excluded tool.
   - Not isolated: the argv is unchanged (no `--tools`).
   - `--tools` must combine correctly with the W8 isolation flags and with `--resume` (W9).
2. No background tasks in isolated workers.
   - Set `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` and `CLAUDE_CODE_DISABLE_CRON=1`.
   - So that long test runs still work in the foreground, set `BASH_MAX_TIMEOUT_MS` to the dispatch's timeout in milliseconds. Set `BASH_DEFAULT_TIMEOUT_MS` to the smaller of 20 minutes and that timeout. `ClaudeCliProvider.run_agent` already receives `timeout_seconds`: derive both values there, in one helper, so every dispatch path gets them without new plumbing.
   - Values the profile's `extra_env` sets still win.
3. Usage across several `result` events (`providers/claude_cli.py`).
   - When a stream has more than one `result` event for the same session, sum `num_turns`, input, output, cache-read and cache-creation tokens across them.
   - `total_cost_usd` is cumulative: take the last. `duration` is the sum.
   - The text, `is_error`, session id and quota detection come from the last `result`.
   - Keep the per-model breakdown consistent with the summed totals.
4. A usage limit seen before a timeout is still a usage limit (`providers/claude_cli.py`, from W9's review).
   - Today the timeout path returns `timed out after Ns` without parsing what the run streamed. Suppose Claude Code retried a refused request internally, or waited inside the run, until the agent timeout killed it. W9's `quota` detection would then be lost: the dispatch fails as a timeout, and the attempt is consumed.
   - On the timeout path, parse the partial stream collected so far. When it shows a rejected `rate_limit_event`, or an assistant `error` of `rate_limit` / `billing_error`, return the quota result (with the session id) instead of the timeout.
   - Also watch the stream live. Once a rejected `rate_limit_event` has been seen and the process has not exited within a grace period (module constant, 120 s), stop the process group the way the timeout path does and return the quota result. The shared quota gate then waits for the reset and resumes the session.
   - Tests with a fake `claude` that prints a rejected event and then sleeps:
     - with a short timeout: the result has `quota` set, not a timeout error;
     - with the grace constant patched small: the run is stopped after the grace period with `quota` set;
     - a run that prints `allowed_warning` and then completes normally is unaffected.
5. Reset time of a burst rate limit (`_detect_quota`, from W9's review). When the last assistant message has `error: "rate_limit"` but the latest `rate_limit_info` does not refuse (status `allowed`, or overage serving), the account's windows are not exhausted. It is a short-term rate limit, so the window's normal `resetsAt`, possibly hours away, is not the time to resume. Take a reset time from the latest info only when its status is `rejected` or `allowed_warning`. Otherwise leave `resets_at` unset, so the gate uses `execution.quota_retry_minutes`. Tests: `error: "rate_limit"` with an `allowed` info gives `resets_at=None`; with `allowed_warning` it gives that window's reset; with `rejected` it is unchanged.
6. Visibility. The pilot guide (§3 engine/isolation section) lists the allowed tools, the background-task switch, and how to opt out (`execution.worker_isolation: false`). AGENTS.md's isolation bullet mentions the tool allowlist and the background-task switch.

TESTS (deterministic; no real CLI)
- `build_invocation` with `isolated=True` contains `--tools` with exactly the allowlist and the two disable switches. With `isolated=False`, neither appears. `extra_env` overrides each env var. `--resume` still works alongside.
- A provider test with a fake `claude` executable that records its argv/env, called with `timeout_seconds=1800`:
  - the isolated dispatch carries `--tools`, both switches, `BASH_MAX_TIMEOUT_MS=1800000` and `BASH_DEFAULT_TIMEOUT_MS=1200000`;
  - with `timeout_seconds=600`, both Bash values are `600000`;
  - a non-isolated dispatch carries none of them.
- Parsing: a stream with two `result` events (the second with `num_turns: 1`) gives summed turns and tokens and the last cumulative cost; a single-result stream is unchanged.

CONSTRAINTS
- Never weaken or skip an existing test. Never call the live daemon or its MCP tools; never run the real `claude` CLI in tests.
- Quality gate (must stay green): `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit && npm run build`.
