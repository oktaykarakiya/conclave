# Conclave Pilot Guide

You are a **pilot**: a model (or person) operating Conclave through its MCP tools. Conclave runs a
team of AI agents on a git repository: plan → develop → multi-agent review → test gate → commit →
merge. Your job is to give it good work, pick the right agents, watch every run, and decide what
lands. This guide is model-agnostic usage knowledge; read it once per session before acting.

## 1. Mental model

- **Project** = one attached git repo (`path`) plus its config. Work happens in isolated git
  worktrees under Conclave's data dir; the repo's own checkout is only touched on merge.
- **Task** = one unit of work, run by the project's worker. States: `inbox` (needs approval) →
  `approved` → `in_progress` → `waiting_for_approval` (paused at a human checkpoint) → `done` /
  `failed` / `cancelled` / `blocked` (a parent failed). A worker runs
  `execution.max_parallel_tasks` tasks at once (default 1, i.e. strictly in order).
- **Agents** are roles with personas: `planner` (and planning specialists `architect-as-planner`,
  `pm`, `test-architect`), `developer`, mandatory reviewers `tester`, `security`, `reviewer`,
  conditional reviewers (`architect`, `legal`, `risk`, `performance`, `devops`, triggered by
  what the diff touches), `postmortem`, `repo-analyst`.
- **Workflow** = a graph of nodes (agents, `gate`, `merge`) and edges (`always`, `on_pass`,
  `on_fail`, `on_diff_trigger`). Nodes/edges can pause for a human (`await_human_approval`,
  `pause_after`, `require_human`). `conclave_get_workflow` shows the effective graph.
- **Gate** = the project's `execution.baseline_test_command`, run on a baseline before work and
  again after review. Verdicts from reviewers are **grounded**: claims must cite evidence that
  exists in the diff, or they are downgraded.
- **Branches**: each task gets `<branch_prefix><task_id>` (default `conclave/<id>`) from the
  project's `execution.target_branch`. With `auto_merge` on, a green task merges itself; with it
  off, the task ends `done` on its committed branch and **you** merge it.

## 2. Golden rules

1. **Never let work land unreviewed.** Prefer `auto_merge: false`: read every change with
   `conclave_get_task_diff`, then `conclave_merge_task`. Or arm a human checkpoint before the merge.
2. **A project without a test command has no gate.** Always set `execution.baseline_test_command`
   (and `setup_command` when the repo needs an environment). Verify it passes on the target
   branch first, or every task fails at baseline.
3. **Specs decide quality.** Precise, testable requests beat clever models (see §5).
4. **Verify, don't assume.** Check each `agent.result`'s `model_reported` matches the model you
   configured; check the merged result yourself when it matters.
5. **One change at a time per area.** Tasks that touch the same files should run sequentially and
   be merged before the next one starts, or they will conflict.
6. **Measure model choices** on your own tasks (§8), not by reputation.

## 3. Setting up a project

1. `conclave_attach_project(name, path, default_branch)`. **Attaching a path that is already
   attached updates that project** (it is not a duplicate): to run two configurations against
   one repository, attach separate clones.
2. `conclave_patch_config(project_id, config={...})` deep-merges into the project config
   (`conclave_get_config` shows the effective result; `conclave_get_workflow` the graph). Key
   fields:
   - `execution.target_branch`, `execution.auto_merge`, `execution.setup_command`,
     `execution.baseline_test_command`, `execution.require_full_green`,
     `execution.max_parallel_tasks`, `execution.review_rounds_max`
   - `agent_defaults` (applies to every agent) and `agent_overrides.<agent>`:
     `model`, `effort`, `timeout_minutes`, `max_retries`, `compact_before_start`, `skills`
   - `experimental.planner_enabled`, `experimental.auto_planner_char_threshold`
   Deep-merge cannot delete keys: to "unset" an override, set it explicitly to the default value.
3. Personas: `conclave_list_agents` / `conclave_upsert_agent(name, role, persona_md, project_id)`.
   A persona with `project_id` overrides the global one for that project only.

### Engines and model strings
- `provider/model` (e.g. `deepseek/deepseek-flash`) runs on the **opencode** CLI with that
  provider's credentials.
- `claude-code/<model>` (e.g. `claude-code/claude-opus-5-5`, `claude-code/sonnet`,
  `claude-code/default`) runs on the **Claude Code** CLI with its own login.
- A model without `/` and without the `claude-code/` prefix is ignored by opencode (it uses its
  own default) — always use a full model string.
- `effort` (`low` | `medium` | `high` | `xhigh` | `max`) is passed to the engine; engines and
  models support different subsets (some only `low`/`high`/`max`). Prefer the highest level the
  target model supports for quality-critical roles.
- Only configure models whose engine and credentials actually work on this host; a quick
  low-cost task is the cheapest way to confirm a new model string end to end.

## 4. Choosing models per node (generic strategy)

- **Planning** (rare, shapes everything): the strongest reasoning model available, high effort.
  Instruct planners to read the code and verify current official docs before planning, and to
  cite sources in the plan.
- **Developer** (most tool calls, most tokens): pick by measurement (§8). A strong, cheap model
  is often as correct as the most expensive one on well-specified tasks; reserve the strongest
  for large, cross-cutting or architectural changes.
