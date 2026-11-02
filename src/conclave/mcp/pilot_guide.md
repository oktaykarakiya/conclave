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
- **Workflow evolution**: after each task the evolver works out how the workflow should change
  (grow it after a failure or grounded catches; make quiet non-core reviewers optional after 3
  clean tasks). `experimental.workflow_evolution` decides what happens: `suggest` (default)
  stores a **proposal** that changes nothing until you approve it, `auto` applies the change,
  `off` skips the post-task evolution. Every change to a project's workflow, whoever makes it,
  is recorded as a **version** with its source and reason, and can be reverted (§8).
- **Gate** = the project's `execution.baseline_test_command`, run on a baseline before work and
  again after review. Verdicts from reviewers are **grounded**: claims must cite evidence that
  exists in the diff, or they are downgraded.
- **Branches**: each task gets `<branch_prefix><task_id>` (default `conclave/<id>`) from the
  project's `execution.target_branch`. `auto_merge` is off by default: a green task ends `done`
  on its committed branch and **you** merge it. With `auto_merge` on, a green task merges itself.

## 2. Golden rules

1. **Never let work land unreviewed.** `auto_merge` is off by default; keep it off: read every
   change with `conclave_get_task_diff`, then `conclave_merge_task`. If you turn it on, arm a human
   checkpoint before the merge.
2. **A project without a test command has no gate, so its tasks do not run.** Always set
   `execution.baseline_test_command` (and `setup_command` when the repo needs an environment).
   Without one, every task fails at once with reason `no_test_command`, before any agent runs
   (§3). Verify the command passes on the target branch first, or every task fails at baseline.
3. **Specs decide quality.** Precise, testable requests beat clever models (see §5).
4. **Verify, don't assume.** Check each `agent.result`'s `model_reported` matches the model you
   configured; check the merged result yourself when it matters.
5. **One change at a time per area.** Tasks that touch the same files should run sequentially and
   be merged before the next one starts, or they will conflict.
6. **Measure model choices** on your own tasks (§8), not by reputation.

## 3. Setting up a project

1. `conclave_attach_project(name, path, default_branch)`. **Attaching a path that is already
   attached updates that project** (it is not a duplicate): to run two configurations against
   one repository, attach separate clones. The project stores only what you choose here (its
   branch, `config_preset`, `workflow_template`, `max_parallel_tasks`); everything else is
   inherited from the global settings (see "Settings layers" below).
