# W9 Usage limits: wait and resume the interrupted dispatch, never fail or degrade

When an engine's usage limit or balance runs out, wait and resume the interrupted dispatch, never fail or degrade the work.

PROBLEM: Conclave has no notion of a usage limit. Today, when Claude Code's 5-hour or weekly limit is reached, or the opencode provider rejects a request for quota or balance (measured: DeepSeek answers HTTP 402 Insufficient Balance), the dispatch simply ends with `ok=False`:
- The developer attempt fails. The retry loop then reruns it immediately (`max_retries`), the task fails, and the worker starts the next queued task, which fails the same way. One limit hit fails the whole queue within minutes.
- A failed planner dispatch is silently skipped: `_plan` only uses the plan when `result.ok`, so the developer proceeds without a plan.
- A failed reviewer dispatch becomes a non-blocking `unknown` verdict. The all-unknown guard ("ENG-3") only catches a round in which EVERY reviewer failed. A round in which the limit hit two of five reviewers passes with three, and the code is merged with part of the panel silently skipped.
- Claude Code's result can carry the limit message ("... usage limit reached · resets 3pm") as text. `claude_cli._parse_output` decides `ok` only from the exit code, the success hint and `is_error`, so a limit hit might even count as a successful dispatch with useless text.
Required instead: the dispatch waits until the limit resets and then continues where it stopped, for every kind of dispatch, and the operator can see the wait.

MEASURED FORMATS (verify again against the installed CLIs, Claude Code 2.1.326 and opencode 1.19.5, and the Claude Agent SDK documentation for the stream-json message types):
- Claude Code stream-json emits, on every turn, a `rate_limit_event`:
  `{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1794924600,"rateLimitType":"five_hour","overageStatus":"rejected","isUsingOverage":false,"unifiedWindows":{"five_hour":{"utilization":0.02,"resetsAt":1794924600,"status":"allowed"},"seven_day":{"utilization":0.27,"resetsAt":1795388400,"status":"allowed"}}},"session_id":"...","uuid":"..."}`
  `status` is one of `allowed`, `allowed_warning`, `rejected`. `resetsAt` is epoch seconds.
- When a request is refused, an `assistant` message carries a top-level `"error"` field: `"rate_limit"` for limits and `"billing_error"` for billing; other values exist (for example `authentication_failed`, `server_error`). The CLI's own messages read "usage limit reached · continues automatically when it resets", "You've hit your limit · resets <time>", "usage limit reached — check plan".
- opencode `run --format json` emits `{"type":"error","sessionID":"ses_...","error":{"name":"APIError","data":{"message":"Insufficient Balance (request_id: ...)","statusCode":402,"isRetryable":false,"responseHeaders":{...}}}}` and exits 1. A 429 carries `statusCode: 429` and usually a `retry-after` (or `retry-after-ms`) response header.

REQUIRED BEHAVIOUR
1. Detection, in the providers. `AgentResult` gets a new optional field `quota: QuotaExhausted | None`, a frozen dataclass in `providers/base.py` with these fields:
   - `kind`: `"rate_limit" | "billing"`;
   - `resets_at: datetime | None`, timezone-aware UTC;
   - `window: str | None`, for example `five_hour`, `seven_day`, `seven_day_opus`;
   - `message: str`, the provider's text.
   - Claude Code (`providers/claude_cli.py`): set `quota` when the stream has a `rate_limit_event` with `status == "rejected"`, or an assistant message with `error` in (`rate_limit`, `billing_error`), or, as a fallback for older CLIs, a final text matching the usage-limit messages above (including the legacy `Claude AI usage limit reached|<epoch>` form, whose epoch is the reset time). `resets_at` comes from the rejected window's `resetsAt`, else from the latest `rate_limit_info`, else from the legacy epoch, else `None`. A result with `quota` set is never `ok`.
   - opencode (`providers/opencode_cli.py`): set `quota` from an `error` event whose `statusCode` is 402 (`billing`, no reset time) or 429 (`rate_limit`, `resets_at` = now + `retry-after`/`retry-after-ms` when present), or whose message matches insufficient balance / quota exceeded / rate limit patterns. A quota error is not a transient error: the provider's own quick retry loop must not burn attempts on it.
   - Keep the latest `rate_limit_info` a Claude Code dispatch reported (status, windows with utilization and reset times). The quota gate below exposes it.
