"""Complete MCP tool catalog exposing 100% of Conclave's control surface."""

from __future__ import annotations

import re
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Any

from .. import __version__
from ..agents import DEFAULT_PERSONAS
from ..config import (
    EdgeCondition,
    NodeKind,
    OutputContract,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    build_default_workflow,
    create_project_with_config,
    deep_merge,
    load_global_overrides,
    load_project_config,
    normalize_config_keys,
    patched_branch,
    resolve_project_config,
    save_global_config,
    save_project_config,
    workflow_templates,
)
from ..db import (
    CheckpointStatus,
    Project,
    ProjectMode,
    StoreItem,
    StoreItemKind,
    Task,
    TaskState,
)
from ..db import repositories as repo
from ..engine.compactor import compact_planning_session_context, compact_task_context
from ..engine.task_diff import (
    DEFAULT_DIFF_MAX_BYTES,
    MAX_DIFF_MAX_BYTES,
    TaskDiffError,
    compute_task_diff,
)
from ..engine.task_merge import TaskMergeError, merge_task
from ..engine.workflow_evolver import evolve_project_workflow
from ..events import EventType
from ..observability.parquet_sync import export_interaction_logs_to_parquet
from ..providers import engine_status
from ..runtime import Daemon, reload_status_report
from ..verification import quarantine_integrity

PILOT_GUIDE_URI = "conclave://guide/pilot"

_SECTION_RE = re.compile(r"^## (\d+)\.\s+(.+)$")


def load_pilot_guide() -> str:
    """Read the packaged pilot guide markdown (fresh on every call)."""
    return resources.files("conclave.mcp").joinpath("pilot_guide.md").read_text(encoding="utf-8")


def load_pilot_brief() -> str:
    """Read the packaged pilot brief that MCP clients add to the system prompt."""
    return resources.files("conclave.mcp").joinpath("pilot_brief.md").read_text(encoding="utf-8")


def _guide_sections(guide: str) -> list[tuple[str | None, str, str]]:
    """Split the guide into ``(number, title, text)`` for every ``## `` heading."""
    lines = guide.splitlines(keepends=True)
    starts = [idx for idx, line in enumerate(lines) if line.startswith("## ")]
    sections: list[tuple[str | None, str, str]] = []
    for pos, start in enumerate(starts):
        end = starts[pos + 1] if pos + 1 < len(starts) else len(lines)
        text = "".join(lines[start:end]).rstrip()
        heading = lines[start].rstrip("\n")
        match = _SECTION_RE.match(heading)
        number = match.group(1) if match else None
        title = match.group(2) if match else heading.removeprefix("## ").strip()
        sections.append((number, title, text))
    return sections


def _match_guide_section(
    sections: list[tuple[str | None, str, str]], section: str
) -> tuple[str | None, str, str] | None:
    """Match a section by exact number first, then by a title substring."""
    query = section.strip().lower().removesuffix(".").strip()
    if not query:
        return None
    for candidate in sections:
        if candidate[0] is not None and candidate[0] == query:
            return candidate
    for candidate in sections:
        if query in candidate[1].lower():
            return candidate
    return None


def _live_environment_footer(daemon: Daemon) -> str:
    """Markdown footer describing the daemon this tool call actually reached."""
    status = engine_status(daemon.provider)
    available = status["available"]
    engines = ", ".join(
        f"{name}: {'available' if ok else 'not found'}" for name, ok in available.items()
    )
    return (
        "---\n\n"
        "**Live environment** (generated at call time)\n\n"
        f"- Conclave version: {__version__}\n"
        f"- Default engine: {status['default']}\n"
        f"- Engines on this host: {engines}\n"
        f"- MCP tools: {len(MCP_TOOLS)}\n"
    )


class McpToolError(ValueError):
    """Raised when an MCP tool call has invalid arguments or preconditions."""


def _opt_str(args: dict[str, Any], key: str, default: str = "") -> str:
    val = args.get(key)
    if val is None:
        return default
    s = str(val).strip()
    return s if s else default


def _opt_int(args: dict[str, Any], key: str, default: int) -> int:
    val = args.get(key)
    if val is None or isinstance(val, bool):
        return default
    return int(val)


def _opt_bool(args: dict[str, Any], key: str, default: bool) -> bool:
    val = args.get(key)
    if val is None:
        return default
    if isinstance(val, str):
        low = val.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no", ""):
            return False
    return bool(val)


def _prop(
    typ: str,
    description: str,
    *,
    enum: list[str] | None = None,
    default: Any = None,
) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": typ, "description": description}
    if enum is not None:
        schema["enum"] = enum
    if default is not None:
        schema["default"] = default
    return schema


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    }