2. `conclave_patch_config(project_id, config={...})` merges into the project's settings
   (`conclave_get_config` shows the effective result; `conclave_get_workflow` the graph). Key
   fields:
   - `execution.target_branch`, `execution.auto_merge` (default `false`: a green task ends
     `done` on its branch, unmerged), `execution.setup_command`,
     `execution.baseline_test_command`, `execution.require_full_green`,
     `execution.max_parallel_tasks`, `execution.review_rounds_max`
   - `execution.allow_no_test_command` (default `false`): with `require_full_green` on and no test
     command, a task with a developer fails with reason `no_test_command` before any worktree or
     agent is spent; its summary names the fix. Set it to `true` only to run deliberately without
     a gate (the gate then passes, and a `gate_skipped_no_tests` log event says it ran no tests).
     Review-only workflows (no developer) and `require_full_green: false` are not affected.
   - `execution.test_integrity` (`block` | `warn` | `off`, default `block`): after each developer
     attempt Conclave checks, from git, whether the change deleted a pre-existing test file,
     removed a test without adding it back elsewhere, or added a skip / xfail / todo / `.only`
     marker to a pre-existing test file (Python and JS/TS). Findings go to every reviewer as a
     `TEST INTEGRITY` section, a `test_integrity.flagged` event and the task's result summary. With
     `block` a flagged change is never auto-merged, even with `auto_merge: true` (§8); `warn`
     only flags; `off` skips the check.
   - `agent_defaults` (applies to every agent) and `agent_overrides.<agent>`:
     `model`, `effort`, `timeout_minutes`, `max_retries`, `compact_before_start`, `skills`
   - `experimental.planner_enabled`, `experimental.auto_planner_char_threshold` (default `0`: the
     planner runs for every task; a task's `use_planner` wins either way)
   - `ui.model_choices` (default `[]`): the models the UI pickers offer. Empty offers a built-in
     list limited to the engines available on this host; models the settings already use (agent
     defaults, overrides, workflow nodes) are always offered. `GET /api/models?project_id=<id>`
     returns that project's list and its `agent_defaults.model` as `default_model`.
   - `system.hot_reload_watch` (default `false`): daemon-wide, so only the global settings count
     (a project value is stored but ignored). See §10.
   - `context.bulk_max_chars` (default 200000), `context.compaction_enabled` (default `false`),
     `context.compaction_max_chars` (default 60000); both limits accept 1000–2000000. Agents always
     get the task request, plan, reviewer feedback, steer and planning prompt whole. Only
     recoverable bulk (diffs, gate/test and setup output) is fitted, per block: to
     `compaction_max_chars` when compaction is on for that agent, else to `bulk_max_chars`. A
     fitted block keeps its head and tail, and a marker in the middle says how much was omitted,
     which setting limited it and the command that shows the rest.
   - `compact_before_start` (on an agent or a workflow node): `true` turns compaction on for that
     agent, `false` turns it off, `null` defers to the next layer: node → `agent_overrides.<agent>`
     → `agent_defaults` → `context.compaction_enabled`. A stored `true` (older saved settings and
     the `gemini-flash-lite-swarm` preset) therefore means compaction is on; to follow
     `context.compaction_enabled` instead, set it to `null` with `conclave_patch_config`
     (`conclave_configure_agent` ignores null values).
3. Personas: `conclave_list_agents` / `conclave_upsert_agent(name, role, persona_md, project_id)`.
   A persona with `project_id` overrides the global one for that project only. The built-in
   planning personas (`planner`, `architect-as-planner`, `pm`, `test-architect`) are
   research-first: read the code, verify the current official docs of every library, API, CLI
   and service the plan relies on (with web search/fetch tools when available), cite sources
   (URL or `path:line`) and mark anything unverified as `ASSUMPTION`; the planner's JSON plan
   format is unchanged. Seeding only inserts missing personas, so an existing install keeps the
   ones stored in its database: to adopt a new built-in, `conclave_delete_agent(name)` without
   `project_id` resets that global persona to the built-in.

### Settings layers
- The effective config is built from layers, later ones winning: built-in defaults → global
  settings (`conclave_patch_config` without `project_id`) → the project's overrides →
  `agent_overrides.<agent>` on top of `agent_defaults`. That last step runs on the merged
  result, so a global `agent_overrides.<agent>` also beats a project's `agent_defaults` for
  that agent.
- A project stores only its **delta**: the keys whose value differs from what it inherits from
  built-in + global (+ its branch, below). A global change therefore reaches every project's
  next task, except where a project overrides that key.
- Every write is reduced to the delta: sending back the whole `conclave_get_config` document
  stores nothing new, and setting a key to the value it inherits removes the override (it then
  follows later global changes again).
- To unset an override, pass `unset` with dotted paths (at most 100), e.g.
  `unset: ["agent_overrides.reviewer", "execution.setup_command"]`. They are removed from the
  target layer before `config` is applied, so they inherit again; this is the only way to drop
  a project-only entry such as `agent_overrides.<agent>`. `null` is a value, not a delete:
  `setup_command: null` means "no setup command" even when the global settings set one.
- Dicts merge key by key; lists and `workflow` are replaced whole. A project `workflow: null`
  (or the `standard` template) derives the graph from the agents policy even when the global
  settings hold a custom workflow; unset `workflow` to inherit the global graph instead.
- A global patch changes the global layer only. With `apply_to_all_projects: true` (default
  `false`) it also removes the patched and unset keys from every project's delta, so every
  project takes the new global value. A global `conclave_configure_agent` always does this
  for the fields it sets.
- The integration branch is per project. A project inherits its `default_branch` (set at
  attach) as `execution.target_branch`, between the global and project layers, so the effective
  `target_branch` that `conclave_get_config` shows is always the branch its tasks merge into.
  Patching `target_branch` to another branch moves `default_branch` with it (restating the
  current one changes nothing). A project's delta holds a `target_branch` only when it names
  another branch than its `default_branch`, e.g. one an older attach copied from the global
  settings. A global `target_branch` therefore does not move existing projects; with
  `apply_to_all_projects: true` it becomes every project's `default_branch`.
- `conclave_get_config(project_id, include_layers: true)` returns `{effective, global,
  project}`: the effective config, the global payload and the project's stored delta (for
  the global target, `{effective, global}`). REST: `GET /api/projects/{id}/config?layers=true`.

