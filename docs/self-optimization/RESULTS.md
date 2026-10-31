# Developer bake-off: Opus 5.5 (xhigh, Claude Code) vs DeepSeek V4.1 Flash (max, opencode)
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
