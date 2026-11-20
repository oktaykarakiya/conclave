# Run 1 (2026-10-31) — developer bake-off: Opus 5.5 (xhigh, Claude Code) vs DeepSeek V4.1 Flash (max, opencode)
Held constant: planner Opus 5.5 xhigh + research persona; reviewer Opus 5.5 high; tester/security/conditional = V4.1 Flash; same base commit; same gate.

## R1 conclave_get_task_diff — winner: FLASH (merged as 16d9dd9)
| | Opus | Flash |
|---|---|---|
| hidden tests | 8/8 | 8/8 |
| gate | green 507 | green 506 |
| attempts / reviewers | 1 / 8 pass | 1 / 8 pass |
| dev time / cost | 8.2 min / $1.59 | 5.8 min / $0.036 |
| review notes | typed pydantic model; git failure -> RuntimeError -> 500; `git add -A` writes blobs | intent-to-add (no object writes); all git failures -> 4xx; hot-reload rank + test; dict payload |
(R1 first run invalidated: router hot-reload bug sent Flash dispatches to Claude; fixed in 062367a.)

## R2 conclave_merge_task — winner: OPUS (merged as fast-forward)
| | Opus | Flash |
|---|---|---|
| hidden tests | 6/6 | 6/6 |
| gate | green 518 | green 516 |
| attempts / reviewers | 1 / all pass | 1 / all pass |
| dev cost | $1.40 | $0.022 |
| review notes | separate 88-line engine/task_merge.py (orchestrator untouched); enum value comparisons (hot-reload safe); reuses _find_task_branch | +113 lines in 2.8k-line orchestrator.py; moved _find_task_branch into orchestrator; `is` enum checks |
Both: target_sha read after the merge lock is released (minor race) — follow-up.

## R3 commit failure — winner: FLASH (narrow; cherry-picked as 99e3e2e)
| | Opus | Flash |
|---|---|---|
| hidden tests | 2/2 | 2/2 |
| gate | green 509 | green 509 |
| attempts / reviewers | 1 / all pass | 1 / all pass |
| dev cost | $0.58 | $0.011 |
| review notes | equivalent logic; logs blocked-descendant count | equivalent logic + failed merge checkpoint (UI), named constant, exit_code in event |
Combined self-optimize (R1+R2+R3): ruff/mypy clean, 521 passed, tsc ok.

## Overall
Correctness: tied 3/3 (every hidden test passed by both, every task passed all reviewers on attempt 1).
Quality edge: R1 Flash (spec strictness), R2 Opus (architecture), R3 Flash (thoroughness, marginal).
Developer cost: Opus $3.57 vs Flash $0.069 total (~52x). Developer time: Flash faster each round.
Caveat: tasks were small-to-medium; large cross-cutting changes not yet measured.

# Run 2 (2026-11-01/02) — downstream feedback queue (W1–W7)

Source: feedback from a Windows install tracking `master` (prompt truncation, settings layering,
Windows test defects), verified against the code before any task was written. One task at a time on
`self-optimize`; the pilot read every diff before `conclave_merge_task`.

Setup: planner Opus 5.5 (W1 xhigh, W2–W7 max); developer V4.1 Flash max for W1, Opus 5.5 max for
W2–W7 (owner decision 2026-11-01); reviewer + security Opus 5.5 high; tester and conditional
reviewers V4.1 Flash max. Costs are as the engines report them: Claude Code reports a notional
cost against the subscription, not money.

| Task | Attempts | Caught on attempt 1 (by) | Tests after | Reported cost (planner / developer / reviewers) |
|---|---|---|---|---|
| W1 Windows tests + CI | 1 | — (planner also found 6 more fixed-count waits) | 533 | 1.29 / 0.02 / 0.65 |
| W2 no silent prompt caps | 2 | baseline test output still cut to 200 lines (architect, Flash) | 577 | 11.26 / 25.66 / 3.27 |
| W3 gate integrity | 2 | `.js` tests and multi-line `it(` slipped through the guard (risk, Flash) | 661 | 9.11 / 30.86 / 3.10 |
| W4 settings layering | 2 | delta storage could move a project's merge branch (reviewer, Opus) | 727 | 8.17 / 20.65 / 3.27 |
| W5 quality-first defaults | 1 | — | 785 | 10.39 / 14.03 / 1.68 |
| W6 workflow evolution | 1 | — | 822 | 8.94 / 14.90 / 1.64 |
| W7 cancel + attach | 1 | — | 836 | 11.00 / 10.17 / 1.22 |

Observations:
- Every second attempt was triggered by a grounded reviewer finding that the gate alone would not
  have caught; two of the three came from the cheap Flash seats, one from the Opus reviewer. Keeping
  a full, mixed-model panel paid for itself.