### Engines and model strings
- `provider/model` (e.g. `deepseek/deepseek-flash`) runs on the **opencode** CLI with that
  provider's credentials.
- `claude-code/<model>` (e.g. `claude-code/claude-opus-5-5`, `claude-code/sonnet`,
  `claude-code/default`) runs on the **Claude Code** CLI with its own login.
- A model without `/` and without the `claude-code/` prefix is ignored by opencode (it uses its
  own default) — always use a full model string.
- `effort` (`low` | `medium` | `high` | `xhigh` | `max`) is passed to the engine. Unset (the
  default) means **auto**, the highest sensible level for the engine: `xhigh` on Claude Code
  (`max` only when you set it), and on opencode the highest variant the model reports
  (`opencode models <provider> --verbose`, read once per provider and cached; no variant flag
  when none is known). Models support different subsets (some only `low`/`high`/`max`): an
  explicit level the model lacks is clamped to the nearest one it has (the lower one on a tie),
  with one `log` event `stage: "effort_clamped"` (§6), and never fails the dispatch. Claude Code
  also falls back on its own when a model lacks the level passed, without a Conclave event. To
  go back to auto, unset the field: `conclave_patch_config(unset: ["agent_defaults.effort"])`
  (or `agent_overrides.<agent>.effort`).
- Only configure models whose engine and credentials actually work on this host; a quick
  low-cost task is the cheapest way to confirm a new model string end to end.

## 4. Choosing models per node (generic strategy)

- **Planning** (every task by default, shapes everything): the strongest reasoning model
  available, high effort. The built-in planning personas already read the code, verify current
  official docs and cite sources (§3); a custom planner persona should do the same.
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
Long requests are fine. The planner runs for every task by default
(`experimental.auto_planner_char_threshold: 0`); set `use_planner: false` on a task to skip it, or
raise the threshold so that only longer requests are planned. Prefer several small dependent tasks
(`parent_task_id`) over one giant task; a planning session (§9) can produce the tree for you.

## 6. Running and monitoring

- `conclave_create_task(project_id, request, title, auto_approve=true, use_planner)` →
  `conclave_get_task(task_id)` (the task is under the `task` key) for state and `result_summary`.
- `conclave_get_task_events(task_id, after_id)` returns events in **pages**: loop, passing the
  last seen `id` as `after_id`, until the page is empty. Useful types:
  `task.started`, `plan.artifact`, `attempt.started`, `agent.dispatched` (configured `model`,
  and `effort`: the level it runs with after auto and clamping, `null` when no flag is passed),
  `agent.result` (`ok`, `model_reported`, `cost_usd`, `error`), `agent.verdict`
  (`verdict`, `reason`), `pipeline.derived` (which reviewers will run), `node.started`,
  `node.waiting_for_approval`, `hitl.waiting`, `task.committed`, `task.merged`,
  `task.failed` (payload `reason`, e.g. `no_test_command`), `task.done`,
  `test_integrity.flagged` (`findings` with `file`, `test`, `kind`; `attempt`; `mode`). Skip
  `agent.output_chunk` (live output) unless debugging. A `log` event with
  `stage: "gate_skipped_no_tests"` means the gate ran no tests (`allow_no_test_command`). A
  `log` event with `stage: "context_truncated"` marks every cut of a diff or gate/setup output
  in an agent's prompt (`block`, `field`, `original_chars`, `kept_chars`, `agent`, `node_id`);
  there is none when nothing was cut. A `log` event with `stage: "effort_clamped"` marks a
  dispatch whose explicit effort the model does not support (`requested`, `used`, `model`,
  `agent`, `node_id`); the dispatch ran with `used`.
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
- **Test-integrity flags.** A `test_integrity.flagged` event (and a `test integrity (...)` note in
  the result summary) means the change took pre-existing coverage away: `deleted_file` (a test
  file deleted or moved out of the suite), `removed_test` (a test removed and not added back
  elsewhere in the change) or `skip_marker` (a skip, xfail, todo or focus marker added). Reviewers
  are told to fail such a change unless the request asks for it, so the developer usually
  restores the test on its next attempt. Under `block`, a flagged change that still passes ends
  `done` with `merged=False` on its branch, even with `auto_merge: true`. Read the diff: merge it
  with `conclave_merge_task` only if the removal or skip was intended (say so in the request next
  time), otherwise create a follow-up task that restores the tests and fixes the code. The check
  is conservative: a renamed test shows as a removed one. It reads test code and does not run it,
  so a test config change that deselects tests, or a test switched off by control flow, is not
  flagged; review those in the diff.