MCP_TOOLS: list[dict[str, Any]] = [
    # --- Projects & Worker Control ---
    _tool(
        "conclave_list_projects",
        "List all git repositories attached to Conclave.",
        {"limit": _prop("integer", "Max items to return (default 50).", default=50)},
    ),
    _tool(
        "conclave_attach_project",
        "Attach a local git repository to Conclave and start its background worker. The "
        "project stores only the branch, preset, template and parallelism chosen here and "
        "inherits everything else from the global settings.",
        {
            "name": _prop("string", "Display name for the project."),
            "path": _prop("string", "Absolute path to the local git repository."),
            "default_branch": _prop("string", "Target integration branch.", default="main"),
            "workflow_template": _prop(
                "string",
                "Optional workflow blueprint name from the Global Store.",
            ),
            "config_preset": _prop(
                "string",
                "Optional config preset name from the Global Store.",
            ),
            "max_parallel_tasks": _prop(
                "integer",
                "Optional max parallel tasks (1-16) for Swarm execution.",
            ),
        },
        required=["name", "path"],
    ),
    _tool(
        "conclave_detach_project",
        "Detach a project, stopping its worker and cleaning any in-progress worktrees.",
        {"project_id": _prop("string", "Project ID.")},
        required=["project_id"],
    ),
    _tool(
        "conclave_get_project_status",
        "Get a project's worker status (paused/running), execution mode, and config summary.",
        {"project_id": _prop("string", "Project ID.")},
        required=["project_id"],
    ),
    _tool(
        "conclave_pause_project",
        "Pause the background worker for a project.",
        {"project_id": _prop("string", "Project ID.")},
        required=["project_id"],
    ),
    _tool(
        "conclave_resume_project",
        "Resume the background worker for a project.",
        {"project_id": _prop("string", "Project ID.")},
        required=["project_id"],
    ),
    _tool(
        "conclave_set_project_mode",
        "Switch a project between task_queue and autonomous_bug_fixer mode.",
        {
            "project_id": _prop("string", "Project ID."),
            "mode": _prop(
                "string",
                "Execution mode.",
                enum=["task_queue", "autonomous_bug_fixer"],
            ),
        },
        required=["project_id", "mode"],
    ),
    _tool(
        "conclave_get_config",
        "Get the effective (resolved) ConclaveConfig JSON for a project (or the global "
        "settings); include_layers=true also returns the layers it is built from.",
        {
            "project_id": _prop(
                "string",
                "Optional Project ID (omit or pass 'global' to inspect global defaults).",
            ),
            "include_layers": _prop(
                "boolean",
                "Return {effective, global, project} instead: the effective config, the global "
                "payload and the project's stored delta (no project key for the global target).",
                default=False,
            ),
        },
    ),
    _tool(
        "conclave_patch_config",
        "Update configuration settings (target_branch, default_branch, execution, experimental, "
        "context, planning, agents, bug_fixer, notifications, protected, system, ui, "
        "agent_defaults, agent_overrides, workflow) per-repository or globally across Conclave. "
        "system is daemon-wide and only counts in the global settings. A project "
        "stores only what differs from what it inherits (built-in -> global): a key set to the "
        "inherited value, or listed in unset, inherits again.",
        {
            "project_id": _prop(
                "string",
                "Project ID to update, or omit / pass 'global' to update global settings.",
            ),
            "name": _prop(
                "string",
                "Optional new display name for the project (when project_id is specified).",
            ),
            "config": {
                "type": "object",
                "description": "Partial ConclaveConfig overrides object.",
            },
            "unset": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Dotted paths removed from the target layer before config is applied, so "
                    "they inherit again (e.g. 'agent_overrides.reviewer', "
                    "'execution.setup_command'; at most 100)."
                ),
            },
            "default_branch": _prop(
                "string",
                "Optional default/target branch update (syncs default_branch and target_branch).",
            ),
            "target_branch": _prop(
                "string",
                "Optional target integration branch (syncs target_branch and default_branch).",
            ),
            "branch_prefix": _prop("string", "Optional per-task branch prefix."),
            "auto_merge": _prop(
                "boolean",
                "Optional auto-merge toggle on green gate (default false: a green task ends done "
                "on its branch; merge it with conclave_merge_task).",
            ),
            "require_full_green": _prop("boolean", "Optional green-gate requirement toggle."),
            "parallel_reviewers": _prop("boolean", "Optional parallel reviewers toggle."),
            "max_parallel_tasks": _prop("integer", "Optional max parallel tasks (1-16)."),
            "setup_command": _prop("string", "Optional worktree setup command."),
            "baseline_test_command": _prop("string", "Optional authoritative test gate command."),
            "allow_no_test_command": _prop(
                "boolean",
                "Optional: run tasks without a test gate when no test command resolves "
                "(default false: such a task fails before any agent runs).",
            ),
            "test_integrity": _prop(
                "string",
                "Optional test-integrity guard for deleted, removed or skipped pre-existing tests: "
                "block (default; flag to reviewers, never auto-merge), warn (flag only) or off.",
                enum=["block", "warn", "off"],
            ),
            "wall_clock_budget_minutes": _prop(
                "integer", "Optional task timeout budget in minutes."
            ),
            "review_rounds_max": _prop("integer", "Optional max review rounds per task (1-50)."),
            "stop_on_failure": _prop("boolean", "Optional stop-on-failure toggle."),
            "setup_timeout_seconds": _prop(
                "integer", "Optional setup_command timeout in seconds."
            ),
            "retention_events_max": _prop(
                "integer", "Optional max retained events per project (>=100)."
            ),
            "grounding_checks": _prop("boolean", "Optional evidence grounding checks toggle."),
            "planner_enabled": _prop("boolean", "Optional auto-planner toggle."),
            "cross_attempt_memory": _prop("boolean", "Optional cross-attempt memory toggle."),
            "post_mortem_enabled": _prop("boolean", "Optional failure post-mortem toggle."),
            "structured_verdicts": _prop(
                "boolean", "Optional structured reviewer verdicts toggle."
            ),
            "persistent_baseline_cache": _prop(
                "boolean", "Optional persistent baseline cache toggle."
            ),
            "auto_planner_char_threshold": _prop(
                "integer",
                "Optional minimum request length (chars) that runs the planner (default 0: every "
                "task; a task's use_planner wins).",
            ),
            "cross_attempt_memory_entries": _prop(
                "integer", "Optional max cross-attempt memory entries (0-50)."
            ),
            "webhook_url": _prop("string", "Optional terminal notification webhook URL."),
            "context": {
                "type": "object",
                "description": (
                    "Optional ContextSettings overrides: compaction_enabled, "
                    "compaction_max_chars, bulk_max_chars (1000-2000000 chars per block of "
                    "diff or gate/setup output; requests, plans and feedback are never cut)."
                ),
            },
            "planning": {
                "type": "object",
                "description": "Optional PlanningSettings overrides.",
            },
            "agents": {
                "type": "object",
                "description": "Optional AgentsPolicy overrides.",
            },
            "bug_fixer": {
                "type": "object",
                "description": "Optional BugFixerPolicy overrides.",
            },
            "notifications": {
                "type": "object",
                "description": "Optional NotificationSettings overrides.",
            },
            "protected": {
                "type": "object",
                "description": "Optional ProtectedPaths overrides.",
            },
            "system": {
                "type": "object",
                "description": (
                    "Optional SystemSettings overrides, daemon-wide (global settings only; a "
                    "project value is ignored): hot_reload_watch (default false) reloads "
                    "Conclave when its source files change."
                ),
            },
            "ui": {
                "type": "object",
                "description": (
                    "Optional UiSettings overrides: model_choices, the models the UI pickers "
                    "offer (empty: the built-in list for the engines on this host)."
                ),
            },
            "agent_defaults": {
                "type": "object",
                "description": "Optional default AgentSettings overrides.",
            },
            "agent_overrides": {
                "type": "object",
                "description": "Optional per-agent AgentSettings overrides map.",
            },
            "workflow": {
                "type": "object",
                "description": "Optional WorkflowGraph JSON object.",
            },
            "apply_to_all_projects": _prop(
                "boolean",
                "Global updates only: also remove the patched and unset keys from every "
                "project's stored delta, so every project inherits the new global value (a "
                "patched target_branch also becomes each project's default_branch).",
                default=False,
            ),
        },
    ),
    # --- Tasks & Live Execution ---
    _tool(
        "conclave_list_tasks",
        "List tasks for a project, optionally filtered by state.",
        {
            "project_id": _prop("string", "Project ID."),
            "state": _prop(
                "string",
                "Optional state filter.",
                enum=[
                    "inbox",
                    "approved",
                    "in_progress",
                    "waiting_for_approval",
                    "done",
                    "failed",
                    "cancelled",
                    "blocked",
                ],
            ),
            "limit": _prop("integer", "Max tasks to return.", default=50),
        },
        required=["project_id"],
    ),
    _tool(
        "conclave_create_task",
        "Create a coding or PR-review task in a project (auto_approve=true queues immediately).",
        {
            "project_id": _prop("string", "Project ID."),
            "request": _prop("string", "Detailed task instructions/specification."),
            "title": _prop("string", "Short task title.", default=""),
            "auto_approve": _prop("boolean", "Approve immediately for execution.", default=True),
            "use_planner": _prop("boolean", "Force or skip the pre-developer planner."),
            "parent_task_id": _prop(
                "string",
                "Optional parent task ID to create a child/subtask in a dependency tree.",
            ),
            "source_branch": _prop(
                "string",
                "Optional source branch for PR reviews (e.g. 'origin/feature-login-fix'); "
                "checks out source_branch and diffs against default_branch/target_branch.",
            ),
        },
        required=["project_id", "request"],
    ),
    _tool(
        "conclave_get_task",
        "Get full details of a task, including its state, branch, result summary, and checkpoints.",
        {"task_id": _prop("string", "Task ID.")},
        required=["task_id"],
    ),
    _tool(
        "conclave_approve_task",
        "Approve an inbox, failed, blocked, cancelled, or waiting_for_approval task so the "
        "worker executes/resumes it.",
        {"task_id": _prop("string", "Task ID.")},
        required=["task_id"],
    ),
    _tool(
        "conclave_set_hitl_checkpoint",
        "Dynamically arm or disarm a Human-in-the-Loop (HITL) pause on a workflow node or "
        "connector edge for a task.",
        {
            "task_id": _prop("string", "Task ID."),
            "target_id": _prop(
                "string",
                "Workflow node ID (e.g. 'developer', 'gate') or connector edge ID to arm/disarm.",
            ),
            "enabled": _prop(
                "boolean",
                "True to arm the HITL pause, False to disarm/release it.",
                default=True,
            ),
        },
        required=["task_id", "target_id"],
    ),
    _tool(
        "conclave_complete_node",
        "Approve/complete a workflow node or HITL connector paused at waiting_for_approval, "
        "or manually override a node's status/verdict/output.",
        {
            "task_id": _prop("string", "Task ID."),
            "node_id": _prop(
                "string",
                "Workflow node ID (e.g. 'planner', 'developer', 'reviewer', 'gate', or '*').",
                default="*",
            ),
            "status": _prop(
                "string",
                "Target checkpoint status.",
                enum=["completed", "failed", "skipped"],
                default="completed",
            ),
            "verdict": _prop(
                "string",
                "Optional reviewer verdict override.",
                enum=["pass", "fail", "block", "decline"],
            ),
            "message": _prop(
                "string",
                "Optional guidance or feedback injected into the next stage.",
            ),
            "output": _prop(
                "string",
                "Optional manual output/artifact for this node.",
            ),
            "edge_id": _prop(
                "string",
                "Optional HITL connector edge ID.",
            ),
        },
        required=["task_id"],
    ),
    _tool(
        "conclave_cascade_approve_task",
        "Approve a parent task and all of its descendant subtasks in dependency order.",
        {"task_id": _prop("string", "Root task ID.")},
        required=["task_id"],
    ),
    _tool(
        "conclave_cancel_task",
        "Cancel an inbox, approved, waiting_for_approval, or currently running (in_progress) task.",
        {"task_id": _prop("string", "Task ID.")},
        required=["task_id"],
    ),
    _tool(
        "conclave_steer_task",
        "Inject live operator guidance into a task's next dispatch (or a specific node/connector).",
        {
            "task_id": _prop("string", "Task ID."),
            "message": _prop("string", "Guidance message to inject."),
            "node_id": _prop(
                "string",
                "Optional workflow node ID to scope guidance to (e.g. 'developer', 'reviewer').",
            ),
            "edge_id": _prop(
                "string",
                "Optional workflow edge/connector ID to scope guidance to.",
            ),
        },
        required=["task_id", "message"],
    ),
    _tool(
        "conclave_delete_task",
        "Delete a non-running task and its associated events and usage records.",
        {"task_id": _prop("string", "Task ID.")},
        required=["task_id"],
    ),
    _tool(
        "conclave_get_task_events",
        "Fetch execution events and live agent output chunks for a task.",
        {
            "task_id": _prop("string", "Task ID."),
            "after_id": _prop("integer", "Only return events with id > after_id.", default=0),
        },
        required=["task_id"],
    ),
    _tool(
        "conclave_get_task_verdicts",
        "Fetch grounded reviewer verdicts for a task across attempts.",
        {"task_id": _prop("string", "Task ID.")},
        required=["task_id"],
    ),
    _tool(
        "conclave_get_task_checkpoints",
        "Fetch step-level execution checkpoints and git tree SHAs for a task.",
        {"task_id": _prop("string", "Task ID.")},
        required=["task_id"],
    ),
    _tool(
        "conclave_get_task_diff",
        "Read-only unified diff and stat of a task's change (live worktree or task branch).",
        {
            "task_id": _prop("string", "Task ID."),
            "stat_only": _prop(
                "boolean", "Return only the diff stat and omit the unified diff text.",
                default=False,
            ),
            "max_bytes": _prop(
                "integer",
                f"Maximum diff size in bytes (1-{MAX_DIFF_MAX_BYTES}).",
                default=DEFAULT_DIFF_MAX_BYTES,
            ),
        },
        required=["task_id"],
    ),
    _tool(
        "conclave_merge_task",
        "Merge a done task's branch into the project's configured target branch.",
        {"task_id": _prop("string", "Task ID.")},
        required=["task_id"],
    ),
    # --- Dynamic Agent Diagram (Workflow Graph) ---
    _tool(
        "conclave_get_workflow",
        "Get the project's active node-edge agent workflow diagram and available preset templates.",
        {"project_id": _prop("string", "Project ID.")},
        required=["project_id"],
    ),
    _tool(
        "conclave_apply_workflow_template",
        "Apply a built-in workflow diagram preset (standard, fast_track, tdd_loop, deep_audit).",
        {
            "project_id": _prop("string", "Project ID."),
            "template_id": _prop(
                "string",
                "Template identifier.",
                enum=["standard", "fast_track", "tdd_loop", "deep_audit"],
            ),
        },
        required=["project_id", "template_id"],
    ),
    _tool(
        "conclave_set_workflow",
        "Replace a project's full node-edge workflow graph (validated for DAG integrity).",
        {
            "project_id": _prop("string", "Project ID."),
            "workflow": {"type": "object", "description": "Full WorkflowGraph JSON object."},
        },
        required=["project_id", "workflow"],
    ),
    _tool(
        "conclave_workflow_upsert_node",
        "Add or update a single agent/stage node on a project's workflow diagram.",
        {
            "project_id": _prop("string", "Project ID."),
            "id": _prop("string", "Unique node ID on the diagram (e.g. 'custom-sec')."),
            "kind": _prop(
                "string",
                "Node execution role.",
                enum=["planner", "developer", "reviewer", "gate", "merge", "custom"],
            ),
            "label": _prop("string", "Display label on the diagram.", default=""),
            "agent": _prop("string", "Agent persona name from DB."),
            "output_contract": _prop(
                "string",
                "Output contract.",
                enum=["text", "json", "verdict", "plan", "diff"],
                default="verdict",
            ),
            "prompt_template": _prop("string", "Optional custom instructions for this node."),
            "triggers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Diff triggers (e.g. ['new_files', 'db_change', 'auth_change']).",
            },
            "optional": _prop("boolean", "Whether node only runs on triggers.", default=False),
            "enabled": _prop("boolean", "Whether node is active.", default=True),
            "position_x": _prop("number", "Canvas X position.", default=560.0),
            "position_y": _prop("number", "Canvas Y position.", default=220.0),
            "model": _prop(
                "string",
                "Optional per-node model override (e.g. 'google/gemini-2.5-flash-lite').",
            ),
            "effort": _prop(
                "string",
                "Optional per-node reasoning effort override.",
                enum=["low", "medium", "high", "xhigh", "max"],
            ),
            "cli_agent": _prop(
                "string",
                "Optional per-node CLI --agent override (e.g. 'build', 'general', 'explore').",
            ),
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Per-node skills injected into dispatch.",
            },
            "thinking": _prop("boolean", "Optional per-node thinking toggle."),
            "compact_before_start": _prop(
                "boolean",
                "Optional per-node compaction of recoverable bulk (diffs, gate/test output) to "
                "context.compaction_max_chars; overrides the agent setting.",
            ),
            "pause_after": _prop(
                "boolean",
                "If True, pause task at waiting_for_approval after this node finishes.",
                default=False,
            ),
            "await_human_approval": _prop(
                "boolean",
                "Alias for pause_after: wait for human approval after this node finishes.",
                default=False,
            ),
        },
        required=["project_id", "id", "kind"],
    ),
    _tool(
        "conclave_workflow_remove_node",
        "Remove a node and all of its connected edges from a project's workflow diagram.",
        {
            "project_id": _prop("string", "Project ID."),
            "node_id": _prop("string", "Node ID to remove."),
        },
        required=["project_id", "node_id"],
    ),
    _tool(
        "conclave_workflow_upsert_edge",
        "Connect or update a directed edge between two nodes on a project's workflow diagram.",
        {
            "project_id": _prop("string", "Project ID."),
            "source": _prop("string", "Source node ID."),
            "target": _prop("string", "Target node ID."),
            "condition": _prop(
                "string",
                "Edge traversal condition.",
                enum=["always", "on_pass", "on_fail", "on_diff_trigger"],
                default="always",
            ),
            "label": _prop("string", "Edge label.", default=""),
            "max_iterations": _prop("integer", "Max retry iterations for on_fail.", default=3),
            "require_human": _prop(
                "boolean",
                "If True, pause on this connector for Human-in-the-Loop approval before advancing.",
                default=False,
            ),
        },
        required=["project_id", "source", "target"],
    ),
    _tool(
        "conclave_workflow_remove_edge",
        "Remove a directed edge from a project's workflow diagram by edge_id or source+target.",
        {
            "project_id": _prop("string", "Project ID."),
            "edge_id": _prop("string", "Edge ID to remove (optional if source+target given)."),
            "source": _prop("string", "Source node ID (optional if edge_id given)."),
            "target": _prop("string", "Target node ID (optional if edge_id given)."),
        },
        required=["project_id"],
    ),
    # --- Agent Personas ---
    _tool(
        "conclave_list_agents",
        "List all global and project-scoped agent personas.",
        {"project_id": _prop("string", "Optional project ID to include project overrides.")},
    ),
    _tool(
        "conclave_upsert_agent",
        "Create or update an agent persona (global or project-scoped) with Markdown instructions.",
        {
            "name": _prop("string", "Agent identifier (e.g. 'rust-auditor' or 'reviewer')."),
            "role": _prop(
                "string",
                "Agent role.",
                enum=[
                    "planning",
                    "developer",
                    "mandatory",
                    "conditional",
                    "analyst",
                    "hunter",
                    "repro",
                    "postmortem",
                ],
                default="conditional",
            ),
            "persona_md": _prop("string", "Markdown system prompt / persona definition."),
            "project_id": _prop("string", "Optional project ID for project-only override."),
        },
        required=["name", "persona_md"],
    ),
    _tool(
        "conclave_delete_agent",
        "Delete a custom agent persona (or reset a built-in global persona to its seed default).",
        {
            "name": _prop("string", "Agent name."),
            "project_id": _prop("string", "Optional project ID."),
        },
        required=["name"],
    ),
    # --- Multi-Agent Planning Sessions (Agent-ception) ---
    _tool(
        "conclave_list_planning_sessions",
        "List multi-agent planning sessions for a project.",
        {"project_id": _prop("string", "Project ID.")},
        required=["project_id"],
    ),
    _tool(
        "conclave_create_planning_session",
        "Start a multi-agent planning session where specialists debate and build a task tree.",
        {
            "project_id": _prop("string", "Project ID."),
            "prompt": _prop("string", "Feature or architectural goal to plan."),
            "title": _prop("string", "Short session title.", default=""),
            "max_rounds": _prop("integer", "Max discussion rounds.", default=5),
        },
        required=["project_id", "prompt"],
    ),
    _tool(
        "conclave_get_planning_session",
        "Get a planning session along with its discussion transcript and proposed task tree.",
        {"session_id": _prop("string", "Planning session ID.")},
        required=["session_id"],
    ),
    _tool(
        "conclave_interject_planning_message",
        "Send a human guidance message into an active planning session and trigger a planner turn.",
        {
            "session_id": _prop("string", "Planning session ID."),
            "content": _prop("string", "Guidance message."),
        },
        required=["session_id", "content"],
    ),
    _tool(
        "conclave_approve_planning_session",
        "Approve a planning session, materializing its proposed task tree into executable tasks.",
        {"session_id": _prop("string", "Planning session ID.")},
        required=["session_id"],
    ),
    _tool(
        "conclave_cancel_planning_session",
        "Cancel an active multi-agent planning session.",
        {"session_id": _prop("string", "Planning session ID.")},
        required=["session_id"],
    ),
    # --- Bug-Fixer & Quarantine ---
    _tool(
        "conclave_list_bug_candidates",
        "List discovered/reproduced/fixed bug candidates from the autonomous Bug-Fixer ledger.",
        {
            "project_id": _prop("string", "Project ID."),
            "needs_human_only": _prop(
                "boolean", "Only return candidates in declined_needs_human.", default=False
            ),
        },
        required=["project_id"],
    ),
    _tool(
        "conclave_list_quarantine",
        "List quarantined test patterns and gate health integrity for a project.",
        {"project_id": _prop("string", "Project ID.")},
        required=["project_id"],
    ),
    _tool(
        "conclave_add_quarantine",
        "Quarantine a flaky test pattern until an ISO date (YYYY-MM-DD).",
        {
            "project_id": _prop("string", "Project ID."),
            "pattern": _prop("string", "Test pattern to exclude."),
            "reason": _prop("string", "Reason for quarantine."),
            "until": _prop("string", "Expiry date in YYYY-MM-DD format."),
        },
        required=["project_id", "pattern", "reason", "until"],
    ),
    _tool(
        "conclave_delete_quarantine",
        "Remove a test quarantine entry by ID.",
        {"entry_id": _prop("string", "Quarantine entry ID.")},
        required=["entry_id"],
    ),
    # --- Global Store (Workflows, Agents, Config Presets) ---
    _tool(
        "conclave_store_list",
        "List reusable workflow diagrams, agent personas, and config presets in the Global Store.",
        {
            "kind": _prop(
                "string",
                "Optional filter by store item kind.",
                enum=["workflow", "agent", "config_preset"],
            ),
        },
    ),
    _tool(
        "conclave_store_publish",
        "Publish or update a workflow diagram, agent persona, or config preset in the Store.",
        {
            "kind": _prop(
                "string",
                "Item kind.",
                enum=["workflow", "agent", "config_preset"],
            ),
            "name": _prop("string", "Unique name within its kind."),
            "description": _prop("string", "Short description.", default=""),
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Searchable tags.",
            },
            "payload": {
                "type": "object",
                "description": (
                    "For 'workflow': WorkflowGraph JSON; for 'agent': "
                    "{'role': ..., 'persona_md': ...}; for 'config_preset': ConclaveConfig."
                ),
            },
        },
        required=["kind", "name", "payload"],
    ),
    _tool(
        "conclave_store_delete",
        "Delete a custom item from the Global Store by item_id.",
        {"item_id": _prop("string", "Store item ID.")},
        required=["item_id"],
    ),
    _tool(
        "conclave_store_install",
        "Install/apply a Global Store workflow, agent persona, or config preset onto a project.",
        {
            "project_id": _prop("string", "Target Project ID."),
            "item_id": _prop("string", "Store item ID (optional if kind + name given)."),
            "kind": _prop(
                "string",
                "Store item kind (optional if item_id given).",
                enum=["workflow", "agent", "config_preset"],
            ),
            "name": _prop("string", "Store item name (optional if item_id given)."),
        },
        required=["project_id"],
    ),
    # --- Parallel Swarms for Mass Changes ---
    _tool(
        "conclave_launch_swarm",
        "Launch a parallel Swarm of dedicated worktree sessions for mass changes.",
        {
            "project_id": _prop("string", "Project ID."),
            "concurrency": _prop(
                "integer",
                "Number of parallel worktree sessions to run concurrently (1-16).",
                default=4,
            ),
            "prompt": _prop(
                "string",
                "Mass-change objective (auto-decomposed into parallel units if units omitted).",
            ),
            "units": {
                "type": "array",
                "description": "Explicit list of independent work units.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "request": {"type": "string"},
                        "use_planner": {"type": "boolean"},
                    },
                    "required": ["request"],
                },
            },
            "auto_approve": _prop(
                "boolean",
                "Approve all swarm tasks immediately for parallel execution.",
                default=True,
            ),
        },
        required=["project_id"],
    ),
    _tool(
        "conclave_get_swarm_status",
        "Get progress and task status counts for a running or completed Swarm batch.",
        {
            "project_id": _prop("string", "Project ID."),
            "swarm_id": _prop("string", "Swarm batch ID."),
        },
        required=["project_id", "swarm_id"],
    ),
    # --- Agent Execution Settings, Context Compaction & Observability ---
    _tool(
        "conclave_configure_agent",
        "Configure an agent's execution settings (model, effort, cli_agent, skills, thinking, "
        "pure, compact_before_start, timeout_minutes, max_retries) in agent_defaults or "
        "agent_overrides[agent] for a project, or globally if project_id is omitted/'global' "
        "(the configured fields are then cleared from every project, which inherits them).",
        {
            "project_id": _prop(
                "string",
                "Project ID (omit or pass 'global' to configure global default agent settings).",
            ),
            "agent": _prop(
                "string",
                "Agent name (e.g. 'developer', 'reviewer'). Omit or pass 'default' for defaults.",
            ),
            "model": _prop(
                "string",
                "Model identifier (provider/model), e.g. 'google/gemini-2.5-flash-lite'.",
            ),
            "effort": _prop(
                "string",
                "Reasoning effort/variant tier. Unset is auto: xhigh on Claude Code, the model's "
                "highest variant on opencode; a level the model lacks is clamped (an "
                "effort_clamped log event). To go back to auto, unset it with "
                "conclave_patch_config.",
                enum=["low", "medium", "high", "xhigh", "max"],
            ),
            "cli_agent": _prop(
                "string",
                "Optional CLI agent name passed via --agent (e.g. 'build', 'general', 'explore').",
            ),
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Skills or capability instructions injected into the prompt.",
            },
            "thinking": _prop("boolean", "Enable reasoning/thinking output (--thinking)."),
            "pure": _prop("boolean", "Run opencode in --pure mode without external plugins."),
            "compact_before_start": _prop(
                "boolean",
                "Fit this agent's recoverable bulk (diffs, gate/test and setup output) to "
                "context.compaction_max_chars per block; unset inherits agent_defaults, then "
                "context.compaction_enabled. Requests, plans and feedback are never cut.",
            ),
            "timeout_minutes": _prop("integer", "Timeout in minutes (1-600)."),
            "max_retries": _prop("integer", "Max retries (1-50)."),
        },
    ),
    _tool(
        "conclave_compact_session",
        "Record a compacted summary and size estimate for a task_id (never rewrites its request "
        "or plan), or summarize a planning_session_id's earlier turns into one digest message.",
        {
            "task_id": _prop(
                "string", "Task ID to compact (provide task_id or planning_session_id)."
            ),
            "planning_session_id": _prop(
                "string",
                "Planning session ID to compact (provide task_id or planning_session_id).",
            ),
            "session_id": _prop("string", "Alias for planning_session_id."),
        },
    ),
    _tool(
        "conclave_list_interactions",
        "List logged agent and MCP conversations from interaction_logs.",
        {
            "project_id": _prop("string", "Optional project ID filter."),
            "task_id": _prop("string", "Optional task ID filter."),
            "agent": _prop("string", "Optional agent filter."),
            "model": _prop("string", "Optional model filter."),
            "search": _prop("string", "Optional full-text search query."),
            "limit": _prop("integer", "Max rows to return (default 50).", default=50),
        },
    ),
    _tool(
        "conclave_get_interaction",
        "Fetch the full prompt, response transcript, and tool breakdown for a session_id.",
        {
            "session_id": _prop("string", "Interaction log session ID."),
        },
        required=["session_id"],
    ),
    _tool(
        "conclave_query_interactions",
        "Execute a read-only SQL SELECT query against interaction_logs and return rows.",
        {
            "sql": _prop("string", "Read-only SELECT or WITH SQL query against interaction_logs."),
            "limit": _prop("integer", "Max rows to return (default 200).", default=200),
        },
        required=["sql"],
    ),
    _tool(
        "conclave_get_observability_stats",
        "Get aggregated observability statistics (sessions, cost, token totals, per-model and "
        "per-agent breakdowns) from interaction_logs, optionally exporting to Parquet.",
        {
            "project_id": _prop("string", "Optional project ID filter."),
            "export_parquet": _prop(
                "boolean",
                "If true, also synchronize/export interaction_logs to INTERACTION_LOGS.parquet.",
                default=False,
            ),
        },
    ),
    _tool(
        "conclave_get_reload_status",
        "Get current quiescent hot-reload status, pending state, changed files, module coverage "
        "and watcher_enabled (the global system.hot_reload_watch setting) without triggering a "
        "reload.",
        {},
    ),
    _tool(
        "conclave_reload_system",
        "Hot-reload all Conclave Python modules, DB migrations, global defaults, runtime "
        "orchestrators/workers, and FastAPI/MCP routes in-place (defers until quiescent "
        "if a task is in-flight unless force=true).",
        {
            "force": _prop(
                "boolean",
                "If true, reload immediately even if a task is currently in-flight.",
                default=False,
            ),
        },
    ),
    _tool(
        "conclave_evolve_workflow",
        "Longitudinally evolve a project's workflow graph based on historical verdicts, "
        "gate outcomes, and clean task streaks (growing on failures/catches, shrinking "
        "zero-catch non-core reviewers after >= 3 clean done tasks).",
        {
            "project_id": _prop("string", "Project ID."),
            "task_id": _prop("string", "Optional task ID to evaluate."),
            "gate_failed_after_review": _prop(
                "boolean",
                "Whether the green-gate failed after reviewers passed.",
                default=False,
            ),
            "task_failed": _prop(
                "boolean",
                "Whether the task failed.",
                default=False,
            ),
        },
        required=["project_id"],
    ),
    # --- Pilot Guide ---
    _tool(
        "conclave_get_pilot_guide",
        "Read the Conclave pilot guide (markdown): operating flows, review/merge, model "
        "strategy, pitfalls, and the full tool index. Optionally return one section by its "
        "number or a substring of its title.",
        {
            "section": _prop(
                "string",
                'Optional section selector: its number (e.g. "6") or a substring of its title '
                '(e.g. "monitoring"). Omit for the whole guide.',
            )
        },
    ),
]


