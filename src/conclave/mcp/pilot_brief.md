# Conclave Pilot Brief

Conclave runs a team of AI agents on your attached git repositories: it plans, develops, runs a
multi-agent review, executes the project's test gate, commits, and merges — one task at a time in
an isolated worktree. You are the pilot: you choose the work, the agents, and what lands. Read the
full guide once per session before acting.

**Golden rules**
- Never let work land unreviewed: `auto_merge` is off by default, so a green task waits on its
  branch. Read every change with `conclave_get_task_diff`, then `conclave_merge_task` (if you turn
  auto-merge on, arm a checkpoint before the merge).
- A project without a test command has no gate, so its tasks fail at once (`no_test_command`):
  always set `execution.baseline_test_command` and verify it passes on the target branch first.
- A change that deletes, removes or skips existing tests is flagged to the reviewers and, by
  default, never auto-merged: read its diff before `conclave_merge_task`.
- Specs decide quality: precise, testable requests beat clever models.
- Verify, don't assume: check each `agent.result`'s `model_reported` against the model you
  configured, and check the merged result yourself when it matters.
- One change at a time per area: tasks touching the same files should run and merge sequentially.
- A usage limit or empty balance fails nothing: dispatches wait for the reset, then resume
  (`quota.waiting` events, `conclave_get_engine_quota`); cancel a task to stop its wait.
- Measure model choices on your own tasks, not by reputation.
- Answers are compact by default: project rows come without their stored config,
  `conclave_get_task` without checkpoint artifacts and verdict evidence, workflow decisions
  without graphs. Each tool's description names the `include_*` flag that adds them.

**Full guide**
- Resource: `conclave://guide/pilot` (markdown).
- Tool: `conclave_get_pilot_guide` — omit `section` for everything, or pass a section number
  (`"6"`) or a title substring (`"monitoring"`) for just that section. Each call ends with a
  generated live-environment footer (version, default engine, engines on this host, tool count).