- **Comparing models (bake-off)**: give each candidate its own project on its own clone, the
  same base commit, the same spec and the same reviewers; score with acceptance tests the agents
  never see, the gate, review rounds, diff quality, cost and time. Merge the better diff.

### Workflow proposals and versions
- `conclave_list_workflow_proposals(project_id, state: "pending")` lists what the evolver
  suggests (a `workflow.evolution_proposed` event announces each new one): `reason.summary`,
  `reason.changes` (one line per change), the evidence (`task_id`, `catching_reviewers`,
  `task_failed`, `gate_failed_after_review`, `clean_streak`), the `proposed_graph` and the
  `base_graph` it was computed from. While a proposal is pending, an identical one is not stored
  again.
- `conclave_decide_workflow_proposal(project_id, proposal_id, decision, reason?)`: `approve`
  applies the proposed graph as a `proposal` version; it is refused while the live workflow
  differs from `base_graph`, so reject that proposal and run `conclave_evolve_workflow` for a
  fresh one. `reject` records the decision and leaves the workflow alone.
- `conclave_evolve_workflow(project_id, task_id?)` runs evolution on demand: in `suggest` mode it
  stores and returns a `proposal`, in `auto` mode it applies the change (an `evolver` version),
  in `off` mode it only previews; `apply: true` applies the change at once as your decision (an
  `operator` version).
- `conclave_list_workflow_versions(project_id, limit?)` shows every change, newest first:
  `source` (`evolver`, `operator`, `template`, `revert`, `proposal`), `reason`, `created_at`,
  `graph_after` and `graph_before` (`null` = the pipeline derived from the agents policy). The
  graph, node and edge tools, templates, Store installs, config patches that change `workflow`
  and the evolver all record one; pass `reason` to say why.
- `conclave_revert_workflow(project_id, version_id, reason?)` restores that version's
  `graph_after` as a new `revert` version (history is never deleted); a version holding `null`
  restores the derived pipeline.
- REST: `GET /api/projects/{id}/workflow/versions?limit=`,
  `POST /api/projects/{id}/workflow/versions/{version_id}/revert`,
  `GET /api/projects/{id}/workflow/proposals?state=`, and
  `POST /api/projects/{id}/workflow/proposals/{proposal_id}/approve` (or `/reject`); a refused
  approval is a `409`.

## 9. Planning big goals

`conclave_create_planning_session(project_id, prompt, title, max_rounds)` runs a panel of
specialists that debate and build a task tree. Follow it with `conclave_get_planning_session`,
add guidance with `conclave_interject_planning_message`, then `conclave_approve_planning_session`
(creates the tasks) or `conclave_cancel_planning_session`. Review the tree before approving.

## 10. When Conclave works on its own repository

- Develop on a separate branch (not the branch the running daemon is checked out on) and promote
  to the stable branch only when the gate is green.
- The file watcher is off by default: the daemon reloads its own code only when asked, with
  `conclave_reload_system` (or **Reload now** on the Settings page), which waits until no task
  runs unless `force: true`. To reload automatically when files in its checkout change, turn on
  `system.hot_reload_watch` in the global settings
  (`conclave_patch_config(config={"system": {"hot_reload_watch": true}})` without `project_id`).
  It is read live, so no restart is needed, and a project value is ignored. With it on, avoid
  editing that checkout while tasks run.
- `conclave_get_reload_status` shows `watcher_enabled`, `reload_pending` and `changed_files`
  without reloading.