- **Reviewers**: use a *different model family* from the developer for the most consequential
  seats (`reviewer`, `security`) — same-family reviewers share the developer's blind spots.
  Mechanical seats (`tester`, conditional reviewers) can use a cheaper model.
- **Background** (`postmortem`, summaries, `repo-analyst`): cheapest capable model.

## 5. Writing task requests (specs)

A good request is a contract the reviewers and the gate can check:
- the **problem** and why it matters, in one or two sentences;
- **exact interfaces**: endpoint paths, tool names, argument names/types/defaults, response
  fields, status/error codes, event names;
- **behaviour rules and edge cases**, numbered;
- **constraints**: what must not change, what to reuse instead of duplicating, safety rules
  (e.g. read-only, never bypass hooks);
- **tests to add** and the **quality gate** command that must stay green.
Long requests are fine (they automatically trigger the planner). Set `use_planner: true` to force
planning. Prefer several small dependent tasks (`parent_task_id`) over one giant task; a planning
session (§9) can produce the tree for you.

## 6. Running and monitoring

- `conclave_create_task(project_id, request, title, auto_approve=true, use_planner)` →
  `conclave_get_task(task_id)` (the task is under the `task` key) for state and `result_summary`.
- `conclave_get_task_events(task_id, after_id)` returns events in **pages**: loop, passing the
  last seen `id` as `after_id`, until the page is empty. Useful types:
  `task.started`, `plan.artifact`, `attempt.started`, `agent.dispatched` (configured model),
  `agent.result` (`ok`, `model_reported`, `cost_usd`, `error`), `agent.verdict`
  (`verdict`, `reason`), `pipeline.derived` (which reviewers will run), `node.started`,
  `node.waiting_for_approval`, `hitl.waiting`, `task.committed`, `task.merged`,
  `task.failed` (payload `reason`), `task.done`. Skip `agent.output_chunk` (live output) unless
  debugging.
- `conclave_get_task_verdicts`, `conclave_get_task_checkpoints` summarize the review.
- Typical timeline: setup + baseline test run, planner, developer, each reviewer in turn, the
  gate, commit. Poll every 15–60 s; long silences during planning or development are normal.
- Costs are reported per dispatch in `agent.result.cost_usd`. Subscription-backed engines
  report a notional cost that counts against plan limits, not money.

## 7. Human in the loop

- `conclave_steer_task(task_id, message, node_id?)`: guidance injected into the next dispatch.
- `conclave_set_hitl_checkpoint(task_id, target_id, enabled)`: pause a running task after a node
  (e.g. `gate`) or on an edge; the task goes `waiting_for_approval`.
- `conclave_complete_node(task_id, node_id, status, verdict?, message?)`: resume a paused node
  (`completed`), fail it, or skip it; `message` is passed on as feedback.
- `conclave_cancel_task(task_id)`: stop a task (its worktree is cleaned up).
- `conclave_approve_task` / `conclave_cascade_approve_task` for tasks created without
  `auto_approve` (e.g. a planning session's task tree).

## 8. Reviewing, merging and comparing

- `conclave_get_task_diff(task_id, stat_only?, max_bytes?)`: live worktree diff while a task
  runs, the task branch diff once done. Review it like a pull request: correctness against the
  spec, error handling, tests that actually exercise the behaviour, no deleted or skipped tests,
  no unrelated changes.
- `conclave_merge_task(task_id)`: merges a `done` task's branch into the target through
  Conclave's guarded merge (conflicts leave everything untouched and return an error). If a
  change is not acceptable, do not merge it: create a follow-up task that states exactly what to
  fix, or cancel and rewrite the spec.
- **Comparing models (bake-off)**: give each candidate its own project on its own clone, the
  same base commit, the same spec and the same reviewers; score with acceptance tests the agents
  never see, the gate, review rounds, diff quality, cost and time. Merge the better diff.

## 9. Planning big goals

`conclave_create_planning_session(project_id, prompt, title, max_rounds)` runs a panel of
specialists that debate and build a task tree. Follow it with `conclave_get_planning_session`,
add guidance with `conclave_interject_planning_message`, then `conclave_approve_planning_session`
(creates the tasks) or `conclave_cancel_planning_session`. Review the tree before approving.

## 10. When Conclave works on its own repository

- Develop on a separate branch (not the branch the running daemon is checked out on) and promote
  to the stable branch only when the gate is green.
- The daemon may hot-reload its own code when files in its checkout change; avoid editing that
  checkout while tasks run. `conclave_get_reload_status` / `conclave_reload_system` show and
  control reloads.

## 11. Pitfalls and response shapes

- `conclave_get_task` wraps the task as `{"task": {...}}`; list tools may wrap lists too —
  read the keys before indexing.
- Event lists are paged (§6); a single call is not the full history.
- Tool errors come back with `isError: true` and a text message; they are not exceptions.
- Attaching an already-attached path updates that project (§3).
- A task that fails at **baseline** usually means the test or setup command is wrong for the
  target branch — fix the config, not the code.
- If a model's `model_reported` differs from the configured one, stop and investigate before
  trusting results (wrong engine, wrong model string, or routing problem).