- Opus planners at max effort cited official docs and repository lines in every plan and found work
  the specs missed (W1: six more flaky waits; W2: `run_tests` trimmed all output at the source).
- No test was weakened: the few removed tests covered deleted code and were replaced, and every new
  skip marker in the tests is test data for the integrity parser.

# Run 3 (2026-11-17/18) — worker isolation, usage limits, review completeness, backlog (W8–W18)

Owner decisions: Claude only (every seat `claude-code/claude-opus-5-5`, project-scoped: planners
and developer `max`, reviewers `xhigh`); fix every flaw found, including new ones, with complete
tests; a usage limit must pause the work and continue seamlessly after the reset; push when done.

Method: the pilot wrote each spec from measurements on this host, then a hidden acceptance test
(`hidden-tests/`) from the spec alone, and checked that it fails on master for the right reason.
After the developer finished, the pilot snapshotted the worktree, ran every hidden test so far and
the full suite on the snapshot, read the diff, checked the committed branch was byte-identical to
the snapshot, and only then called `conclave_merge_task`. Each wave was deployed (SQLite backup,
stop, fast-forward `master`, start) before the next one began, so later waves ran on the fixes of
earlier ones. Costs are the engine's notional subscription cost, as reported.

| Task | Attempts | Caught on attempt 1 (by) | Tests after | Reported cost (planner / developer / reviewers) |
|---|---|---|---|---|
| W8 Claude Code worker isolation | 1 | — | 859 | 4.44 / 6.18 / 11.46 |
| W9 usage-limit wait and resume | 2 | a `rate_limit_event` with `status: rejected` but `isUsingOverage: true` (paid extra usage, request served) was read as a refusal: the paid result thrown away and the gate closed for 4 h (tester) | 942 | 8.88 / 24.53 / 24.55 |
| W17 Claude Code worker tool scope | 1 | — | 966 | 8.49¹ / 5.35 / 6.94 |
| W10 opencode worker isolation | 1 | — | 992 | 5.26 / 4.03 / 9.11 |
| W11 rule files once per engine | 1 | — | 1103 | 5.88 / 7.37 / 7.64 |
| W12 review and plan completeness | 1 | — | 1120 | 12.11 / 9.38 / 9.30 |
| W13 engine backlog fixes | 1 | — | 1243 | 12.49 / 16.10 / 10.86 |
| W14 MCP stdio safety, enum identity | 1 | — | 1252 | 4.98 / 5.92 / 5.92 |
| W15 MCP parity, compact answers | 1 | — | 1259 | 5.80 / 7.45 / 6.73 |
| W16 interaction-log retention, honest export | 1 | — | 1275 (+1 skipped without pyarrow) | 5.30 / 7.87 / 5.76 |
| W18 evolver respects rejections, follow-ups | 1 | — | 1307 (+1 skipped) | 6.57 / 8.72 / 6.77 |

¹ Recorded as 13.01: the double count of a resumed Claude Code session that W13 fixed.

Usage limits, live (Claude Code 2.1.326/2.1.327, the five-hour window): each hit closed the
engine's gate, the dispatch waited for the reset and resumed the same session
(`quota.waiting` → `quota.resumed`, `resumed_session: true`); no attempt was consumed, nothing
failed, and no reviewer was skipped:
- W17's planner, 2026-11-17 12:56 UTC: waited 4,492 s.
- W12's planner, 18:28 UTC: waited 2,558 s.
- W12's `risk` reviewer, 20:05 UTC: waited 14,728 s overnight, verdict at 00:11, commit at 00:15.
- The laptop was rebooted at 00:35: the systemd user service restarted the daemon at boot, with
  nothing in flight and nothing lost.

Before W9 (`e2e/` harness: scratch daemon, a fake `claude` emitting Claude Code's measured
refusal stream), one refusal burned all three developer attempts in the same second, the
post-mortem hit the same limit, the task failed with `max_retries`, and the workflow evolver
proposed growing the review panel over a usage limit.

Flaws found while piloting, beyond the specs, and fixed in the same run:
- isolated workers could still message other Claude sessions on the host (`SendMessage`,
  `ListAgents`), schedule wake-ups, and a background `pytest` woke the planner for a second turn
  (W17);
- a resumed Claude Code session's cost was counted twice (W13);
- a failed attempt or task never said why (W13);
- `GET /mcp` answered 200 JSON, so the pilot's own MCP client re-polled it every second: 392,577
  requests in two days (W14; after deploy, two 405s and the client stops);
- the evolver re-proposed the rejected "make risk optional" change after every clean task (ten
  times, N = 9 to 19), and read a removed test command or an infra gate failure as a quality
  failure (W18; after deploy, a manual evolve computed the same change and returned
  `suppressed_by` with nothing stored);
