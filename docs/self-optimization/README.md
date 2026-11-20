# Self-optimization log

Conclave improving its own repository, driven by a pilot over MCP (see
`src/conclave/mcp/pilot_guide.md`). Work lands on branch `self-optimize`, is reviewed with
`conclave_get_task_diff`, merged with `conclave_merge_task`, and promoted to `master` only when the
full gate is green.

## Decided configuration (owner decisions)

- Planning nodes (`planner`, `architect-as-planner`, `pm`, `test-architect`) and the `developer`:
  the strongest reasoning model at its highest effort (Opus 5.5, `max`, since 2026-11-01), with a
  "research first, cite sources" planning persona. The first bake-off (`RESULTS.md`, run 1) found
  a strong cheap developer (DeepSeek V4.1 Flash) as correct on well-specified small tasks; for this
  repository the owner chose the strongest model for every change.
- Review panel: `reviewer` + `security` on a different model family from the cheap seats (Opus 5.5,
  high); `tester` and conditional reviewers on V4.1 Flash (max). Background nodes on V4.1 Flash.
- Never use DeepSeek V4 Pro (superseded by V4.1 Flash).

## Done

- Run 1 (2026-10-31): R1 `conclave_get_task_diff`, R2 `conclave_merge_task`, R3 commit failure fails
  the task, T1 pilot guide over MCP (`specs/r1..r3`, `specs/t1`).
- Run 2 (2026-11-01/02), from a downstream Windows install's feedback (`RESULTS.md`, run 2):
  - W1 platform-neutral tests and a `windows-latest` CI leg (`specs/w1_windows_ci.md`);
  - W2 no silent prompt caps: `context.*` settings, compaction opt-in and only for recoverable bulk,
    every cut marked and evented (`specs/w2_no_silent_prompt_caps.md`, supersedes `q1`);
  - W3 gate integrity: no silent green without a test command, test-integrity guard
    `block | warn | off` (`specs/w3_gate_integrity.md`);
  - W4 settings layering: built-in -> global -> project delta, live global layer, delta storage,
    one-time normalisation migration (`specs/w4_settings_layering.md`);
  - W5 quality-first built-in defaults: `auto_merge` off, auto effort per engine with clamping,
    planner always with research-first personas, hot reload off, `/api/models` from config
    (`specs/w5_quality_first_defaults.md`);
  - W6 workflow evolution suggest-only by default, versioned history, revert, proposals
    (`specs/w6_workflow_evolution.md`);
  - W7 cancellation stops retries immediately; attaching an attached path no longer modifies it
    (`specs/w7_cancel_and_attach.md`).
- Run 3 (2026-11-17/18), Claude only, with the usage limit as a live test (`RESULTS.md`, run 3):
  - W8 Claude Code worker isolation: no MCP servers, user settings, plugins, skills, auto-memory
    or `CLAUDE.md` loading in worker dispatches (`specs/w8_claude_worker_isolation.md`);
  - W9 usage limits: a refusal waits for the reset and resumes the same session, never fails
    work or skips a reviewer (`specs/w9_quota_wait_and_resume.md`);
  - W17 Claude Code worker tool scope: no messaging other sessions, scheduling, notifications,
    workflows or leaving the worktree; no background wake-ups; full usage
    (`specs/w17_claude_worker_tool_scope.md`);
  - W10 opencode worker isolation: no MCP servers, no external skills, `extra_env` honoured
    (`specs/w10_opencode_worker_isolation.md`);
  - W11 each project rule file once, per engine (`specs/w11_rules_once_per_engine.md`);
  - W12 mandatory reviewers must answer, failed planners retried and visible, the committed SPA
    out of the review diff (`specs/w12_review_and_plan_completeness.md`);
  - W13 engine backlog: merge race, bounded baseline, no-test-command paths, linear focus scan,
    repro-gate cancel, resumed-session cost counted once, failure reasons everywhere
    (`specs/w13_engine_backlog_fixes.md`);
  - W14 MCP stdio bridge never opens the database, hot-reload-safe enum comparisons, response
    shapes in the descriptions, `GET /mcp` 405 (`specs/w14_mcp_stdio_and_enum_safety.md`);
  - W15 MCP parity and compact answers (`specs/w15_mcp_parity.md`, completes `r4`);
  - W16 interaction-log retention and an honest export
    (`specs/w16_interaction_log_retention.md`);
  - W18 the evolver respects a rejection and learns only from quality failures, plus the W12/W13
    review follow-ups (`specs/w18_evolver_respects_rejections.md`);
  - audits A1–A3: Conclave audited its providers, engine and MCP/web/database layers and fixed the
    37 defects it could demonstrate, each with a test that fails on the old code
    (`specs/a1_audit_providers.md`, `specs/a2_audit_engine.md`, `specs/a3_audit_mcp_web_db.md`);
  - W19 the audits' follow-ups: isolation no settings file can undo, reviewers that cannot disturb
    each other unseen, retry bounds that bind, operator SQL that cannot stall the daemon or read
    secrets (`specs/w19_audit_followups.md`).