- A hot reload keeps the running daemon objects (the watcher loop, the engine router), so a change
  to them takes effect after a restart; restart the daemon once after deploying such a change.

## 11. Pitfalls and response shapes

- `conclave_get_task` wraps the task as `{"task": {...}}`; list tools may wrap lists too —
  read the keys before indexing.
- Event lists are paged (§6); a single call is not the full history.
- Tool errors come back with `isError: true` and a text message; they are not exceptions.
- Attaching an already-attached path updates that project (§3).
- Settings are layered (§3): a global change reaches every project that does not override that
  key, on its next task. To see where a value comes from, call `conclave_get_config` with
  `include_layers: true`.
- A project override hides later global changes to that key. To follow the global value
  again, unset it (`unset: [...]`) or set it to the inherited value.
- `null` sets a value; it does not delete an override. Use `unset` to remove one.
- `apply_to_all_projects` defaults to `false`: a global patch writes nothing into projects. Pass
  `true` to also clear the patched keys from every project, for example when projects still
  override the old value.
- A global `target_branch` alone does not change where a project's tasks merge: patch the
  project's `target_branch`, or pass `apply_to_all_projects: true` (§3).
- A task that fails at **baseline** usually means the test or setup command is wrong for the
  target branch — fix the config, not the code.
- A task that fails at once with reason `no_test_command` never ran an agent: set
  `execution.baseline_test_command` (or `allow_no_test_command: true`) and approve it again
  (`conclave_approve_task`).
- A `done` task whose summary says `merged=False` waits for you: `auto_merge` is off by default,
  so a green task stays on its branch until you read the diff and call `conclave_merge_task`. To
  merge automatically again, set `execution.auto_merge: true` in the global settings or per
  project. With `not auto-merged` in the summary, `test_integrity: block` held it even though
  `auto_merge` is on (§8). The autonomous bug-fixer follows the same setting: with it off, a
  candidate whose fix task ends `done` is marked fixed while the fix waits on its branch.
- If a model's `model_reported` differs from the configured one, stop and investigate before
  trusting results (wrong engine, wrong model string, or routing problem).
- Frequent `context_truncated` events mean diffs or test output exceed the limit (§3): agents can
  still run the command named in the marker, but for whole-diff review raise
  `context.bulk_max_chars` (or `compaction_max_chars`), or make the task smaller or the test
  output quieter.
- `conclave_compact_session` on a task only records a summary and a size estimate; it never
  changes the request or plan that agents receive.
- With the default `experimental.workflow_evolution: suggest` the evolver never changes a
  workflow by itself: its proposals wait for you. After failed tasks, check
  `conclave_list_workflow_proposals(state: "pending")` and decide them (§8).
- Approving a proposal fails with `isError` when the workflow changed after it was computed (an
  edit, a template, a revert or another approval); reject it and run `conclave_evolve_workflow`
  again.
- A revert restores a version's `graph_after`. The workflow a project had before its first
  recorded change is only that version's `graph_before`: to get back to the derived pipeline
  when no version holds `null`, apply the `standard` template (itself recorded).
- Versions record changes to a project's own settings. A global `workflow` change reaches the
  projects that inherit it without a version in their history.

## 12. Tool index

Every MCP tool, grouped by purpose. Read the guide section named for the flow before using a
group for the first time.

### Projects & config
- `conclave_list_projects` — see which repositories are attached before picking a `project_id`.
- `conclave_attach_project` — attach a local git repo (re-attaching a path updates it); it
  stores only the settings chosen at attach.
- `conclave_detach_project` — remove a project, stop its worker, and clean in-progress worktrees.
- `conclave_get_project_status` — check whether a project's worker is paused or running.
- `conclave_pause_project` — stop the worker from starting new tasks.
- `conclave_resume_project` — restart a paused worker.
- `conclave_set_project_mode` — switch between task_queue and autonomous_bug_fixer mode.
- `conclave_get_config` — read the effective config before changing behaviour;
  `include_layers: true` also returns the global and project layers.
- `conclave_patch_config` — merge execution, agent, planning, and workflow settings into a
  project or the global layer; `unset` removes overrides, `apply_to_all_projects` clears the
  patched keys from every project.