- MCP responses too large for a pilot's context (a 110k-character `conclave_get_task`, a 20k-token
  echo for a one-line decision) (W15; after deploy, a rejection answers in seven lines).

Observations:
- One second attempt in eleven tasks: the panel caught a real money-and-availability defect that
  every gate and the other seven reviewers passed. Hidden tests found no defect the panel missed.
- Opus planners at `max` read the installed Claude Code bundle and the opencode source to verify
  behaviour before planning (W9: the real refusal stream; W14: which client versions mishandle a
  405 or a 404), instead of trusting the spec.
- The test-integrity guard flagged one removed test (W14): the rewrite of the stdio fallback
  test, intended by the spec; the duplicate-execution guarantees it held were kept.

## Run 3, part 2 (2026-11-18) — audits A1–A3 and their follow-ups (W19)

After W18 was pushed, Conclave audited its own providers (A1), engine (A2) and MCP/web/database
layers (A3). Each audit task asked for defects against the repository's own contracts, every one
demonstrated by a test that fails on the old code before it is fixed, plus a SUSPECTED list (could
not demonstrate) and a NOT A DEFECT list (checked and found correct). An empty diff was an
acceptable answer. The planners did the reproduction themselves (throwaway scripts against HEAD,
deleted afterwards), so the plan was already a list of confirmed defects.

| Task | Attempts | Defects fixed | Tests after | Reported cost (planner / developer / reviewers) |
|---|---|---|---|---|
| A1 providers | 1 | 9 | 1332 (+1 skipped) | 10.05 / 7.14 / 7.50 |
| A2 engine | 1 | 13 | 1374 (+1 skipped) | 10.01 / 12.10 / 8.07 |
| A3 MCP, web, database | 1 | 15 | 1413 (+1 skipped) | 11.55 / 11.76 / 7.57 |
| W19 audit follow-ups | 1 | 8 items | 1448 (+1 skipped) | 2.19² / 17.17 / 8.90 |

The most serious defects found:
- A1: a crashed or error-ended Claude Code run kept its raw transcript as the answer and could be
  ok, so a crashed reviewer whose transcript contained "VERDICT: pass" counted as a pass.
  Cancelling a dispatch left the CLI running; a large prompt to a CLI that exited early crashed
  the dispatch; the process group was looked up after its leader was reaped; UTF-8 characters
  split across reads and JSON lines split on U+2028 were garbled; the usage of opencode attempts
  that were retried was dropped.
- A2: a failed `git worktree add` during a merge deleted the committed, reviewed branch and
  answered 500; a workflow with a developer but no merge node deleted the work and still ended
  `done`; a target branch that moved during a fast-forward lost the commit that moved it; a target
  checked out in a linked worktree was moved underneath it; the first verdict block in a reply
  decided instead of the last; quarantine exclusions were appended to whatever command came last
  (`pytest && mypy` passed them to mypy); a cancel did not stop a running setup, baseline or gate
  command; one raising parallel reviewer left the others running unawaited; a retry's history cut
  the previous diff to 1,500 characters without a marker.
- A3: a cancel landing on a transaction's `BEGIN` left the shared connection inside a transaction nobody owned, so every later transaction failed; two concurrent config writes (project or global) both answered 200 and kept only the last one's change, and the same for two MCP workflow edits; two requests with one `Idempotency-Key`, or two attaches of one path, both ran; an invalid agent override was stored and then failed every dispatch of that agent; a cancel could overwrite a task a worker had just claimed; huge or non-finite numbers, an unknown project or a dead parent task reached SQLite or waited forever instead of a 4xx; the stdio bridge died on a reply that was not JSON.

Found while piloting the audits, fixed in W19:
- parallel reviewers share one worktree: in A1, two reviewers swapped the old provider files in to
  prove the new tests fail on them while a third ran the full suite, which showed 14 unexplained
  failures (the commit was safe: the reviewed tree is restored before the gate);
- an isolated worker's isolation variables could be overridden by an `env` block in the
  repository's `.claude/settings(.local).json` (Claude Code applies it over the inherited
  environment; `--settings` outranks it);
- `execution.review_rounds_max` and an `on_fail` edge's `max_iterations` were accepted, shown and
  raised by the evolver, but never bounded anything;
