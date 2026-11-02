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

## Remaining work, in order (one task at a time, pilot review before each merge)

1. **MCP parity** — the rest of `specs/r4_mcp_parity.md`: config schema, models/engines, usage,
   HITL state, quarantine integrity, needs-human (the attach fix landed as W7).
2. **Log retention** for interaction logs (settable).
3. Small fixes from `backlog.md`.