async def _require_project(daemon: Daemon, project_id: str) -> Project:
    if not project_id:
        raise McpToolError("project_id is required")
    project = await repo.get_project(daemon.db, project_id)
    if project is None:
        raise McpToolError(f"project not found: {project_id}")
    return project


async def _require_task(daemon: Daemon, task_id: str) -> Task:
    if not task_id:
        raise McpToolError("task_id is required")
    task = await repo.get_task(daemon.db, task_id)
    if task is None:
        raise McpToolError(f"task not found: {task_id}")
    return task


async def _resolve_active_workflow(daemon: Daemon, project: Project) -> WorkflowGraph:
    config = await resolve_project_config(daemon.db, project)
    return config.workflow or build_default_workflow(
        config.agents,
        parallel_reviewers=config.execution.parallel_reviewers,
    )


async def _save_workflow(daemon: Daemon, project: Project, graph: WorkflowGraph) -> dict[str, Any]:
    resolved = await save_project_config(
        daemon.db, project, {"workflow": graph.model_dump(mode="json")}
    )
    active = resolved.workflow or graph
    wf_payload: dict[str, Any] = {
        "workflow": active.model_dump(mode="json"),
        "is_custom": True,
    }
    updated_proj = await _require_project(daemon, project.id)
    await daemon.bus.emit(
        type=EventType.workflow_updated,
        project_id=project.id,
        payload=wf_payload,
    )
    await daemon.bus.emit(
        type=EventType.project_updated,
        project_id=project.id,
        payload={"project": updated_proj.model_dump(mode="json")},
    )
    return wf_payload


