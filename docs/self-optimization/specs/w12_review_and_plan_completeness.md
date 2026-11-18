# W12 Mandatory reviewers must answer, failed planners retried and visible, generated output kept out of review diffs

No task passes review with a mandatory reviewer that never rendered a verdict, and a failed planner is retried and visible, never silently skipped.

CONTEXT (engine/orchestrator.py)
- `_dispatch_reviewer` retries only an EMPTY response. Any non-empty text without a verdict block is returned at once, and `_record_reviewer_verdict` turns a response without a verdict into a non-blocking `unknown`.
- The ENG-3 guard in `_review` refuses only a round where NO reviewer produced a usable verdict. So a round where, for example, the `security` reviewer crashed, timed out, or answered without its verdict block passes on the other reviewers' votes, and the change merges without a security review.
- `config.agents.mandatory` (default `["tester", "security", "reviewer"]`) names the reviewers that must always run.
- `_plan`: when the planner dispatch is not `ok`, or its output has no plan block, the developer proceeds without a plan and no event says so. The planner node is optional in the workflow graph, but this repository's defaults are quality-first (`README` of `docs/self-optimization`: planner always).
- Usage-limit and balance exhaustion is handled separately (dispatches wait and resume; see the quota gate). This task is about every other failure: crash, timeout, CLI error, missing or unparseable verdict.

REQUIRED BEHAVIOUR
1. Mandatory reviewers must render a usable verdict.
   - When a reviewer named in `config.agents.mandatory` (the workflow nodes whose agent is mandatory) returns no usable verdict, re-run that reviewer alone, up to 2 more times. The re-run prompt appends a short note: "Your previous reply had no verdict block; end with exactly the required JSON verdict block."
   - If it still has no usable verdict, the review round fails like ENG-3 does: the attempt fails with a reason naming the reviewer and its last error, and the retry loop re-runs it. The task is never reported green, and never merged or offered for merge, while a mandatory reviewer's verdict is `unknown`.
   - Optional/conditional reviewers keep today's behaviour (`unknown` is non-blocking), but each one emits a visible warning event naming the reviewer.
   - A reviewer that explicitly declines or abstains in its verdict block is a usable verdict and keeps today's semantics.
2. Planner failures.
   - When the planner dispatch fails, or its reply has no parsable plan block, retry it up to `resolve_agent(config, "planner").max_retries` times in total, with the same backoff the reviewers use.
   - If every try fails, proceed without a plan (the node is optional), but emit a `log` event with stage `planner_failed` (attempts and last error) and append "planned: no (planner failed: <reason>)" to the task's result summary.
   - When the workflow has no enabled planner node, or `experimental.planner_enabled` is false, nothing changes.
3. Cancellation: none of these retries runs after cancellation, and each checks the cancel event before dispatching.
4. Generated output must not crowd out the real diff. Measured on task W8: the committed SPA rebuild (`src/conclave/web/static/`, minified bundles) made the review diff 792,173 characters, so every reviewer received a head/tail cut of 164,719 characters. Because files are ordered alphabetically, the cut fell between the source changes and the tests.
   - Add a `.gitattributes` that marks `src/conclave/web/static/**` as `-diff linguist-generated=true`, so `git diff` prints one `Binary files ... differ` line per bundle file.
   - Check that every diff Conclave puts into an agent prompt (`git diff` calls in `engine/orchestrator.py` and anywhere else) honours it, which means no `--text`, `-a` or `--binary`.
   - Check that nothing that applies or replays diffs depends on the bundle's text.
   - Test: in a temporary repository with such an attribute, a reviewer prompt shows the generated file only as the `Binary files` line while a normal file's hunk is intact.
   - Pilot-guide note: repositories that commit build output should mark it `-diff` to keep reviewers' context on the real change.
5. Update `src/conclave/mcp/pilot_guide.md` (review semantics: mandatory reviewers block until they answer; how a failed planner shows up).

TESTS (deterministic, fake provider)
- Mandatory reviewer returns non-empty text without a verdict once, then a pass: the round passes, and the second prompt carries the reminder.
- Mandatory reviewer never returns a verdict: the round fails with a reason naming it, and the task does not reach a green/merge-ready state within the attempts.
- Mandatory reviewer crashes (`ok=False`, an error, no text) every time: same as the previous case.
- Optional reviewer without a verdict: non-blocking, the warning event is emitted, and the round passes on the others.
- Planner fails twice then succeeds: the plan is used (`plan.artifact` emitted). Planner always fails: the developer runs without a plan, the `planner_failed` event is emitted, and the result summary says so.
- Cancel during a reviewer re-run or a planner retry: the task cancels and no further dispatch happens.
- The existing ENG-3, ENG-4 tests keep passing unchanged.

CONSTRAINTS
- Never weaken or skip an existing test. Never call the live daemon or its MCP tools.
- Quality gate (must stay green): `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit && npm run build`.
