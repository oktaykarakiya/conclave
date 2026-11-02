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