async def _emit_projects_updated(daemon: Daemon, projects: list[Project]) -> None:
    """Announce each project's row and effective config after a global config change."""
    for proj in projects:
        resolved = await resolve_project_config(daemon.db, proj)
        await daemon.bus.emit(
            type=EventType.project_updated,
            project_id=proj.id,
            payload={
                "project": proj.model_dump(mode="json"),
                "config": resolved.model_dump(mode="json"),
            },
        )


async def call_mcp_tool(daemon: Daemon, name: str, args: dict[str, Any]) -> Any:
    """Execute a named Conclave MCP tool against *daemon* and return a JSON-serializable result."""
    # --- Projects ---
    if name == "conclave_list_projects":
        limit = max(1, min(_opt_int(args, "limit", 50), 500))
        projects = await repo.list_projects(daemon.db, limit=limit)
        return [p.model_dump(mode="json") for p in projects]

    if name == "conclave_attach_project":
        proj_name = _opt_str(args, "name")
        raw_path = _opt_str(args, "path")
        default_branch = _opt_str(args, "default_branch", "main") or "main"
        if not proj_name or not raw_path:
            raise McpToolError("name and path are required")
        path = Path(raw_path).expanduser()
        if not path.is_dir():
            raise McpToolError(f"path is not a directory: {path}")
        if not (path / ".git").exists():
            raise McpToolError(f"not a git repository: {path}")

        # Only what the caller chose is stored; everything else is inherited (built-in ->
        # global) when the config is resolved, and the server keeps just the delta.
        initial_cfg: dict[str, Any] = {}
        preset_name = _opt_str(args, "config_preset")
        if preset_name:
            preset = await repo.get_store_item_by_name(
                daemon.db, StoreItemKind.config_preset, preset_name
            )
            if preset is not None:
                initial_cfg = deep_merge(initial_cfg, preset.payload)
        wf_tpl = _opt_str(args, "workflow_template")
        if wf_tpl:
            wf_item = await repo.get_store_item_by_name(
                daemon.db, StoreItemKind.workflow, wf_tpl
            )
            if wf_item is not None:
                initial_cfg["workflow"] = wf_item.payload
            else:
                templates = workflow_templates()
                if wf_tpl in templates:
                    initial_cfg["workflow"] = templates[wf_tpl].model_dump(mode="json")
        if args.get("max_parallel_tasks") is not None:
            exec_sec = dict(initial_cfg.get("execution") or {})
            exec_sec["max_parallel_tasks"] = max(
                1, min(_opt_int(args, "max_parallel_tasks", 1), 16)
            )
            initial_cfg["execution"] = exec_sec

        existing = await repo.get_project_by_path(daemon.db, str(path))
        exec_cfg = dict(initial_cfg.get("execution") or {})
        if existing is not None or "target_branch" not in exec_cfg or default_branch != "main":
            exec_cfg["target_branch"] = default_branch
        initial_cfg["execution"] = exec_cfg

        if existing is not None:
            await save_project_config(
                daemon.db,
                existing,
                initial_cfg,
                name=proj_name or existing.name,
                default_branch=default_branch,
            )
            updated_existing = await _require_project(daemon, existing.id)
            await daemon.start_worker(updated_existing.id)
            proj_json = updated_existing.model_dump(mode="json")
            await daemon.bus.emit(
                type=EventType.project_updated,
                project_id=updated_existing.id,
                payload={"project": proj_json},
            )
            return proj_json

        project = await create_project_with_config(
            daemon.db,
            name=proj_name,
            path=str(path),
            default_branch=default_branch,
            patch=initial_cfg,
        )
        await daemon.start_worker(project.id)
        proj_json = project.model_dump(mode="json")
        await daemon.bus.emit(
            type=EventType.project_created,
            payload={"project": proj_json},
        )
        return proj_json

    if name in ("conclave_detach_project", "conclave_delete_project"):
        pid = _opt_str(args, "project_id")
        await _require_project(daemon, pid)
        await daemon.stop_worker(pid)
        await daemon.cleanup_in_progress_work(pid)
        await repo.delete_project(daemon.db, pid)
        await daemon.bus.emit(
            type=EventType.project_deleted,
            payload={"project_id": pid},
        )
        return {"detached": pid}

    if name == "conclave_get_project_status":
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        usage = await repo.usage_summary(daemon.db, project.id)
        return {
            "project": project.model_dump(mode="json"),
            "paused": daemon.is_paused(project.id),
            "mode": project.mode.value,
            "usage": usage,
        }

    if name == "conclave_pause_project":
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        return {"project_id": project.id, "paused": daemon.set_paused(project.id, True)}

    if name == "conclave_resume_project":
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        return {"project_id": project.id, "running": daemon.set_paused(project.id, False)}

    if name == "conclave_set_project_mode":
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        mode = ProjectMode(_opt_str(args, "mode", "task_queue"))
        await repo.set_project_mode(daemon.db, project.id, mode)
        updated = await _require_project(daemon, project.id)
        updated_json = updated.model_dump(mode="json")
        await daemon.bus.emit(
            type=EventType.project_updated,
            project_id=project.id,
            payload={"project": updated_json},
        )
        return updated_json

    if name == "conclave_get_config":
        raw_pid = _opt_str(args, "project_id")
        cfg_project = (
            None
            if not raw_pid or raw_pid in ("global", "default", "defaults", "*")
            else await _require_project(daemon, raw_pid)
        )
        effective = (await resolve_project_config(daemon.db, cfg_project)).model_dump(mode="json")
        if not _opt_bool(args, "include_layers", False):
            return effective
        layers: dict[str, Any] = {
            "effective": effective,
            "global": await load_global_overrides(daemon.db),
        }
        if cfg_project is not None:
            layers["project"] = cfg_project.config
        return layers

    if name in ("conclave_patch_config", "conclave_update_config", "conclave_update_settings"):
        raw_pid = _opt_str(args, "project_id")
        new_name = _opt_str(args, "name") or None
        raw_patch = args.get("config")
        patch: dict[str, Any] = (
            normalize_config_keys(dict(raw_patch)) if isinstance(raw_patch, dict) else {}
        )
        # Support top-level convenience settings (target_branch, default_branch, execution.*, etc.)
        new_branch_raw = (
            _opt_str(args, "default_branch")
            or _opt_str(args, "target_branch")
            or patch.pop("default_branch", None)
        )
        new_branch = str(new_branch_raw).strip() if new_branch_raw else None
        exec_patch = dict(patch.get("execution") or {})
        if new_branch:
            exec_patch["target_branch"] = new_branch
        for exec_key in (
            "branch_prefix",
            "auto_merge",
            "require_full_green",
            "parallel_reviewers",
            "max_parallel_tasks",
            "setup_command",
            "baseline_test_command",
            "allow_no_test_command",
            "test_integrity",
            "wall_clock_budget_minutes",
            "review_rounds_max",
            "stop_on_failure",
            "setup_timeout_seconds",
            "retention_events_max",
        ):
            if exec_key in args and args[exec_key] is not None:
                exec_patch[exec_key] = args[exec_key]
        if exec_patch:
            patch["execution"] = exec_patch

        exp_patch = dict(patch.get("experimental") or {})
        for exp_key in (
            "grounding_checks",
            "planner_enabled",
            "cross_attempt_memory",
            "post_mortem_enabled",
            "structured_verdicts",
            "persistent_baseline_cache",
            "auto_planner_char_threshold",
            "cross_attempt_memory_entries",
        ):
            if exp_key in args and args[exp_key] is not None:
                exp_patch[exp_key] = args[exp_key]
        if exp_patch:
            patch["experimental"] = exp_patch

        if args.get("webhook_url") is not None:
            notif_patch = dict(patch.get("notifications") or {})
            notif_patch["webhook_url"] = _opt_str(args, "webhook_url")
            patch["notifications"] = notif_patch

        for sec_name in (
            "context",
            "planning",
            "agents",
            "bug_fixer",
            "notifications",
            "protected",
            "system",
            "ui",
            "agent_defaults",
            "agent_overrides",
            "workflow",
        ):
            if sec_name in args and isinstance(args[sec_name], dict):
                patch[sec_name] = deep_merge(
                    dict(patch.get(sec_name) or {}), normalize_config_keys(args[sec_name])
                )

        raw_unset = args.get("unset")
        if raw_unset is not None and (not isinstance(raw_unset, list) or len(raw_unset) > 100):
            raise McpToolError("unset must be a list of at most 100 dotted paths")
        unset_paths: list[Any] = list(raw_unset or [])

        if not patch and not new_branch and not new_name and not unset_paths:
            raise McpToolError("config object, unset paths or setting fields are required")

        if not raw_pid or raw_pid in ("global", "default", "defaults", "*"):
            resolved_global, updated_projects = await save_global_config(
                daemon.db,
                patch,
                unset=unset_paths,
                apply_to_all_projects=_opt_bool(args, "apply_to_all_projects", False),
            )
            await _emit_projects_updated(daemon, updated_projects)
            return resolved_global.model_dump(mode="json")

        project = await _require_project(daemon, raw_pid)
        # A patched target_branch moves the project's branch; restating it changes nothing.
        new_branch = new_branch or patched_branch(project, patch)
        if new_branch:
            patch = deep_merge(patch, {"execution": {"target_branch": new_branch}})
        resolved = await save_project_config(
            daemon.db,
            project,
            patch,
            unset=unset_paths,
            name=new_name or project.name,
            default_branch=new_branch or project.default_branch,
        )
        updated_proj = await _require_project(daemon, project.id)
        resolved_json = resolved.model_dump(mode="json")
        await daemon.bus.emit(
            type=EventType.project_updated,
            project_id=project.id,
            payload={
                "project": updated_proj.model_dump(mode="json"),
                "config": resolved_json,
            },
        )
        if "workflow" in patch:
            active_wf = resolved.workflow or build_default_workflow(
                resolved.agents,
                parallel_reviewers=resolved.execution.parallel_reviewers,
            )
            await daemon.bus.emit(
                type=EventType.workflow_updated,
                project_id=project.id,
                payload={"workflow": active_wf.model_dump(mode="json")},
            )
        return resolved_json

    # --- Tasks ---
    if name == "conclave_list_tasks":
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        raw_state = _opt_str(args, "state")
        state = TaskState(raw_state) if raw_state else None
        limit = max(1, min(_opt_int(args, "limit", 50), 500))
        tasks = await repo.list_tasks(daemon.db, project.id, state, limit=limit)
        return [t.model_dump(mode="json") for t in tasks]

    if name == "conclave_create_task":
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        request_text = _opt_str(args, "request")
        if not request_text:
            raise McpToolError("request is required")
        title = _opt_str(args, "title")
        auto_approve = _opt_bool(args, "auto_approve", True)
        use_planner_raw = args.get("use_planner")
        use_planner = bool(use_planner_raw) if isinstance(use_planner_raw, bool) else None
        parent_task_id = _opt_str(args, "parent_task_id") or None
        source_branch = _opt_str(args, "source_branch") or None
        state = TaskState.approved if auto_approve else TaskState.inbox
        task = await repo.create_task(
            daemon.db,
            project_id=project.id,
            request=request_text,
            title=title,
            use_planner=use_planner,
            state=state,
            parent_task_id=parent_task_id,
            source_branch=source_branch,
        )
        await daemon.bus.emit(
            type=EventType.task_created,
            project_id=project.id,
            task_id=task.id,
            payload={
                "title": task.title,
                "auto_approve": auto_approve,
                "parent_task_id": task.parent_task_id,
                "source_branch": task.source_branch,
            },
        )
        return task.model_dump(mode="json")

    if name == "conclave_get_task":
        task = await _require_task(daemon, _opt_str(args, "task_id"))
        checkpoints = await repo.list_task_checkpoints(daemon.db, task.id)
        verdicts = await repo.list_verdicts(daemon.db, task.id, limit=100)
        usage = await repo.get_task_usage(daemon.db, task.id)
        return {
            "task": task.model_dump(mode="json"),
            "checkpoints": [cp.model_dump(mode="json") for cp in checkpoints],
            "verdicts": [v.model_dump(mode="json") for v in verdicts],
            "usage": usage,
            "hitl": daemon.get_hitl_state(task.id),
        }

    if name == "conclave_approve_task":
        task = await _require_task(daemon, _opt_str(args, "task_id"))
        if task.state == TaskState.waiting_for_approval:
            signaled = daemon.resolve_node(task.id, "*", status="completed")
            if not signaled:
                await repo.approve_task(daemon.db, task.id)
            await daemon.bus.emit(
                type=EventType.task_approved,
                project_id=task.project_id,
                task_id=task.id,
            )
            return {"approved": task.id}
        ok = await repo.approve_task(daemon.db, task.id)
        if not ok:
            raise McpToolError(
                f"task {task.id} is in state {task.state.value}; "
                "only inbox, failed, blocked, cancelled, or waiting_for_approval can be approved"
            )
        await daemon.bus.emit(
            type=EventType.task_approved,
            project_id=task.project_id,
            task_id=task.id,
        )
        return {"approved": task.id}

    if name in ("conclave_set_hitl_checkpoint", "conclave_toggle_hitl"):
        task = await _require_task(daemon, _opt_str(args, "task_id"))
        target_id = (
            _opt_str(args, "target_id")
            or _opt_str(args, "node_id")
            or _opt_str(args, "edge_id")
        )
        if not target_id:
            raise McpToolError("target_id is required")
        enabled = _opt_bool(args, "enabled", True)
        armed = daemon.set_hitl_checkpoint(task.id, target_id, enabled)
        hitl_event_type = getattr(
            EventType,
            "hitl_updated",
            EventType.hitl_waiting if enabled else EventType.hitl_resolved,
        )
        await daemon.bus.emit(
            type=hitl_event_type,
            project_id=task.project_id,
            task_id=task.id,
            payload={
                "target_id": target_id,
                "enabled": enabled,
                "armed": armed,
            },
        )
        return {
            "task_id": task.id,
            "target_id": target_id,
            "enabled": enabled,
            **daemon.get_hitl_state(task.id),
        }

    if name in ("conclave_complete_node", "conclave_approve_node"):
        task = await _require_task(daemon, _opt_str(args, "task_id"))
        node_id = _opt_str(args, "node_id", "*") or "*"
        raw_status = _opt_str(args, "status", "completed").lower()
        if raw_status not in ("completed", "failed", "skipped"):
            raise McpToolError("status must be one of: completed, failed, skipped")
        verdict = _opt_str(args, "verdict").lower() or None
        message = _opt_str(args, "message") or None
        output = _opt_str(args, "output") or None
        edge_id = _opt_str(args, "edge_id") or None
        cp_status = CheckpointStatus(raw_status)
        existing_cps = await repo.list_task_checkpoints(daemon.db, task.id)
        waiting_hitl = daemon.get_hitl_state(task.id).get("waiting", [])
        target_node = (
            node_id
            if node_id not in ("*", "all")
            else (
                next(
                    (
                        cp.node_id
                        for cp in reversed(existing_cps)
                        if cp.status == CheckpointStatus.waiting_for_approval
                    ),
                    waiting_hitl[0] if waiting_hitl else "developer",
                )
            )
        )
        node_cps = [cp for cp in existing_cps if cp.node_id == target_node]
        default_att = 0 if target_node in ("baseline", "planner") else 1
        attempt = node_cps[-1].attempt if node_cps else default_att
        artifact: dict[str, Any] = dict(node_cps[-1].artifact) if node_cps else {}
        artifact["operator_completed"] = True
        if message:
            artifact["operator_message"] = message
        if output:
            artifact["operator_output"] = output
            if target_node == "planner":
                artifact["plan_preamble"] = (
                    f"\n\n=== PLAN (Operator Completed) ===\n{output}\n=== END PLAN ===\n"
                )
        if verdict:
            artifact["verdict"] = verdict

        cp = await repo.save_task_checkpoint(
            daemon.db,
            task_id=task.id,
            node_id=target_node,
            attempt=attempt,
            status=cp_status,
            git_tree_sha=node_cps[-1].git_tree_sha if node_cps else None,
            artifact=artifact,
        )
        if verdict:
            await repo.add_verdict(
                daemon.db,
                task_id=task.id,
                attempt=max(1, attempt),
                agent=target_node,
                verdict=verdict,
                reason=message or output or f"Operator marked {target_node} as {verdict}",
                source="operator",
                grounded_count=1,
                evidence=[],
            )
        signaled = daemon.resolve_node(
            task.id,
            target_node,
            status=raw_status,
            message=message,
            verdict=verdict,
            output=output,
            edge_id=edge_id,
        )
        if not signaled and task.state == TaskState.waiting_for_approval:
            await repo.approve_task(daemon.db, task.id)
        await daemon.bus.emit(
            type=EventType.hitl_resolved,
            project_id=task.project_id,
            task_id=task.id,
            payload={
                "node_id": target_node,
                "edge_id": edge_id,
                "attempt": attempt,
                "status": cp_status.value,
                "verdict": verdict,
                "message": message,
            },
        )
        await daemon.bus.emit(
            type=EventType.node_completed,
            project_id=task.project_id,
            task_id=task.id,
            payload={
                "node_id": target_node,
                "workflow_node_id": target_node,
                "attempt": attempt,
                "status": cp_status.value,
                "verdict": verdict,
            },
        )
        return {
            "ok": True,
            "task_id": task.id,
            "node_id": target_node,
            "status": cp_status.value,
            "signaled": signaled,
            "checkpoint": cp.model_dump(mode="json"),
        }

    if name == "conclave_cascade_approve_task":
        task = await _require_task(daemon, _opt_str(args, "task_id"))
        approved_ids: list[str] = []
        visited: set[str] = {task.id}
        queue = [task]
        while queue and len(visited) <= 1000:
            cur = queue.pop(0)
            if cur.state in (
                TaskState.inbox,
                TaskState.failed,
                TaskState.blocked,
                TaskState.cancelled,
            ):
                await repo.set_task_state(daemon.db, cur.id, TaskState.approved)
                await daemon.bus.emit(
                    type=EventType.task_approved,
                    project_id=cur.project_id,
                    task_id=cur.id,
                    payload={"cascade": True, "root": task.id},
                )
                approved_ids.append(cur.id)
            for child in await repo.get_child_tasks(daemon.db, cur.id):
                if child.id not in visited:
                    visited.add(child.id)
                    queue.append(child)
        return {"approved": True, "task_ids": approved_ids, "count": len(approved_ids)}

    if name == "conclave_cancel_task":
        task = await _require_task(daemon, _opt_str(args, "task_id"))
        if task.state in (TaskState.inbox, TaskState.approved):
            await repo.set_task_state(daemon.db, task.id, TaskState.cancelled)
            await daemon.bus.emit(
                type=EventType.task_cancelled,
                project_id=task.project_id,
                task_id=task.id,
            )
            return {"cancelled": True, "task_id": task.id}
        if task.state in (TaskState.in_progress, TaskState.waiting_for_approval):
            cancelled = await daemon.request_cancel(task.id)
            if not cancelled and task.state == TaskState.waiting_for_approval:
                await repo.set_task_state(daemon.db, task.id, TaskState.cancelled)
                await daemon.bus.emit(
                    type=EventType.task_cancelled,
                    project_id=task.project_id,
                    task_id=task.id,
                )
                cancelled = True
            return {"cancelled": cancelled, "task_id": task.id}
        raise McpToolError(f"cannot cancel task in state {task.state.value}")

    if name == "conclave_steer_task":
        task = await _require_task(daemon, _opt_str(args, "task_id"))
        message = _opt_str(args, "message")
        if not message:
            raise McpToolError("message is required")
        node_id_arg = _opt_str(args, "node_id") or None
        edge_id_arg = _opt_str(args, "edge_id") or None
        if node_id_arg or edge_id_arg:
            if task.state in (TaskState.done, TaskState.cancelled):
                raise McpToolError(f"cannot steer task in state {task.state.value}")
            await daemon.request_steer(
                task.id, message, node_id=node_id_arg, edge_id=edge_id_arg
            )
            await daemon.bus.emit(
                type=EventType.operator_steer,
                project_id=task.project_id,
                task_id=task.id,
                payload={"message": message, "node_id": node_id_arg, "edge_id": edge_id_arg},
            )
            return {
                "steered": True,
                "task_id": task.id,
                "node_id": node_id_arg,
                "edge_id": edge_id_arg,
            }
        if task.state == TaskState.waiting_for_approval:
            await daemon.request_steer(task.id, message, node_id="developer")
            await daemon.bus.emit(
                type=EventType.operator_steer,
                project_id=task.project_id,
                task_id=task.id,
                payload={"message": message},
            )
            return {"steered": True, "task_id": task.id}
        if task.state is not TaskState.in_progress:
            raise McpToolError(f"task is in state {task.state.value}, not in_progress")
        queued = await daemon.request_steer(task.id, message)
        if not queued:
            raise McpToolError("task is no longer in-flight")
        return {"steered": True, "task_id": task.id}

    if name == "conclave_delete_task":
        task = await _require_task(daemon, _opt_str(args, "task_id"))
        if task.state is TaskState.in_progress:
            raise McpToolError("cannot delete an in_progress task — cancel it first")
        deleted = await repo.delete_task(daemon.db, task.id)
        if not deleted:
            raise McpToolError("task could not be deleted")
        await daemon.bus.emit(
            type=EventType.task_deleted, project_id=task.project_id, task_id=task.id
        )
        return {"deleted": task.id}

    if name == "conclave_get_task_events":
        task = await _require_task(daemon, _opt_str(args, "task_id"))
        after_id = _opt_int(args, "after_id", 0)
        events = await repo.list_events(daemon.db, task_id=task.id, after_id=after_id)
        return [e.model_dump(mode="json") for e in events]

    if name == "conclave_get_task_verdicts":
        task = await _require_task(daemon, _opt_str(args, "task_id"))
        verdicts = await repo.list_verdicts(daemon.db, task.id, limit=200)
        return [v.model_dump(mode="json") for v in verdicts]

    if name == "conclave_get_task_checkpoints":
        task = await _require_task(daemon, _opt_str(args, "task_id"))
        checkpoints = await repo.list_task_checkpoints(daemon.db, task.id)
        return [cp.model_dump(mode="json") for cp in checkpoints]

    if name == "conclave_get_task_diff":
        task = await _require_task(daemon, _opt_str(args, "task_id"))
        stat_only = _opt_bool(args, "stat_only", False)
        max_bytes = _opt_int(args, "max_bytes", DEFAULT_DIFF_MAX_BYTES)
        if not 1 <= max_bytes <= MAX_DIFF_MAX_BYTES:
            raise McpToolError(
                f"max_bytes must be between 1 and {MAX_DIFF_MAX_BYTES}"
            )
        project = await _require_project(daemon, task.project_id)
        try:
            return await compute_task_diff(
                daemon.db,
                daemon.home,
                project,
                task,
                stat_only=stat_only,
                max_bytes=max_bytes,
            )
        except TaskDiffError as exc:
            raise McpToolError(exc.detail) from exc

    if name == "conclave_merge_task":
        task = await _require_task(daemon, _opt_str(args, "task_id"))
        project = await _require_project(daemon, task.project_id)
        try:
            return await merge_task(daemon.orchestrator, project, task)
        except TaskMergeError as exc:
            raise McpToolError(exc.detail) from exc

    # --- Dynamic Agent Diagram (Workflow Graph) ---
    if name == "conclave_get_workflow":
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        config = await resolve_project_config(daemon.db, project)
        active = config.workflow or build_default_workflow(
            config.agents,
            parallel_reviewers=config.execution.parallel_reviewers,
        )
        return {
            "workflow": active.model_dump(mode="json"),
            "is_custom": config.workflow is not None,
            "templates": {
                k: v.model_dump(mode="json")
                for k, v in workflow_templates(config.agents).items()
            },
        }

    if name == "conclave_apply_workflow_template":
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        template_id = _opt_str(args, "template_id")
        config = await resolve_project_config(daemon.db, project)
        templates = workflow_templates(config.agents)
        if template_id not in templates:
            raise McpToolError(f"unknown template_id: {template_id}")
        wf_dump = templates[template_id].model_dump(mode="json")
        # "standard" derives the graph from the agents policy, even over a global workflow.
        await save_project_config(
            daemon.db, project, {"workflow": None if template_id == "standard" else wf_dump}
        )
        updated_proj = await _require_project(daemon, project.id)
        await daemon.bus.emit(
            type=EventType.workflow_updated,
            project_id=project.id,
            payload={
                "applied_template": template_id,
                "workflow": wf_dump,
                "is_custom": template_id != "standard",
            },
        )
        await daemon.bus.emit(
            type=EventType.project_updated,
            project_id=project.id,
            payload={"project": updated_proj.model_dump(mode="json")},
        )
        return {
            "applied_template": template_id,
            "workflow": wf_dump,
        }

    if name == "conclave_set_workflow":
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        wf_raw = args.get("workflow")
        if not isinstance(wf_raw, dict):
            raise McpToolError("workflow must be a JSON object")
        graph = WorkflowGraph.model_validate(wf_raw)
        return await _save_workflow(daemon, project, graph)

    if name in ("conclave_workflow_upsert_node", "conclave_upsert_workflow_node"):
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        current = await _resolve_active_workflow(daemon, project)
        node_id = _opt_str(args, "id")
        existing_node = next((n for n in current.nodes if n.id == node_id), None)
        kind = NodeKind(
            _opt_str(
                args,
                "kind",
                existing_node.kind.value if existing_node else "reviewer",
            )
        )
        raw_triggers = args.get("triggers")
        triggers = (
            [str(t) for t in raw_triggers if t is not None]
            if isinstance(raw_triggers, list)
            else (list(existing_node.triggers) if existing_node else [])
        )
        raw_skills = args.get("skills")
        skills = (
            [str(s) for s in raw_skills if s is not None]
            if isinstance(raw_skills, list)
            else (list(existing_node.skills) if existing_node else [])
        )
        model_val = (
            _opt_str(args, "model") or None
            if args.get("model") is not None
            else (existing_node.model if existing_node else None)
        )
        effort_val = (
            _opt_str(args, "effort") or None
            if args.get("effort") is not None
            else (existing_node.effort if existing_node else None)
        )
        cli_agent_val = (
            _opt_str(args, "cli_agent") or None
            if args.get("cli_agent") is not None
            else (existing_node.cli_agent if existing_node else None)
        )
        thinking_val = (
            bool(args["thinking"])
            if isinstance(args.get("thinking"), bool)
            else (existing_node.thinking if existing_node else None)
        )
        compact_val = (
            bool(args["compact_before_start"])
            if isinstance(args.get("compact_before_start"), bool)
            else (existing_node.compact_before_start if existing_node else None)
        )
        has_pause_arg = (
            args.get("pause_after") is not None
            or args.get("await_human_approval") is not None
        )
        pause_val = (
            bool(
                _opt_bool(args, "pause_after", False)
                or _opt_bool(args, "await_human_approval", False)
            )
            if has_pause_arg
            else (existing_node.pause_after if existing_node else False)
        )
        raw_pos_x = args.get("position_x")
        pos_x = (
            float(raw_pos_x)
            if raw_pos_x is not None
            else (existing_node.position_x if existing_node else 560.0)
        )
        raw_pos_y = args.get("position_y")
        pos_y = (
            float(raw_pos_y)
            if raw_pos_y is not None
            else (existing_node.position_y if existing_node else 220.0)
        )
        new_node = WorkflowNode(
            id=node_id,
            label=_opt_str(
                args,
                "label",
                existing_node.label if existing_node else node_id,
            ),
            kind=kind,
            agent=(
                _opt_str(args, "agent")
                or (existing_node.agent if existing_node else node_id)
            ),
            output_contract=OutputContract(
                _opt_str(
                    args,
                    "output_contract",
                    existing_node.output_contract.value
                    if existing_node
                    else "verdict",
                )
            ),
            prompt_template=(
                str(args["prompt_template"])
                if args.get("prompt_template") is not None
                else (existing_node.prompt_template if existing_node else None)
            ),
            triggers=triggers,
            optional=_opt_bool(
                args,
                "optional",
                existing_node.optional if existing_node else False,
            ),
            enabled=_opt_bool(
                args, "enabled", existing_node.enabled if existing_node else True
            ),
            position_x=pos_x,
            position_y=pos_y,
            model=model_val,
            effort=effort_val,
            cli_agent=cli_agent_val,
            skills=skills,
            thinking=thinking_val,
            compact_before_start=compact_val,
            pause_after=pause_val,
            await_human_approval=pause_val,
        )
        nodes = [n for n in current.nodes if n.id != node_id] + [new_node]
        updated_graph = WorkflowGraph(
            version=current.version,
            name=current.name,
            description=current.description,
            template_id=current.template_id,
            parallel_reviewers=current.parallel_reviewers,
            nodes=nodes,
            edges=current.edges,
        )
        return await _save_workflow(daemon, project, updated_graph)

    if name in ("conclave_workflow_remove_node", "conclave_remove_workflow_node"):
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        node_id = _opt_str(args, "node_id")
        current = await _resolve_active_workflow(daemon, project)
        nodes = [n for n in current.nodes if n.id != node_id]
        edges = [
            e for e in current.edges if e.source != node_id and e.target != node_id
        ]
        updated_graph = WorkflowGraph(
            version=current.version,
            name=current.name,
            description=current.description,
            template_id=current.template_id,
            parallel_reviewers=current.parallel_reviewers,
            nodes=nodes,
            edges=edges,
        )
        return await _save_workflow(daemon, project, updated_graph)

    if name in ("conclave_workflow_upsert_edge", "conclave_upsert_workflow_edge"):
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        source = _opt_str(args, "source")
        target = _opt_str(args, "target")
        condition = EdgeCondition(_opt_str(args, "condition", "always"))
        edge_id = _opt_str(args, "id") or f"e-{source}-{target}-{condition.value}"
        require_human = _opt_bool(args, "require_human", False)
        new_edge = WorkflowEdge(
            id=edge_id,
            source=source,
            target=target,
            condition=condition,
            max_iterations=_opt_int(args, "max_iterations", 3),
            label=_opt_str(args, "label"),
            require_human=require_human,
        )
        current = await _resolve_active_workflow(daemon, project)
        edges = [
            e
            for e in current.edges
            if e.id != edge_id
            and not (
                e.source == source and e.target == target and e.condition == condition
            )
        ] + [new_edge]
        updated_graph = WorkflowGraph(
            version=current.version,
            name=current.name,
            description=current.description,
            template_id=current.template_id,
            parallel_reviewers=current.parallel_reviewers,
            nodes=current.nodes,
            edges=edges,
        )
        return await _save_workflow(daemon, project, updated_graph)

    if name in ("conclave_workflow_remove_edge", "conclave_remove_workflow_edge"):
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        edge_id = _opt_str(args, "edge_id")
        source = _opt_str(args, "source")
        target = _opt_str(args, "target")
        current = await _resolve_active_workflow(daemon, project)
        edges = [
            e
            for e in current.edges
            if not (
                (edge_id and e.id == edge_id)
                or (source and target and e.source == source and e.target == target)
            )
        ]
        updated_graph = WorkflowGraph(
            version=current.version,
            name=current.name,
            description=current.description,
            template_id=current.template_id,
            parallel_reviewers=current.parallel_reviewers,
            nodes=current.nodes,
            edges=edges,
        )
        return await _save_workflow(daemon, project, updated_graph)

    # --- Agent Personas ---
    if name == "conclave_list_agents":
        pid_opt = _opt_str(args, "project_id") or None
        agents = await repo.list_agents(daemon.db, pid_opt, limit=200)
        return [a.model_dump(mode="json") for a in agents]

    if name == "conclave_upsert_agent":
        agent_name = _opt_str(args, "name")
        persona_md = _opt_str(args, "persona_md")
        role = _opt_str(args, "role", "conditional") or "conditional"
        pid_opt = _opt_str(args, "project_id") or None
        if not agent_name or not persona_md:
            raise McpToolError("name and persona_md are required")
        agent = await repo.upsert_agent(
            daemon.db,
            name=agent_name,
            role=role,
            persona_md=persona_md,
            project_id=pid_opt,
        )
        agent_json = agent.model_dump(mode="json")
        await daemon.bus.emit(
            type=EventType.agent_upserted,
            project_id=pid_opt,
            payload={"agent": agent_json},
        )
        if pid_opt is None:
            first_line = next(
                (
                    line.lstrip("# ").strip()
                    for line in persona_md.splitlines()
                    if line.strip()
                ),
                agent_name,
            )
            store_item = await repo.upsert_store_item(
                daemon.db,
                kind=StoreItemKind.agent,
                name=agent_name,
                description=f"{first_line} ({role} role)",
                tags=[role, "agent"],
                payload={"name": agent_name, "role": role, "persona_md": persona_md},
                built_in=agent_name in DEFAULT_PERSONAS,
            )
            await daemon.bus.emit(
                type=EventType.store_upserted,
                payload={"item": store_item.model_dump(mode="json")},
            )
        return agent_json

    if name == "conclave_delete_agent":
        agent_name = _opt_str(args, "name")
        pid_opt = _opt_str(args, "project_id") or None
        deleted = await repo.delete_agent(daemon.db, name=agent_name, project_id=pid_opt)
        if pid_opt is None and agent_name in DEFAULT_PERSONAS:
            default_role, default_md = DEFAULT_PERSONAS[agent_name]
            reset_agent = await repo.upsert_agent(
                daemon.db,
                name=agent_name,
                role=default_role.value,
                persona_md=default_md,
                project_id=None,
            )
            await daemon.bus.emit(
                type=EventType.agent_deleted,
                project_id=None,
                payload={"name": agent_name, "reset_to_default": True},
            )
            await daemon.bus.emit(
                type=EventType.agent_upserted,
                project_id=None,
                payload={"agent": reset_agent.model_dump(mode="json")},
            )
            return {"reset_to_default": agent_name}
        if not deleted:
            raise McpToolError(f"agent not found: {agent_name}")
        if pid_opt is None:
            existing_store = await repo.get_store_item_by_name(
                daemon.db, StoreItemKind.agent, agent_name
            )
            if existing_store is not None and not existing_store.built_in:
                await repo.delete_store_item(daemon.db, existing_store.id)
                await daemon.bus.emit(
                    type=EventType.store_deleted,
                    payload={"id": existing_store.id, "name": agent_name},
                )
        await daemon.bus.emit(
            type=EventType.agent_deleted,
            project_id=pid_opt,
            payload={"name": agent_name, "project_id": pid_opt},
        )
        return {"deleted": agent_name}

    # --- Multi-Agent Planning Sessions ---
    if name == "conclave_list_planning_sessions":
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        sessions = await repo.list_planning_sessions(daemon.db, project.id, limit=50)
        return [s.model_dump(mode="json") for s in sessions]

    if name == "conclave_create_planning_session":
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        prompt = _opt_str(args, "prompt")
        if not prompt:
            raise McpToolError("prompt is required")
        title = _opt_str(args, "title")
        max_rounds = _opt_int(args, "max_rounds", 5)
        session = await daemon.planning_orchestrator.create_and_start(
            project_id=project.id,
            title=title,
            prompt=prompt,
            max_rounds=max_rounds,
        )
        return session.model_dump(mode="json")

    if name == "conclave_get_planning_session":
        sid = _opt_str(args, "session_id")
        session_row = await repo.get_planning_session(daemon.db, sid)
        if session_row is None:
            raise McpToolError(f"planning session not found: {sid}")
        messages = await repo.list_planning_messages(daemon.db, sid, limit=200)
        task_nodes = await repo.list_planning_task_nodes(daemon.db, sid, limit=200)
        return {
            "session": session_row.model_dump(mode="json"),
            "messages": [m.model_dump(mode="json") for m in messages],
            "tasks": [n.model_dump(mode="json") for n in task_nodes],
        }

    if name == "conclave_interject_planning_message":
        sid = _opt_str(args, "session_id")
        content = _opt_str(args, "content")
        if not sid or not content:
            raise McpToolError("session_id and content are required")
        msg = await daemon.planning_orchestrator.add_human_message(sid, content)
        return msg.model_dump(mode="json")

    if name == "conclave_approve_planning_session":
        sid = _opt_str(args, "session_id")
        task_ids = await daemon.planning_orchestrator.approve_session(sid)
        return {"approved": True, "task_ids": task_ids, "count": len(task_ids)}

    if name == "conclave_cancel_planning_session":
        sid = _opt_str(args, "session_id")
        await daemon.planning_orchestrator.cancel_session(sid)
        return {"cancelled": True, "session_id": sid}

    # --- Bug-Fixer & Quarantine ---
    if name == "conclave_list_bug_candidates":
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        if _opt_bool(args, "needs_human_only", False):
            cands = await repo.list_needs_human(daemon.db, project.id)
        else:
            cands = await repo.list_bug_candidates(daemon.db, project.id, limit=100)
        return [c.model_dump(mode="json") for c in cands]

    if name == "conclave_list_quarantine":
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        entries = await repo.list_quarantine(daemon.db, project.id, limit=100)
        health = await quarantine_integrity(daemon.db, project.id)
        return {
            "integrity": health,
            "entries": [q.model_dump(mode="json") for q in entries],
        }

    if name == "conclave_add_quarantine":
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        pattern = _opt_str(args, "pattern")
        reason = _opt_str(args, "reason")
        until = _opt_str(args, "until")
        date.fromisoformat(until)
        entry = await repo.add_quarantine(
            daemon.db,
            project_id=project.id,
            pattern=pattern,
            reason=reason,
            until=until,
        )
        return entry.model_dump(mode="json")

    if name == "conclave_delete_quarantine":
        eid = _opt_str(args, "entry_id")
        await repo.delete_quarantine(daemon.db, eid)
        return {"deleted": eid}

    # --- Global Store ---
    if name == "conclave_store_list":
        raw_kind = _opt_str(args, "kind")
        store_kind_filter = StoreItemKind(raw_kind) if raw_kind else None
        items = await repo.list_store_items(daemon.db, kind=store_kind_filter, limit=200)
        return [item.model_dump(mode="json") for item in items]

    if name in ("conclave_store_publish", "conclave_store_upsert_item"):
        store_kind = StoreItemKind(_opt_str(args, "kind", "workflow"))
        item_name = _opt_str(args, "name")
        description = _opt_str(args, "description")
        raw_tags = args.get("tags")
        tags = [str(t) for t in raw_tags if t is not None] if isinstance(raw_tags, list) else []
        raw_payload = args.get("payload")
        if not item_name or not isinstance(raw_payload, dict):
            raise McpToolError("name and object payload are required")
        payload = normalize_config_keys(raw_payload)
        if store_kind is StoreItemKind.workflow:
            WorkflowGraph.model_validate(payload)
        elif store_kind is StoreItemKind.config_preset:
            load_project_config(payload)
        elif store_kind is StoreItemKind.agent:
            ag_role = str(payload.get("role") or "conditional")
            ag_md = str(payload.get("persona_md") or "").strip()
            if not ag_md:
                raise McpToolError("agent payload must include non-empty persona_md")
            upserted_ag = await repo.upsert_agent(
                daemon.db,
                name=item_name,
                role=ag_role,
                persona_md=ag_md,
                project_id=None,
            )
            await daemon.bus.emit(
                type=EventType.agent_upserted,
                project_id=None,
                payload={"agent": upserted_ag.model_dump(mode="json")},
            )
        saved = await repo.upsert_store_item(
            daemon.db,
            kind=store_kind,
            name=item_name,
            description=description,
            tags=tags,
            payload=payload,
            built_in=False,
        )
        saved_json = saved.model_dump(mode="json")
        await daemon.bus.emit(
            type=EventType.store_upserted,
            payload={"item": saved_json},
        )
        return saved_json

    if name in ("conclave_store_delete", "conclave_store_delete_item"):
        item_id = _opt_str(args, "item_id") or _opt_str(args, "id")
        existing_item = await repo.get_store_item(daemon.db, item_id)
        if existing_item is None:
            raise McpToolError(f"store item not found: {item_id}")
        if existing_item.built_in:
            raise McpToolError("cannot delete a built-in store item")
        await repo.delete_store_item(daemon.db, item_id)
        await daemon.bus.emit(
            type=EventType.store_deleted,
            payload={"id": item_id},
        )
        return {"deleted": item_id}

    if name in ("conclave_store_install", "conclave_store_install_item"):
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        item: StoreItem | None = None
        req_item_id = _opt_str(args, "item_id")
        req_kind = _opt_str(args, "kind")
        req_name = _opt_str(args, "name")
        if req_item_id:
            item = await repo.get_store_item(daemon.db, req_item_id)
        elif req_kind and req_name:
            item = await repo.get_store_item_by_name(
                daemon.db,
                StoreItemKind(req_kind),
                req_name,
            )
        if item is None:
            raise McpToolError("store item not found (specify item_id or kind + name)")

        await daemon.bus.emit(
            type=EventType.store_upserted,
            payload={"item": item.model_dump(mode="json"), "project_id": project.id},
        )

        if item.kind is StoreItemKind.workflow:
            resolved = await save_project_config(daemon.db, project, {"workflow": item.payload})
            wf_out = (resolved.workflow or build_default_workflow()).model_dump(
                mode="json"
            )
            updated_proj = await _require_project(daemon, project.id)
            await daemon.bus.emit(
                type=EventType.workflow_updated,
                project_id=project.id,
                payload={"workflow": wf_out, "is_custom": True},
            )
            await daemon.bus.emit(
                type=EventType.project_updated,
                project_id=project.id,
                payload={"project": updated_proj.model_dump(mode="json")},
            )
            return {
                "installed": item.name,
                "kind": item.kind.value,
                "workflow": wf_out,
            }
        if item.kind is StoreItemKind.config_preset:
            resolved = await save_project_config(daemon.db, project, item.payload)
            updated_proj = await _require_project(daemon, project.id)
            resolved_json = resolved.model_dump(mode="json")
            await daemon.bus.emit(
                type=EventType.project_updated,
                project_id=project.id,
                payload={
                    "project": updated_proj.model_dump(mode="json"),
                    "config": resolved_json,
                },
            )
            return {
                "installed": item.name,
                "kind": item.kind.value,
                "config": resolved_json,
            }
        role = str(item.payload.get("role") or "conditional")
        persona_md = str(item.payload.get("persona_md") or "")
        ag_name = str(item.payload.get("name") or item.name)
        persona = await repo.upsert_agent(
            daemon.db,
            name=ag_name,
            role=role,
            persona_md=persona_md,
            project_id=project.id,
        )
        persona_json = persona.model_dump(mode="json")
        await daemon.bus.emit(
            type=EventType.agent_upserted,
            project_id=project.id,
            payload={"agent": persona_json},
        )
        return {
            "installed": item.name,
            "kind": item.kind.value,
            "agent": persona_json,
        }

    # --- Parallel Swarms ---
    if name == "conclave_launch_swarm":
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        raw_units = args.get("units")
        units_list = (
            [u for u in raw_units if isinstance(u, dict)]
            if isinstance(raw_units, list)
            else None
        )
        swarm_prompt = _opt_str(args, "prompt") or None
        concurrency = _opt_int(args, "concurrency", 4)
        auto_approve = _opt_bool(args, "auto_approve", True)
        return await daemon.launch_swarm(
            project.id,
            units=units_list,
            prompt=swarm_prompt,
            concurrency=concurrency,
            auto_approve=auto_approve,
        )

    if name == "conclave_get_swarm_status":
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        swarm_id = _opt_str(args, "swarm_id")
        if not swarm_id:
            raise McpToolError("swarm_id is required")
        tasks = await repo.list_swarm_tasks(daemon.db, project.id, swarm_id)
        counts: dict[str, int] = {}
        for t in tasks:
            counts[t.state.value] = counts.get(t.state.value, 0) + 1
        return {
            "swarm_id": swarm_id,
            "project_id": project.id,
            "total": len(tasks),
            "counts": counts,
            "tasks": [t.model_dump(mode="json") for t in tasks],
        }

    # --- Agent Execution Settings, Context Compaction & Observability ---
    if name == "conclave_configure_agent":
        raw_pid = _opt_str(args, "project_id")
        raw_agent_target = _opt_str(args, "agent")
        agent_target = raw_agent_target.strip('"').strip("'").strip()
        settings_patch: dict[str, Any] = {}
        for field_name in (
            "model",
            "effort",
            "cli_agent",
            "skills",
            "thinking",
            "pure",
            "compact_before_start",
            "timeout_minutes",
            "max_retries",
        ):
            if field_name in args and args[field_name] is not None:
                settings_patch[field_name] = args[field_name]

        is_defaults = not agent_target or agent_target in ("default", "defaults", "*")
        target_label = "agent_defaults" if is_defaults else agent_target
        agent_patch: dict[str, Any] = (
            {"agent_defaults": settings_patch}
            if is_defaults
            else {"agent_overrides": {agent_target: settings_patch}}
        )

        if not raw_pid or raw_pid in ("global", "default", "defaults", "*"):
            # Every project inherits the new global value: clearing the configured fields from
            # each project's delta (instead of copying them in) keeps nothing frozen.
            resolved_global, updated_projects = await save_global_config(
                daemon.db, agent_patch, apply_to_all_projects=True
            )
            await _emit_projects_updated(daemon, updated_projects)
            return {
                "project_id": "global",
                "target": target_label,
                "agent": target_label,
                "applied": settings_patch,
                "settings": settings_patch,
                "agent_defaults": resolved_global.agent_defaults.model_dump(mode="json"),
                "agent_overrides": resolved_global.agent_overrides,
            }

        project = await _require_project(daemon, raw_pid)
        resolved = await save_project_config(daemon.db, project, agent_patch)
        updated_proj = await _require_project(daemon, project.id)
        await daemon.bus.emit(
            type=EventType.project_updated,
            project_id=project.id,
            payload={
                "project": updated_proj.model_dump(mode="json"),
                "config": resolved.model_dump(mode="json"),
            },
        )
        return {
            "project_id": project.id,
            "target": target_label,
            "agent": target_label,
            "applied": settings_patch,
            "settings": settings_patch,
            "agent_defaults": resolved.agent_defaults.model_dump(mode="json"),
            "agent_overrides": resolved.agent_overrides,
        }

    if name == "conclave_compact_session":
        tid = _opt_str(args, "task_id")
        psid = _opt_str(args, "planning_session_id") or _opt_str(args, "session_id")
        if tid:
            task = await _require_task(daemon, tid)
            result_payload = await compact_task_context(daemon.db, task.id)
            await daemon.bus.emit(
                type=EventType.context_compacted,
                project_id=task.project_id,
                task_id=task.id,
                payload=result_payload,
            )
            return result_payload

        if psid:
            session_row = await repo.get_planning_session(daemon.db, psid)
            if session_row is None:
                raise McpToolError(f"planning session not found: {psid}")
            ps_payload = await compact_planning_session_context(daemon.db, psid)
            await daemon.bus.emit(
                type=EventType.context_compacted,
                project_id=session_row.project_id,
                planning_session_id=session_row.id,
                payload=ps_payload,
            )
            return ps_payload

        raise McpToolError("task_id or planning_session_id is required")

    if name == "conclave_list_interactions":
        pid_filter = _opt_str(args, "project_id") or None
        tid_filter = _opt_str(args, "task_id") or None
        agent_filter = _opt_str(args, "agent") or None
        model_filter = _opt_str(args, "model") or None
        search_filter = _opt_str(args, "search") or None
        limit = max(1, min(_opt_int(args, "limit", 50), 500))
        logs = await repo.list_interaction_logs(
            daemon.db,
            project_id=pid_filter,
            task_id=tid_filter,
            agent=agent_filter,
            model=model_filter,
            search=search_filter,
            limit=limit,
        )
        out_list: list[dict[str, Any]] = []
        for log_entry in logs:
            d = log_entry.model_dump(mode="json")
            d["message_count"] = log_entry.message_count or len(log_entry.messages)
            d["messages"] = []
            out_list.append(d)
        return out_list

    if name == "conclave_get_interaction":
        sid = _opt_str(args, "session_id")
        if not sid:
            raise McpToolError("session_id is required")
        log_row = await repo.get_interaction_log(daemon.db, sid)
        if log_row is None:
            raise McpToolError(f"interaction session not found: {sid}")
        return log_row.model_dump(mode="json")

    if name == "conclave_query_interactions":
        sql_query = _opt_str(args, "sql")
        limit = max(1, min(_opt_int(args, "limit", 200), 5000))
        return await repo.query_interaction_logs_readonly(
            daemon.db, sql_query, limit=limit
        )

    if name == "conclave_get_observability_stats":
        pid_filter = _opt_str(args, "project_id") or None
        if pid_filter in ("global", "default", "defaults", "*"):
            pid_filter = None
        stats = await repo.interaction_stats(daemon.db, project_id=pid_filter)
        if _opt_bool(args, "export_parquet", False):
            stats["parquet"] = await export_interaction_logs_to_parquet(daemon.db)
        return stats

    if name == "conclave_get_reload_status":
        return await reload_status_report(daemon)

    if name == "conclave_reload_system":
        force = _opt_bool(args, "force", False)
        return await daemon.reload_now(force=force)

    if name == "conclave_evolve_workflow":
        project = await _require_project(daemon, _opt_str(args, "project_id"))
        tid_val = _opt_str(args, "task_id") or None
        gate_failed = _opt_bool(args, "gate_failed_after_review", False)
        task_failed = _opt_bool(args, "task_failed", False)
        return await evolve_project_workflow(
            daemon.db,
            daemon.bus,
            project.id,
            task_id=tid_val,
            gate_failed_after_review=gate_failed,
            task_failed=task_failed,
        )

    if name == "conclave_get_pilot_guide":
        section = _opt_str(args, "section")
        guide = load_pilot_guide()
        if section:
            sections = _guide_sections(guide)
            matched = _match_guide_section(sections, section)
            if matched is None:
                headings = [
                    f"{number}. {title}" if number is not None else title
                    for number, title, _ in sections
                ]
                raise McpToolError(
                    f"unknown section {section!r}; available sections: " + "; ".join(headings)
                )
            body = matched[2]
        else:
            body = guide
        return body.rstrip() + "\n\n" + _live_environment_footer(daemon)

    raise McpToolError(f"unknown MCP tool: {name!r}")