- Run 3, part 3 (2026-11-20), the backlog's leftovers (`RESULTS.md`, run 3 part 3):
  - W20 reviewers told which failures predate the task; unknown ids answer 404 on REST; a cancel
    that cannot overwrite an approval; operator overrides scoped to their attempt; no double claim
    while a wait is announced; one lock for every workflow-graph writer
    (`specs/w20_backlog_leftovers.md`);
  - W21 the test suite passes on a loaded host: every wait for background work polls for its
    condition (`specs/w21_deterministic_waits.md`);
  - W22 no operator action writes `approved` for a task whose run is live
    (`specs/w22_live_run_never_approved.md`);
  - W23 a complete event stream: replay after a reconnect, gaps announced, the SPA handles every
    event; the SPA's first unit tests (`specs/w23_event_stream_completeness.md`).

## Remaining work, in order (one task at a time, pilot review before each merge)

1. The two open items in `backlog.md`, each waiting on an outside resource: a persona-first
   prompt-order A/B (needs a funded DeepSeek account) and staggered parallel reviewers (needs a
   usage measurement first).

## Run 3 (2026-11-17/18): worker isolation, usage limits, review completeness, backlog

Owner decisions: Claude only (every seat `claude-code/claude-opus-5-5`, project-scoped; the DeepSeek
balance is empty, HTTP 402); fix every flaw found, including new ones, with complete tests; a usage
limit must pause the work and continue seamlessly after the reset.