- since A2, a retry prompt sent the newest rejected diff twice (A2's performance reviewer).

Efficiency: after A1, the run's usage was 34% developer, 32% planner, 13% tester, and 22% for the
other seven reviewers, which had not failed a change in 13 tasks. The owner delegated the trade-off
to the pilot; five reviewers (architect, risk, legal, devops, performance) went from `xhigh` to
`high`, and W19's planner ran at `high` on a fully verified spec. Risk stayed mandatory. In A2,
right after the change, the risk reviewer cost 0.70 against 1.35 in A1 and still verified the gate.

Usage limits, live: A2's planner hit the five-hour limit at 16:21 UTC, waited 3,577 s and resumed
the same session at 17:21; W19's developer hit it at 21:32, waited 2,894 s and resumed at 22:21
(`resumed_session: true` both times); nothing failed. With the reviewers at `high`, the five-hour
window lasted about four hours of this work instead of about two.

Verified live after deploy: a query on `secrets` answers 400; the real Claude Code CLI accepts the
isolated invocation with its inline `--settings` (one Haiku call answered normally).

² W19's planner at `high` on a spec the pilot had verified line by line (A1–A3's planners ran at
`max`: 10–11.55). Run 3 in all: 15 tasks, 406.68 reported.

## Run 3, part 3 (2026-11-20) — the backlog's leftovers (W20–W23)

After the audits the backlog held nine open items. Two wait on outside resources and stay open: a
persona-first prompt-order A/B needs a funded DeepSeek account, and staggered parallel reviewers
need a usage measurement first. The rest were fixed in four tasks, each item first demonstrated by
a test that fails on the old code. W20 ran with the planner (two of its items were only suspected
and had to be reproduced first). W21–W23 ran without it, because the pilot had checked against the
code everything their specs named. Every task passed on its first attempt.

| Task | Attempts | Fixed | Tests after | Reported cost (planner / developer / reviewers) |
|---|---|---|---|---|
| W20 backlog leftovers | 1 | 6 items | 1471 (+1 skipped) | 9.58 / 11.74 / 9.23 |
| W21 deterministic waits | 1 | every fixed wait in 18 test files | 1476 (+1 skipped) | – / 14.20 / 7.57 |
| W22 live runs never approved | 1 | 1 | 1488 (+1 skipped) | – / 6.28 / 4.50 |
| W23 event stream completeness | 1 | 3 | 1520 (+1 skipped), 15 SPA | – / 10.74 / 7.07 |

What changed:
- W20 fixed six items. W20's planner reproduced all six on HEAD before planning, including the
  two that were only suspected.
  - Reviewers now get the baseline's pre-existing failures, so they no longer blame those on the
    diff.
  - An unknown id answers 404 on REST, where MCP already answered `isError`: tasks, quarantine,
    events and verdicts, project delete, pause and resume. A project delete for an unknown id had
    emitted `project.deleted` for a project that never existed. The SPA now treats a 404 on
    detach as already detached.
  - The cancel fallback for a task waiting for approval is now conditional, so it cannot
    overwrite an approval.
  - An operator's node completion applies only to the attempt it was posted for. One posted for
    an earlier attempt is dropped with a `log` event.
  - The approval waiter is registered before the task reads `waiting_for_approval`, so an
    approval landing meanwhile cannot let a parallel tick claim the running task.
  - Every writer of a workflow graph takes one lock: MCP edits, REST `PUT`, the evolver,
    templates, reverts and config writes.
- W21 makes the suite pass on a loaded host.
  - Found live: while another project compiled on the host (load average about 40), a planning
    test failed on every run, on master too. It slept a fixed 0.1 s, then asserted that background
    work had happened. The full suite took 24 minutes instead of 6.
  - Every wait for background work now polls for its condition through one helper,
    `tests/waits.py`. Its 60 s deadline only bounds a failure.
  - The complexity check compares two input sizes on CPU time instead of an absolute time.
  - The pilot's check: with pytest and two busy loops pinned to one core, master's test failed
    2 runs of 3, while W21 passed 3 of 3 and its full suite passed (1,475 passed in 26 minutes).
- W22: found by W20. Completing a node other than the one a live run waits at, or an approval
  landing between two waits, fell back to writing `approved` for a running task. While the run
  is live, the four fallbacks now signal its waits instead (`release_or_approve`). A task whose
  run is gone (after a daemon restart) is still set `approved`, so a worker resumes it.
- W23: the backlog's WebSocket item, checked, was a real defect with two more behind it:
  - the SPA reacted only to the last event of a burst (an effect over React state holding the
    latest event);
  - a reconnect lost what was emitted meanwhile;
  - the bus's overflow flag was never sent.

  The fixes:
  - `/ws/stream` and SSE `/api/events/stream` replay the persisted events after `after_id`,
    capped at 1,000. SSE also takes `Last-Event-ID` and sends `id:` lines.
  - A `stream.gap` frame marks lost events.
  - The SPA handles each event through stream listeners and refetches on a gap.
  - The SPA has its first unit tests: vitest in the node environment, on a pure module.

No usage limit was hit in this part. Run 3 in all: 19 tasks, 487.58 reported.