### Tasks
- `conclave_list_tasks` — find tasks, optionally filtered by state.
- `conclave_create_task` — queue a coding or PR-review task (auto_approve=true runs it now).
- `conclave_get_task` — read state, branch, result summary, and checkpoints for one task.
- `conclave_get_task_events` — page through execution events while a task runs.
- `conclave_cancel_task` — stop a task you no longer want.
- `conclave_delete_task` — remove a finished task and its records.

### Review & merge
- `conclave_get_task_diff` — review the change as a unified diff before merging.
- `conclave_merge_task` — merge a done task's branch through the guarded merge.
- `conclave_get_task_verdicts` — read grounded reviewer verdicts across attempts.
- `conclave_get_task_checkpoints` — inspect step-level checkpoints and tree SHAs.

### Human-in-the-loop
- `conclave_approve_task` — approve an inbox, failed, blocked, cancelled, or paused task.
- `conclave_cascade_approve_task` — approve a parent and all descendants in dependency order.
- `conclave_set_hitl_checkpoint` — arm or disarm a pause on a node or edge.
- `conclave_complete_node` — resume, fail, or skip a node paused at a checkpoint.
- `conclave_steer_task` — inject guidance into the next dispatch.

### Workflow
- `conclave_get_workflow` — inspect the effective graph and available presets.
- `conclave_apply_workflow_template` — start from a built-in preset.
- `conclave_set_workflow` — replace the whole graph (validated as a DAG).
- `conclave_workflow_upsert_node` — add or change an agent/stage node.
- `conclave_workflow_remove_node` — drop a node and its edges.
- `conclave_workflow_upsert_edge` — connect or update a directed edge.
- `conclave_workflow_remove_edge` — remove one directed edge.
- `conclave_evolve_workflow` — evolve the graph from verdict and gate history (a proposal in
  `suggest` mode; `apply: true` applies it now).
- `conclave_list_workflow_proposals` — review the evolver's proposals and their evidence.
- `conclave_decide_workflow_proposal` — approve (apply) or reject a pending proposal.
- `conclave_list_workflow_versions` — see every recorded workflow change, newest first.
- `conclave_revert_workflow` — restore an earlier version's graph as a new version.

### Agents
- `conclave_list_agents` — see global and project-scoped personas.
- `conclave_upsert_agent` — create or update a persona with Markdown instructions.
- `conclave_delete_agent` — remove a custom persona or reset a built-in one.
- `conclave_configure_agent` — set model, effort, tooling, and retry settings per agent.
- `conclave_compact_session` — summarize a task's context, or digest old planning turns.

### Planning
- `conclave_list_planning_sessions` — find planning sessions for a project.
- `conclave_create_planning_session` — start specialists debating a task tree.
- `conclave_get_planning_session` — read the transcript and proposed tree.
- `conclave_interject_planning_message` — add human guidance and trigger a planner turn.
- `conclave_approve_planning_session` — materialize the tree into executable tasks.
- `conclave_cancel_planning_session` — stop a session you do not want.

### Quarantine
- `conclave_list_bug_candidates` — review candidates from the autonomous bug-fixer ledger.
- `conclave_list_quarantine` — inspect quarantined test patterns and gate health.
- `conclave_add_quarantine` — quarantine a flaky test pattern until a date.
- `conclave_delete_quarantine` — remove a quarantine entry.

### Store & swarms
- `conclave_store_list` — browse reusable workflows, personas, and config presets.
- `conclave_store_publish` — publish or update an item in the Store.
- `conclave_store_delete` — remove a custom Store item.
- `conclave_store_install` — apply a Store item to a project.
- `conclave_launch_swarm` — run a parallel batch of worktree sessions for mass changes.
- `conclave_get_swarm_status` — check a swarm's progress and task counts.

### Observability
- `conclave_list_interactions` — list logged agent and MCP conversations.
- `conclave_get_interaction` — read one full transcript and tool breakdown.
- `conclave_query_interactions` — run a read-only SQL SELECT over interaction logs.
- `conclave_get_observability_stats` — read aggregated cost, token, and model stats.

### System
- `conclave_get_reload_status` — check hot-reload state without triggering one.
- `conclave_reload_system` — hot-reload Conclave in place (defers while tasks run).
- `conclave_get_pilot_guide` — re-read this guide, one section or all of it.