Waves (one task at a time; the pilot reads every diff, runs the hidden tests in `hidden-tests/`,
then merges; deploy between waves when the daemon's other project is idle):

| Wave | Task | Why |
|---|---|---|
| A | W8 Claude Code worker isolation (`specs/w8_claude_worker_isolation.md`) | workers loaded the operator's MCP servers (including Conclave's own), plugins, skills and auto-memory under `--dangerously-skip-permissions` |
| A | W9 usage-limit wait and resume (`specs/w9_quota_wait_and_resume.md`) | a limit hit failed the whole queue, skipped the plan and let a partial review panel pass |
| A | W10 opencode worker isolation (`specs/w10_opencode_worker_isolation.md`) | Playwright MCP and synced skills in every request; random duplicate skills broke the prompt cache; `extra_env` ignored |
| B | W11 rule files once per engine (`specs/w11_rules_once_per_engine.md`) | `AGENTS.md` sent twice per dispatch |
| B | W12 review and plan completeness (`specs/w12_review_and_plan_completeness.md`) | a mandatory reviewer without a verdict did not block (hidden test: a task whose security reviewer never answered, or crashed, ended `done`); a failed planner was skipped silently; the committed SPA bundle filled the review diff |
| A | W17 Claude Code worker tool scope (`specs/w17_claude_worker_tool_scope.md`) | found live during W9: isolated workers still had `SendMessage`/`ListAgents` (reach other Claude sessions on the host, the pilot's included), `CronCreate`/`RemoteTrigger`, `PushNotification`, `Workflow`, `EnterWorktree`; a background `pytest` woke the planner for a second, repeated turn; usage kept only the last `result` (1 turn instead of 103) |
| C | W13 engine backlog fixes (`specs/w13_engine_backlog_fixes.md`) | backlog 5–9; reproduced on master by the hidden tests: two concurrent merges both returned 200, a stored baseline row of 840,000 chars, a test command removed mid-task still ended `done`, a quadratic focus-marker scan (4k markers 2.3 s); found live: a resumed Claude Code session's cost counted twice (13.01 recorded for a planner that cost 8.49); found on W12's branch: a failed attempt and a failed task never said why (`attempt.failed` without `reason`, summary "failed (max_retries)") |
| C | W14 MCP stdio safety, enum identity, response shapes (`specs/w14_mcp_stdio_and_enum_safety.md`) | backlog 2–4: the stdio fallback mutated the live DB without the daemon lock; found live: `GET /mcp` answered 200 JSON, so the pilot's own MCP client re-polled it every second (392,577 requests in two days) |
| C | W15 MCP parity (`specs/w15_mcp_parity.md`) | rest of r4 |
| C | W18 the evolver respects a rejection and learns only from quality failures, plus review follow-ups (`specs/w18_evolver_respects_rejections.md`) | found live: the same "make risk optional" proposal came back after every clean task (ten times) although the owner rejected each one; found in W13's review: a removed test command or an infra gate failure read as a quality failure, a resumed run whose total restarted charged 0, operator rejections without a reason |
| C | W16 interaction-log retention and honest export (`specs/w16_interaction_log_retention.md`) | 38.5 MB never pruned; a JSONL file named `.parquet` rewritten after every dispatch |
| D | audits A1 providers, A2 engine, A3 MCP/web/database (`specs/a1..a3`): Conclave hunts for defects, fixes each one it can demonstrate with a failing test | "all flaws fixed, even newly discovered ones" |
| D | W19 audit follow-ups (`specs/w19_audit_followups.md`) | found while piloting the audits: parallel reviewers swapped files in the shared worktree under another reviewer's test run; a repository settings file could undo worker isolation; `review_rounds_max` and `max_iterations` bound nothing; plus A1–A3's leftovers |
| E | W20 backlog leftovers (`specs/w20_backlog_leftovers.md`) | backlog 3–7 and 9, each demonstrated first: reviewers blamed pre-existing failures on the diff; `DELETE /api/projects/<unknown>` answered 200 and emitted `project.deleted`; a cancel could overwrite an approval; an override could apply to a later attempt; an approve could let a parallel tick claim a running task; REST and the evolver could undo MCP graph edits |
| E | W21 deterministic waits (`specs/w21_deterministic_waits.md`) | found live: with another project compiling on the host, a planning test failed on every run, on master too (a fixed 0.1 s sleep before asserting background work) |
| E | W22 live runs never approved (`specs/w22_live_run_never_approved.md`) | found by W20: completing another node, or an approval between two waits, wrote `approved` for a live run |
| E | W23 event stream completeness (`specs/w23_event_stream_completeness.md`) | backlog's WebSocket item, checked: the SPA handled only the last event of a burst, a reconnect lost what was emitted meanwhile, an overflow went unannounced |

Deferred with reasons: persona-first prompt order (it only helps prefix-cached engines; Claude Code
sends the prompt as one block, so there is nothing to gain there, and a DeepSeek A/B needs a funded
account); staggered parallel reviewers (needs a measurement first).

Usage-limit evidence before W9 (`e2e/`: a scratch daemon on its own home and port, a fake `claude`
emitting Claude Code 2.1.326's measured `rate_limit_event`/`error: "rate_limit"` stream): one refusal
burned all three developer attempts within the same second, the post-mortem hit the same limit, the
task failed with `max_retries`, and the workflow evolver proposed growing the review panel over what
was only a usage limit.

Usage limits hit for real during the run (Claude Code 2.1.326, the five-hour window), each one waited
out by the gate and resumed in the same session (`quota.waiting` → `quota.resumed`,
`resumed_session: true`), no attempt consumed, nothing failed:
- W17's planner, 2026-11-17 12:56 UTC: waited 4,492 s, resumed and finished its plan.
- W12's planner, 18:28 UTC: waited 2,558 s, resumed, plan in 8 more minutes.
- W12's `risk` reviewer, 20:05 UTC: waited 14,728 s (overnight), resumed at 00:11, rendered its
  verdict; the task committed at 00:15. The laptop was rebooted at 00:35: the systemd user service
  brought the daemon back at boot with nothing in flight and nothing lost.
- A2's planner, 2026-11-18 16:21 UTC: waited 3,577 s, resumed and finished its plan.
- W19's developer, 21:32 UTC: waited 2,894 s, resumed and finished; the task passed on its first
  attempt.

Efficiency (owner, 2026-11-18: "do whatever is most efficient, you are the pilot"): after 13 tasks
the usage was 34% developer, 32% planner, 13% tester and 22% for the seven other reviewers, which
had not failed a change. Architect, risk, legal, devops and performance moved from `xhigh` to
`high` (risk stays mandatory); the planner ran at `high` for W19, whose spec the pilot had verified,
and is back at `max` for audits. The five-hour window then lasted about four hours of this work
instead of about two.