2. Waiting, one shared mechanism. A daemon-wide `QuotaGate`, keyed per engine and account: Claude Code dispatches share one key per engine profile; opencode dispatches share one key per (profile, model provider prefix), for example `deepseek`.
   - When a dispatch returns `quota`, the gate closes for that key until `resets_at` plus a margin of 60 s. With no reset time, it closes for `execution.quota_retry_minutes`, a new setting (default 15, 1 to 1440, layered like every setting, shown on the Settings page).
   - Every later dispatch for that key, from any project, waits for the gate before starting, so parallel reviewers and other projects do not each hammer the API.
   - When the gate reopens, the interrupted dispatch runs again. When the provider reported a session id, it resumes that session with a short continuation prompt ("You were interrupted by a usage limit. Continue the same task from where you stopped; the worktree contains your partial work."): Claude Code `--resume <session_id>`, opencode `--session <id>`. When resuming fails (the CLI reports an unknown session), it falls back to a fresh dispatch of the original prompt. A dispatch that hits the limit again simply waits again; there is no cap on the number of waits.
   - The wait is cancellable at once: cancelling the task, or the planning session, ends it within a second, with the normal cancelled outcome.
   - Implement this once, in the dispatch path every caller shares, so that task dispatches (planner, developer, every reviewer, gate fix-ups, post-mortem), planning-session turns, bug-fixer dispatches (hunter, repro, consensus) and repository analysis all wait and resume. No caller ever sees a quota result as its dispatch outcome. Specifically: no `attempt.failed`, no retry consumed, no skipped plan, no `unknown` verdict.
3. Budgets. Time spent waiting does not count against `execution.wall_clock_budget_minutes`, agent `timeout_minutes`, or any planning-session limit. Track waited time per task and subtract it in the budget check.
4. Visibility.
   - Events `quota.waiting` {engine, key, kind, window, resets_at, message, agent, node_id} and `quota.resumed` {engine, key, waited_seconds, resumed_session: bool}, on the task (or the planning session).
   - A notification through the existing webhook when a wait starts.
   - The gate state and the latest rate-limit info per key are exposed through REST and an MCP tool: either a new `conclave_get_engine_quota` tool or a block in an existing status tool, your choice; document which.
   - The UI shows a banner while any gate is closed: "<engine> limit reached — resuming at <local time>".
   - `pilot_guide.md` gets a section on limits: what waits, where to see it, how to cancel, and that `auto_merge: false` tasks still stop at the merge step for the pilot.
5. Restart. A daemon restart during a wait must not fail the task. Today startup returns in-progress tasks to `approved` (`repo.recover_in_progress`). That is acceptable: the re-run hits the limit again and waits. Assert it in a test.
6. `allowed_warning` changes nothing except updating the exposed rate-limit info.

TESTS (deterministic; no real CLI; never sleep for real, so the gate takes an injectable clock and sleep)
- Claude Code parsing: a rejected `rate_limit_event` with `resetsAt`; an assistant `error: "rate_limit"` with the reset taken from an earlier `rate_limit_info`; `billing_error`; the legacy `usage limit reached|<epoch>` text; a normal stream with `allowed` and `allowed_warning` events, which stays ok with no quota; a result that has exit code 0 and no `is_error` but a rejected event, which is NOT ok.
- opencode parsing: the 402 event above; a 429 with `retry-after`, and one with `retry-after-ms`; a 429 without headers; the transient retry loop does not retry a quota error.
- Gate: closing until reset plus margin; the no-reset fallback using `quota_retry_minutes`; two concurrent waiters released together; keys isolated (a Claude Code wait does not block opencode, and DeepSeek does not block another opencode provider); cancel during a wait.
- Orchestrator integration with the fake provider scripted to return a quota result once (then success), for each of: planner, developer, one of several parallel reviewers, gate fix-up. The task finishes normally. No attempt is consumed, the plan is used, the reviewer's real verdict counts, both events are emitted, and the resumed dispatch carries the reported session id. Also: the wall-clock budget is not consumed by the wait; a cancel during the wait cancels the task; a planning-session turn that hits the quota waits and completes; a bug-fixer dispatch that hits the quota waits.
- Restart: an in-progress task during a wait is recovered to `approved` and completes after the gate reopens.

CONSTRAINTS
- Never weaken or skip an existing test. Never call the live daemon or its MCP tools; never run the real `claude` or `opencode` CLI in tests.
- Quality gate (must stay green): `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit && npm run build` (rebuild and commit `src/conclave/web/static/`).
