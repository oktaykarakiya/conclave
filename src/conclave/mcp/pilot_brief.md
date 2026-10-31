# Conclave Pilot Brief

Conclave runs a team of AI agents on your attached git repositories: it plans, develops, runs a
multi-agent review, executes the project's test gate, commits, and merges — one task at a time in
an isolated worktree. You are the pilot: you choose the work, the agents, and what lands. Read the
full guide once per session before acting.

**Golden rules**
- Never let work land unreviewed: prefer `auto_merge: false`, read every change with
  `conclave_get_task_diff`, then `conclave_merge_task` (or arm a checkpoint before the merge).
- A project without a test command has no gate: always set `execution.baseline_test_command` and
  verify it passes on the target branch first.
- Specs decide quality: precise, testable requests beat clever models.
- Verify, don't assume: check each `agent.result`'s `model_reported` against the model you
  configured, and check the merged result yourself when it matters.
- One change at a time per area: tasks touching the same files should run and merge sequentially.
- Measure model choices on your own tasks, not by reputation.

**Full guide**
- Resource: `conclave://guide/pilot` (markdown).
- Tool: `conclave_get_pilot_guide` — omit `section` for everything, or pass a section number
  (`"6"`) or a title substring (`"monitoring"`) for just that section. Each call ends with a
  generated live-environment footer (version, default engine, engines on this host, tool count).
