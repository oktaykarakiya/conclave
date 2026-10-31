# Self-optimization log

Conclave improving its own repository, driven by a pilot over MCP (see
`src/conclave/mcp/pilot_guide.md`). Work lands on branch `self-optimize`, is reviewed with
`conclave_get_task_diff`, merged with `conclave_merge_task`, and promoted to `master` only when the
full gate is green.

## Decided configuration (owner decisions, 2026-10-31)

- Planning nodes (`planner`, `architect-as-planner`, `pm`, `test-architect`): strongest reasoning
  model at xhigh effort, with a "research first, cite sources" persona.
- Developer: DeepSeek V4.1 Flash (max) by default; switch to the strongest model for large or
  cross-cutting tasks. Chosen by the bake-off in `RESULTS.md`.
- Review panel: `reviewer` + `security` on a different model family from the developer (high);
  `tester` and conditional reviewers on V4.1 Flash (max). Background nodes on V4.1 Flash.
- Never use DeepSeek V4 Pro (superseded by V4.1 Flash).

## Remaining work, in order (one task at a time, pilot review before each merge)

1. **MCP parity** — `specs/r4_mcp_parity.md` (global config, config schema, models/engines,
   usage, HITL state, quarantine integrity, needs-human; attach must not rename an existing
   project).
2. **No silent prompt caps** — `specs/q1_no_silent_caps.md` (large/cross-cutting: strongest
   developer).
3. **Quality-first built-in defaults** (all settable in Settings and over MCP):
   - `execution.auto_merge` default **false**;
   - effort default **"highest sensible" per engine** (DeepSeek `max`, Claude `xhigh`), with
     engine-aware clamping so an unsupported level never fails a dispatch;
   - planner always runs (`auto_planner_char_threshold` default 0) and the research-first planner
     persona becomes the built-in default;
   - a project without `baseline_test_command` must not pass the gate silently (require an
     explicit choice).
4. **Workflow evolution suggest-only** (large, has a migration): setting
   `off | suggest | auto`, default `suggest`; every workflow change versioned with reason; list /
   revert / approve-proposal over REST and MCP.
5. **Hot reload off by default** (daemon setting in global config; manual reload stays).
6. **Test-integrity guard** in the normal gate (`block | warn | off`, default `block`).
7. **Log retention** for interaction logs (settable).
8. Small fixes from `backlog.md` (cancellation retries, merge target_sha race, etc.).

The global defaults on the owner's instance already apply most of these choices through
configuration; items 3–7 make them the built-in defaults for every installation.
