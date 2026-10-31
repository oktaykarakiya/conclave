"""REST API — the sole control surface for Conclave.

Thin handlers over the repository layer + runtime. Model/provider selection and auth
are owned by opencode; repo context comes from each project's AGENTS.md, so there are
no engine-profile, secret, or repo-knowledge endpoints here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from .. import __version__
from ..agents import DEFAULT_PERSONAS
from ..config import (
    ConclaveConfig,
    WorkflowGraph,
    build_default_workflow,
    config_schema,
    deep_merge,
    load_project_config,
    normalize_config_keys,
    workflow_templates,
)
from ..db import (
    AgentPersona,
    BugCandidate,
    CheckpointStatus,
    EventRow,
    Project,
    QuarantineEntry,
    StoreItem,
    StoreItemKind,
    Task,
    TaskCheckpoint,
    TaskState,
    VerdictRow,
)
from ..db import repositories as repo
from ..db.planning_models import PlanningMessage, PlanningSession, PlanningTaskNode
from ..engine.compactor import compact_planning_session_context, compact_task_context
from ..engine.task_diff import (
    DEFAULT_DIFF_MAX_BYTES,
    MAX_DIFF_MAX_BYTES,
    TaskDiffError,
    compute_task_diff,
)
from ..engine.workflow_evolver import evolve_project_workflow
from ..events import EventType
from ..observability.parquet_sync import export_interaction_logs_to_parquet
from ..providers import CLAUDE_CODE_PREFIX, Engine
from ..runtime import Daemon
from ..verification import quarantine_integrity
from .deps import get_daemon
from .schemas import (
    AgentUpsert,
    ConfigPatch,
    HitlToggleInput,
    NodeCompleteInput,
    PaginationParams,
    PlanningMessageInput,
    PlanningSessionCreate,
    ProjectCreate,
    ProjectModeUpdate,
    QuarantineInput,
    SteerInput,
    StoreInstallInput,
    StoreItemUpsert,
    SwarmCreateInput,
    TaskCreate,
    WorkflowUpdate,
)


async def _pagination(
    limit: int = Query(50, ge=1, le=500, description="Max items per page"),
    offset: int = Query(0, ge=0, description="Items to skip"),
) -> PaginationParams:
    """Shared pagination dependency — bounds every list endpoint (DoS hardening — WEB-1)."""
    return PaginationParams(limit=limit, offset=offset)


logger = logging.getLogger("conclave.web")

router = APIRouter(prefix="/api")


async def _require_project(daemon: Daemon, project_id: str) -> Project:
    project = await repo.get_project(daemon.db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


async def _require_task(daemon: Daemon, task_id: str) -> Task:
    task = await repo.get_task(daemon.db, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


async def _require_planning_session(
    daemon: Daemon, session_id: str
) -> PlanningSession:
    """Look up a planning session, raising 404 when missing.

    This mirrors _require_project: every session-scoped endpoint calls it first
    so a bogus session ID returns 404 instead of 500 (or an empty surrogate).
    """
    session = await repo.get_planning_session(daemon.db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="planning session not found")
    return session


# --- meta -------------------------------------------------------------------

_DEFAULT_MODEL = "deepseek/deepseek-flash"  # DeepSeek V4.1 Flash; keep in sync with AgentSettings

_RECOMMENDED_MODELS: list[str] = [
    _DEFAULT_MODEL,  # supersedes deepseek-v4-pro, which is intentionally not offered
    "google/gemini-2.5-flash-lite",
    "google/gemini-3.1-flash-lite-preview",
    "google/gemini-2.5-flash",
    "google/gemini-2.5-pro",
    "google/gemini-3-flash-preview",
    "google/gemini-3.1-pro-preview",
    "deepseek/deepseek-chat",
    "deepseek/deepseek-reasoner",
    "anthropic/claude-sonnet-4-5",
    # Claude Code CLI engine (its own login/subscription), routed by the prefix.
    f"{CLAUDE_CODE_PREFIX}default",
    f"{CLAUDE_CODE_PREFIX}opus",
    f"{CLAUDE_CODE_PREFIX}sonnet",
    f"{CLAUDE_CODE_PREFIX}haiku",
]
_EFFORT_VARIANTS: list[str] = ["low", "medium", "high", "xhigh", "max"]
_CLI_AGENTS: list[str] = ["build", "general", "explore", "plan"]
_SKILL_TEMPLATES: list[str] = [
    "tdd-strict",
    "minimal-diff",
    "security-hardening",
    "async-safety",
    "type-strict",
    "zero-regression",
]


class ObservabilityQueryInput(BaseModel):
    sql: str
    limit: int = Field(default=200, ge=1, le=5000)


class SystemReloadInput(BaseModel):
    force: bool = False


class WorkflowEvolveInput(BaseModel):
    task_id: str | None = None
    gate_failed_after_review: bool = False
    task_failed: bool = False


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get("/system/reload-status")
async def get_system_reload_status(
    daemon: Daemon = Depends(get_daemon),
) -> dict[str, Any]:
    return daemon.reload_status()


@router.post("/system/reload")
async def trigger_system_reload(
    body: SystemReloadInput | None = None,
    force: bool = Query(False),
    daemon: Daemon = Depends(get_daemon),
) -> dict[str, Any]:
    effective_force = bool(force or (body.force if body is not None else False))
    return await daemon.reload_now(force=effective_force)


@router.get("/config/schema")
async def get_config_schema() -> dict[str, Any]:
    return config_schema()


@router.get("/models")
async def get_models_catalog(daemon: Daemon = Depends(get_daemon)) -> dict[str, Any]:
    return {
        "default_model": _DEFAULT_MODEL,
        "models": list(_RECOMMENDED_MODELS),
        "recommended_models": list(_RECOMMENDED_MODELS),
        "efforts": list(_EFFORT_VARIANTS),
        "effort_variants": list(_EFFORT_VARIANTS),
        "cli_agents": list(_CLI_AGENTS),
        "skills": list(_SKILL_TEMPLATES),
        "skill_templates": list(_SKILL_TEMPLATES),
        "engines": _engine_status(daemon),
    }


def _engine_status(daemon: Daemon) -> dict[str, Any]:
    """Which engines this daemon can dispatch to (binary found on PATH / via override)."""
    default = getattr(daemon.provider, "default", Engine.opencode)
    bins = {
        Engine.opencode: os.environ.get("CONCLAVE_OPENCODE_BIN", "opencode"),
        Engine.claude: os.environ.get("CONCLAVE_CLAUDE_BIN", "claude"),
    }
    return {
        "default": str(default),
        "claude_code_prefix": CLAUDE_CODE_PREFIX,
        "available": {str(e): shutil.which(b) is not None for e, b in bins.items()},
    }


@router.get("/events/stream")
async def stream_events_sse(
    project_id: str | None = None,
    task_id: str | None = None,
    daemon: Daemon = Depends(get_daemon),
) -> StreamingResponse:
    norm_pid = project_id.strip() if project_id and project_id.strip() else None
    norm_tid = task_id.strip() if task_id and task_id.strip() else None
    subscriber = daemon.bus.subscribe(project_id=norm_pid, task_id=norm_tid)

    async def _event_generator() -> AsyncIterator[str]:
        try:
            with subscriber:
                async for event in subscriber:
                    data = json.dumps(event.model_dump(mode="json"))
                    yield f"data: {data}\n\n"
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --- projects ---------------------------------------------------------------


@router.get("/projects")
async def list_projects(
    pagination: PaginationParams = Depends(_pagination),
    daemon: Daemon = Depends(get_daemon),
) -> list[Project]:
    return await repo.list_projects(
        daemon.db, limit=pagination.limit, offset=pagination.offset
    )


@router.post("/projects")
async def create_project(
    body: ProjectCreate, daemon: Daemon = Depends(get_daemon)
) -> Project:
    path = Path(body.path).expanduser()
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"path is not a directory: {path}")
    if not (path / ".git").exists():
        raise HTTPException(status_code=400, detail=f"not a git repository: {path}")

    initial_cfg: dict[str, Any] = {}
    global_preset = await repo.get_store_item_by_name(
        daemon.db, StoreItemKind.config_preset, "global-defaults"
    )
    if global_preset is not None:
        initial_cfg = deep_merge(initial_cfg, global_preset.payload)
    if body.config_preset:
        preset = await repo.get_store_item_by_name(
            daemon.db, StoreItemKind.config_preset, body.config_preset
        )
        if preset is not None:
            initial_cfg = deep_merge(initial_cfg, preset.payload)
    if body.workflow_template:
        wf_item = await repo.get_store_item_by_name(
            daemon.db, StoreItemKind.workflow, body.workflow_template
        )
        if wf_item is not None:
            initial_cfg["workflow"] = wf_item.payload
        else:
            templates = workflow_templates()
            if body.workflow_template in templates:
                initial_cfg["workflow"] = templates[body.workflow_template].model_dump(
                    mode="json"
                )
    if body.max_parallel_tasks is not None:
        exec_sec = dict(initial_cfg.get("execution") or {})
        exec_sec["max_parallel_tasks"] = body.max_parallel_tasks
        initial_cfg["execution"] = exec_sec

    default_branch = body.default_branch.strip() or "main"
    exec_cfg = dict(initial_cfg.get("execution") or {})
    if "target_branch" not in exec_cfg or default_branch != "main":
        exec_cfg["target_branch"] = default_branch
    initial_cfg["execution"] = exec_cfg

    existing = await repo.get_project_by_path(daemon.db, str(path))
    if existing is not None:
        merged_cfg = deep_merge(existing.config, initial_cfg)
        merged_exec = dict(merged_cfg.get("execution") or {})
        merged_exec["target_branch"] = default_branch
        merged_cfg["execution"] = merged_exec
        updated = await repo.update_project(
            daemon.db,
            existing.id,
            name=body.name.strip() or existing.name,
            default_branch=default_branch,
            config=merged_cfg,
        )
        assert updated is not None
        await daemon.start_worker(updated.id)
        await daemon.bus.emit(
            type=EventType.project_updated,
            project_id=updated.id,
            payload={"project": updated.model_dump(mode="json")},
        )
        return updated

    project = await repo.create_project(
        daemon.db,
        name=body.name,
        path=str(path),
        default_branch=default_branch,
        config=initial_cfg,
    )
    await daemon.start_worker(project.id)
    created = await _require_project(daemon, project.id)
    await daemon.bus.emit(
        type=EventType.project_created,
        payload={"project": created.model_dump(mode="json")},
    )
    return created


@router.get("/projects/{project_id}")
async def get_project(project_id: str, daemon: Daemon = Depends(get_daemon)) -> Project:
    return await _require_project(daemon, project_id)


@router.delete("/projects/{project_id}")
async def detach_project(project_id: str, daemon: Daemon = Depends(get_daemon)) -> dict[str, str]:
    await daemon.stop_worker(project_id)
    await daemon.cleanup_in_progress_work(project_id)
    await repo.delete_project(daemon.db, project_id)
    await daemon.bus.emit(
        type=EventType.project_deleted,
        payload={"project_id": project_id},
    )
    return {"detached": project_id}


@router.get("/config/global")
async def get_global_config(daemon: Daemon = Depends(get_daemon)) -> dict[str, Any]:
    global_item = await repo.get_store_item_by_name(
        daemon.db, StoreItemKind.config_preset, "global-defaults"
    )
    overrides = global_item.payload if global_item is not None else {}
    return load_project_config(overrides).model_dump(mode="json")


@router.patch("/config/global")
@router.put("/config/global")
async def patch_global_config(
    body: ConfigPatch, daemon: Daemon = Depends(get_daemon)
) -> dict[str, Any]:
    existing_item = await repo.get_store_item_by_name(
        daemon.db, StoreItemKind.config_preset, "global-defaults"
    )
    base_payload = existing_item.payload if existing_item is not None else {}
    patch_dict = normalize_config_keys(dict(body.config))
    if body.default_branch:
        exec_sec = dict(patch_dict.get("execution") or {})
        exec_sec["target_branch"] = body.default_branch.strip()
        patch_dict["execution"] = exec_sec
    merged_global = deep_merge(base_payload, patch_dict)
    try:
        resolved = load_project_config(merged_global)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc

    await repo.upsert_store_item(
        daemon.db,
        kind=StoreItemKind.config_preset,
        name="global-defaults",
        description="Global default settings applied across Conclave repositories.",
        tags=["global", "defaults"],
        payload=merged_global,
        built_in=False,
    )
    new_branch = (
        body.default_branch.strip()
        if body.default_branch and body.default_branch.strip()
        else (
            str(patch_dict["execution"]["target_branch"]).strip()
            if isinstance(patch_dict.get("execution"), dict)
            and patch_dict["execution"].get("target_branch")
            else None
        )
    )
    updated_projects: list[str] = []
    if body.apply_to_all_projects:
        for proj in await repo.list_projects(daemon.db):
            proj_merged = deep_merge(proj.config, patch_dict)
            await repo.update_project(
                daemon.db,
                proj.id,
                default_branch=new_branch or proj.default_branch,
                config=proj_merged,
            )
            updated_projects.append(proj.id)
    return {
        "ok": True,
        "global_config": resolved.model_dump(mode="json"),
        "updated_projects": updated_projects,
    }


@router.get("/projects/{project_id}/config")
async def get_config(project_id: str, daemon: Daemon = Depends(get_daemon)) -> dict[str, Any]:
    project = await _require_project(daemon, project_id)
    return load_project_config(project.config).model_dump(mode="json")


@router.patch("/projects/{project_id}/config")
@router.put("/projects/{project_id}/config")
async def patch_config(
    project_id: str, body: ConfigPatch, daemon: Daemon = Depends(get_daemon)
) -> dict[str, bool]:
    project = await _require_project(daemon, project_id)
    incoming = normalize_config_keys(dict(body.config))
    if "default_branch" in incoming and not body.default_branch:
        raw_db = incoming.pop("default_branch")
        if isinstance(raw_db, str) and raw_db.strip():
            body.default_branch = raw_db.strip()
    is_full_doc = {"execution", "experimental", "planning", "agents", "agent_defaults"} <= set(
        incoming.keys()
    )
    merged_cfg = incoming if is_full_doc else deep_merge(project.config, incoming)
    new_default_branch: str | None = (
        body.default_branch.strip()
        if body.default_branch and body.default_branch.strip()
        else None
    )
    if new_default_branch:
        exec_sec = dict(merged_cfg.get("execution") or {})
        exec_sec["target_branch"] = new_default_branch
        merged_cfg["execution"] = exec_sec
    elif isinstance(incoming.get("execution"), dict) and incoming["execution"].get("target_branch"):
        new_default_branch = str(incoming["execution"]["target_branch"]).strip()

    try:
        resolved = load_project_config(merged_cfg)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    await repo.update_project(
        daemon.db,
        project_id,
        name=body.name.strip() if body.name and body.name.strip() else None,
        default_branch=new_default_branch,
        config=merged_cfg,
    )
    updated_proj = await _require_project(daemon, project_id)
    await daemon.bus.emit(
        type=EventType.project_updated,
        project_id=project_id,
        payload={
            "project": updated_proj.model_dump(mode="json"),
            "config": resolved.model_dump(mode="json"),
        },
    )
    await daemon.bus.emit(
        type=EventType.workflow_updated,
        project_id=project_id,
        payload=_workflow_payload(resolved),
    )
    return {"ok": True}


@router.post("/projects/{project_id}/pause")
async def pause_project(project_id: str, daemon: Daemon = Depends(get_daemon)) -> dict[str, bool]:
    return {"paused": daemon.set_paused(project_id, True)}


@router.post("/projects/{project_id}/resume")
async def resume_project(project_id: str, daemon: Daemon = Depends(get_daemon)) -> dict[str, bool]:
    return {"running": daemon.set_paused(project_id, False)}


@router.get("/projects/{project_id}/status")
async def get_project_status(
    project_id: str, daemon: Daemon = Depends(get_daemon)
) -> dict[str, Any]:
    project = await _require_project(daemon, project_id)
    return {
        "project_id": project_id,
        "paused": daemon.is_paused(project_id),
        "mode": project.mode.value,
    }


def _workflow_payload(config: ConclaveConfig) -> dict[str, Any]:
    active = config.workflow or build_default_workflow(
        config.agents, parallel_reviewers=config.execution.parallel_reviewers
    )
    return {
        "workflow": active.model_dump(mode="json"),
        "is_custom": config.workflow is not None,
        "templates": {
            k: v.model_dump(mode="json")
            for k, v in workflow_templates(config.agents).items()
        },
    }


@router.get("/projects/{project_id}/workflow")
async def get_project_workflow(
    project_id: str, daemon: Daemon = Depends(get_daemon)
) -> dict[str, Any]:
    project = await _require_project(daemon, project_id)
    config = load_project_config(project.config)
    return _workflow_payload(config)


@router.put("/projects/{project_id}/workflow")
async def update_project_workflow(
    project_id: str, body: WorkflowUpdate, daemon: Daemon = Depends(get_daemon)
) -> dict[str, Any]:
    project = await _require_project(daemon, project_id)
    raw_cfg = dict(project.config)
    if body.template_id is not None:
        templates = workflow_templates(load_project_config(raw_cfg).agents)
        if body.template_id not in templates:
            raise HTTPException(
                status_code=400,
                detail=f"unknown workflow template: {body.template_id}",
            )
        if body.template_id == "standard":
            if body.workflow is not None:
                raw_cfg["workflow"] = body.workflow.model_dump(mode="json")
            else:
                raw_cfg.pop("workflow", None)
        else:
            raw_cfg["workflow"] = templates[body.template_id].model_dump(mode="json")
    elif body.workflow is not None:
        raw_cfg["workflow"] = body.workflow.model_dump(mode="json")
    else:
        raw_cfg.pop("workflow", None)
    try:
        resolved = load_project_config(raw_cfg)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    await repo.update_project_config(daemon.db, project_id, raw_cfg)
    wf_payload = _workflow_payload(resolved)
    updated_proj = await _require_project(daemon, project_id)
    await daemon.bus.emit(
        type=EventType.workflow_updated,
        project_id=project_id,
        payload=wf_payload,
    )
    await daemon.bus.emit(
        type=EventType.project_updated,
        project_id=project_id,
        payload={"project": updated_proj.model_dump(mode="json")},
    )
    return wf_payload


@router.post("/projects/{project_id}/workflow/evolve")
async def evolve_project_workflow_endpoint(
    project_id: str,
    body: WorkflowEvolveInput | None = None,
    daemon: Daemon = Depends(get_daemon),
) -> dict[str, Any]:
    await _require_project(daemon, project_id)
    return await evolve_project_workflow(
        daemon.db,
        daemon.bus,
        project_id,
        task_id=body.task_id if body is not None else None,
        gate_failed_after_review=(
            body.gate_failed_after_review if body is not None else False
        ),
        task_failed=body.task_failed if body is not None else False,
    )


@router.post("/projects/{project_id}/mode")
async def update_project_mode(
    project_id: str, body: ProjectModeUpdate, daemon: Daemon = Depends(get_daemon)
) -> Project:
    """Switch a project between ``task_queue`` and ``autonomous_bug_fixer``. The worker re-reads
    the project each tick, so the change takes effect on the next loop iteration."""
    await _require_project(daemon, project_id)
    await repo.set_project_mode(daemon.db, project_id, body.mode)
    updated = await _require_project(daemon, project_id)
    await daemon.bus.emit(
        type=EventType.project_updated,
        project_id=project_id,
        payload={"project": updated.model_dump(mode="json")},
    )
    return updated


@router.get("/projects/{project_id}/bug-candidates")
async def get_bug_candidates(
    project_id: str,
    pagination: PaginationParams = Depends(_pagination),
    daemon: Daemon = Depends(get_daemon),
) -> list[BugCandidate]:
    """The Bug-Fixer ledger for a project — every discovered/reproduced/fixed/… candidate."""
    await _require_project(daemon, project_id)
    return await repo.list_bug_candidates(
        daemon.db, project_id, limit=pagination.limit, offset=pagination.offset
    )


@router.get("/projects/{project_id}/needs-human")
async def get_needs_human(
    project_id: str, daemon: Daemon = Depends(get_daemon)
) -> list[BugCandidate]:
    """Candidates parked in ``declined_needs_human`` — the operator's review queue."""
    await _require_project(daemon, project_id)
    return await repo.list_needs_human(daemon.db, project_id)


@router.get("/projects/{project_id}/usage")
async def get_usage(project_id: str, daemon: Daemon = Depends(get_daemon)) -> dict[str, Any]:
    return await repo.usage_summary(daemon.db, project_id)


# --- quarantine -------------------------------------------------------------


@router.get("/projects/{project_id}/quarantine")
async def list_quarantine(
    project_id: str,
    pagination: PaginationParams = Depends(_pagination),
    daemon: Daemon = Depends(get_daemon),
) -> list[QuarantineEntry]:
    return await repo.list_quarantine(
        daemon.db, project_id, limit=pagination.limit, offset=pagination.offset
    )


@router.get("/projects/{project_id}/quarantine/integrity")
async def quarantine_health(
    project_id: str, daemon: Daemon = Depends(get_daemon)
) -> dict[str, Any]:
    return await quarantine_integrity(daemon.db, project_id)


@router.post("/projects/{project_id}/quarantine")
async def add_quarantine(
    project_id: str, body: QuarantineInput, daemon: Daemon = Depends(get_daemon)
) -> QuarantineEntry:
    await _require_project(daemon, project_id)
    return await repo.add_quarantine(
        daemon.db, project_id=project_id, pattern=body.pattern, reason=body.reason, until=body.until
    )


@router.delete("/quarantine/{entry_id}")
async def delete_quarantine(entry_id: str, daemon: Daemon = Depends(get_daemon)) -> dict[str, str]:
    await repo.delete_quarantine(daemon.db, entry_id)
    return {"deleted": entry_id}


# --- tasks ------------------------------------------------------------------


@router.get("/projects/{project_id}/tasks")
async def list_tasks(
    project_id: str,
    state: TaskState | None = None,
    pagination: PaginationParams = Depends(_pagination),
    daemon: Daemon = Depends(get_daemon),
) -> list[Task]:
    return await repo.list_tasks(
        daemon.db, project_id, state, limit=pagination.limit, offset=pagination.offset
    )


@router.post("/projects/{project_id}/tasks")
async def create_task(
    project_id: str, body: TaskCreate, daemon: Daemon = Depends(get_daemon)
) -> Task:
    await _require_project(daemon, project_id)
    state = TaskState.approved if body.auto_approve else TaskState.inbox
    task = await repo.create_task(
        daemon.db,
        project_id=project_id,
        request=body.request,
        title=body.title,
        use_planner=body.use_planner,
        state=state,
        source_branch=body.source_branch,
    )
    await daemon.bus.emit(
        type=EventType.task_created,
        project_id=project_id,
        task_id=task.id,
        payload={
            "title": task.title,
            "auto_approve": body.auto_approve,
            "source_branch": task.source_branch,
        },
    )
    return task


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, daemon: Daemon = Depends(get_daemon)) -> Task:
    return await _require_task(daemon, task_id)


@router.post("/tasks/{task_id}/approve")
async def approve_task(task_id: str, daemon: Daemon = Depends(get_daemon)) -> dict[str, str]:
    task = await _require_task(daemon, task_id)
    if task.state == TaskState.waiting_for_approval:
        signaled = daemon.resolve_node(task_id, "*", status="completed")
        if not signaled:
            await repo.approve_task(daemon.db, task_id)
        await daemon.bus.emit(type=EventType.task_approved, task_id=task_id)
        return {"approved": task_id}
    updated = await repo.approve_task(daemon.db, task_id)
    if not updated:
        raise HTTPException(
            status_code=409,
            detail=(
                "task is not in an approvable state "
                "(only inbox, failed, or waiting_for_approval tasks can be approved)"
            ),
        )
    await daemon.bus.emit(type=EventType.task_approved, task_id=task_id)
    return {"approved": task_id}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, daemon: Daemon = Depends(get_daemon)) -> dict[str, Any]:
    task = await _require_task(daemon, task_id)
    if task.state in (TaskState.inbox, TaskState.approved):
        await repo.set_task_state(daemon.db, task_id, TaskState.cancelled)
        await daemon.bus.emit(type=EventType.task_cancelled, task_id=task_id)
        return {"cancelled": True}
    if task.state in (TaskState.in_progress, TaskState.waiting_for_approval):
        cancelled = await daemon.request_cancel(task_id)
        if cancelled:
            return {"cancelled": True}
        if task.state == TaskState.waiting_for_approval:
            await repo.set_task_state(daemon.db, task_id, TaskState.cancelled)
            await daemon.bus.emit(type=EventType.task_cancelled, task_id=task_id)
            return {"cancelled": True}
        # The task finished between the state check and the cancel request — the
        # event was already cleaned up. Report what actually happened.
        updated = await repo.get_task(daemon.db, task_id)
        if updated is not None and updated.state == TaskState.cancelled:
            return {"cancelled": True}
        raise HTTPException(
            status_code=409,
            detail="task is no longer in_progress — refresh and retry if needed",
        )
    # Terminal states (done, failed, already cancelled) — clear error.
    raise HTTPException(
        status_code=409,
        detail=f"cannot cancel a task in state {task.state.value}",
    )


@router.post("/tasks/{task_id}/steer")
async def steer_task(
    task_id: str, body: SteerInput, daemon: Daemon = Depends(get_daemon)
) -> dict[str, Any]:
    """Inject operator guidance into an in-progress task's next dispatch (or specific node)."""
    task = await _require_task(daemon, task_id)
    has_target = bool(
        (body.node_id and body.node_id.strip()) or (body.edge_id and body.edge_id.strip())
    )
    if has_target:
        if task.state in (TaskState.done, TaskState.cancelled):
            raise HTTPException(
                status_code=409,
                detail=f"cannot steer a task in terminal state {task.state.value}",
            )
        await daemon.request_steer(
            task_id, body.message, node_id=body.node_id, edge_id=body.edge_id
        )
        target_label = body.node_id or body.edge_id or "task"
        log_row = await repo.record_interaction_log(
            daemon.db,
            source="operator",
            agent=str(target_label),
            model="operator/steer",
            title=f"[Steer] {target_label}: {body.message[:60]}",
            user_prompt=body.message,
            messages=[
                {"role": "user", "text": body.message},
                {
                    "role": "assistant",
                    "text": f"Queued live guidance for {target_label} on task {task.id}.",
                },
            ],
            tool_calls_count=1,
            message_count=2,
            tool_breakdown={"operator.steer": 1},
            project_id=task.project_id,
            task_id=task.id,
            node_id=body.node_id or body.edge_id,
        )
        await daemon.bus.emit(
            type=EventType.operator_steer,
            project_id=task.project_id,
            task_id=task.id,
            payload={
                "message": body.message,
                "node_id": body.node_id,
                "edge_id": body.edge_id,
                "session_id": log_row.session_id,
            },
        )
        await daemon.bus.emit(
            type=EventType.interaction_logged,
            project_id=task.project_id,
            task_id=task.id,
            payload={
                "session_id": log_row.session_id,
                "source": "operator",
                "agent": str(target_label),
                "node_id": body.node_id or body.edge_id,
                "project_id": task.project_id,
                "task_id": task.id,
            },
        )
        return {"steered": True, "node_id": body.node_id, "edge_id": body.edge_id}

    if task.state == TaskState.waiting_for_approval:
        await daemon.request_steer(task_id, body.message, node_id="developer")
        await daemon.bus.emit(
            type=EventType.operator_steer,
            project_id=task.project_id,
            task_id=task.id,
            payload={"message": body.message},
        )
        return {"steered": True}

    if task.state != TaskState.in_progress:
        raise HTTPException(
            status_code=409,
            detail=f"cannot steer a task in state {task.state.value} — it is not in-flight",
        )
    queued = await daemon.request_steer(task_id, body.message)
    if not queued:
        # The task left in_progress between the state check and the steer request.
        raise HTTPException(
            status_code=409,
            detail="task is no longer in-flight — refresh and retry if needed",
        )
    return {"steered": True}


@router.get("/tasks/{task_id}/hitl")
async def get_task_hitl(
    task_id: str, daemon: Daemon = Depends(get_daemon)
) -> dict[str, Any]:
    await _require_task(daemon, task_id)
    return {"task_id": task_id, **daemon.get_hitl_state(task_id)}


@router.post("/tasks/{task_id}/hitl")
async def toggle_task_hitl(
    task_id: str, body: HitlToggleInput, daemon: Daemon = Depends(get_daemon)
) -> dict[str, Any]:
    task = await _require_task(daemon, task_id)
    armed = daemon.set_hitl_checkpoint(task_id, body.target_id.strip(), body.enabled)
    await daemon.bus.emit(
        type=EventType.hitl_waiting if body.enabled else EventType.hitl_resolved,
        project_id=task.project_id,
        task_id=task.id,
        payload={
            "target_id": body.target_id.strip(),
            "enabled": body.enabled,
            "armed": armed,
        },
    )
    return {"task_id": task_id, "armed": armed, **daemon.get_hitl_state(task_id)}


@router.post("/tasks/{task_id}/nodes/{node_id}/complete")
async def complete_task_node(
    task_id: str,
    node_id: str,
    body: NodeCompleteInput,
    daemon: Daemon = Depends(get_daemon),
) -> dict[str, Any]:
    """Approve/complete a waiting node or HITL connector, or override a node's status."""
    task = await _require_task(daemon, task_id)
    clean_node = node_id.strip()
    raw_status = body.status.strip().lower()
    if raw_status not in ("completed", "failed", "skipped"):
        raise HTTPException(
            status_code=400,
            detail="status must be one of: completed, failed, skipped",
        )
    cp_status = CheckpointStatus(raw_status)
    existing_cps = await repo.list_task_checkpoints(daemon.db, task_id)
    node_cps = [cp for cp in existing_cps if cp.node_id == clean_node]
    default_attempt = 0 if clean_node in ("baseline", "planner") else 1
    attempt = (
        body.attempt
        if body.attempt is not None
        else (node_cps[-1].attempt if node_cps else default_attempt)
    )

    artifact: dict[str, Any] = dict(node_cps[-1].artifact) if node_cps else {}
    artifact["operator_completed"] = True
    if body.message:
        artifact["operator_message"] = body.message
    if body.output:
        artifact["operator_output"] = body.output
        if clean_node == "planner":
            artifact["plan_preamble"] = (
                f"\n\n=== PLAN (Operator Completed) ===\n{body.output}\n=== END PLAN ===\n"
            )
    if body.verdict:
        artifact["verdict"] = body.verdict.lower()

    cp = await repo.save_task_checkpoint(
        daemon.db,
        task_id=task_id,
        node_id=clean_node,
        attempt=attempt,
        status=cp_status,
        git_tree_sha=node_cps[-1].git_tree_sha if node_cps else None,
        artifact=artifact,
    )

    if body.verdict:
        await repo.add_verdict(
            daemon.db,
            task_id=task_id,
            attempt=max(1, attempt),
            agent=clean_node,
            verdict=body.verdict.lower(),
            reason=body.message or body.output or f"Operator marked {clean_node} as {body.verdict}",
            source="operator",
            grounded_count=1,
            evidence=[],
        )

    signaled = daemon.resolve_node(
        task_id,
        clean_node,
        status=raw_status,
        message=body.message,
        verdict=body.verdict,
        output=body.output,
        edge_id=body.edge_id,
    )
    if not signaled and task.state == TaskState.waiting_for_approval:
        await repo.approve_task(daemon.db, task_id)

    summary_text = (
        f"Node '{clean_node}' marked {cp_status.value}"
        + (f" (verdict={body.verdict})" if body.verdict else "")
        + (f": {body.message}" if body.message else "")
    )
    log_row = await repo.record_interaction_log(
        daemon.db,
        source="operator",
        agent=clean_node,
        model="operator/hitl",
        title=f"[HITL {cp_status.value}] {clean_node}",
        user_prompt=body.message or body.output or summary_text,
        messages=[
            {
                "role": "user",
                "text": (
                    body.message
                    or body.output
                    or f"Complete node {clean_node} -> {cp_status.value}"
                ),
            },
            {"role": "assistant", "text": summary_text},
        ],
        tool_calls_count=1,
        message_count=2,
        tool_breakdown={"hitl.complete_node": 1},
        project_id=task.project_id,
        task_id=task.id,
        node_id=clean_node,
    )
    await daemon.bus.emit(
        type=EventType.interaction_logged,
        project_id=task.project_id,
        task_id=task.id,
        payload={
            "session_id": log_row.session_id,
            "source": "operator",
            "agent": clean_node,
            "node_id": clean_node,
            "project_id": task.project_id,
            "task_id": task.id,
        },
    )
    await daemon.bus.emit(
        type=EventType.hitl_resolved,
        project_id=task.project_id,
        task_id=task.id,
        payload={
            "node_id": clean_node,
            "edge_id": body.edge_id,
            "attempt": attempt,
            "status": cp_status.value,
            "verdict": body.verdict,
            "message": body.message,
        },
    )
    await daemon.bus.emit(
        type=EventType.node_completed,
        project_id=task.project_id,
        task_id=task.id,
        payload={
            "node_id": clean_node,
            "workflow_node_id": clean_node,
            "attempt": attempt,
            "status": cp_status.value,
            "verdict": body.verdict,
        },
    )
    return {
        "ok": True,
        "signaled": signaled,
        "checkpoint": cp.model_dump(mode="json"),
        "hitl": daemon.get_hitl_state(task_id),
    }


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, daemon: Daemon = Depends(get_daemon)) -> dict[str, str]:
    task = await _require_task(daemon, task_id)
    if task.state == TaskState.in_progress:
        raise HTTPException(
            status_code=409,
            detail="cannot delete an in_progress task — cancel it first",
        )
    deleted = await repo.delete_task(daemon.db, task_id)
    if not deleted:
        # Defense-in-depth: the repo-level WHERE guard also caught it.
        raise HTTPException(
            status_code=409,
            detail="cannot delete an in_progress task — cancel it first",
        )
    # Emit after the delete so the tombstone event persists (queryable by project_id).
    await daemon.bus.emit(
        type=EventType.task_deleted, project_id=task.project_id, task_id=task_id,
    )
    return {"deleted": task_id}


@router.get("/tasks/{task_id}/events")
async def list_task_events(
    task_id: str, after_id: int = 0, daemon: Daemon = Depends(get_daemon)
) -> list[EventRow]:
    return await repo.list_events(daemon.db, task_id=task_id, after_id=after_id)


@router.post("/tasks/{task_id}/cascade-approve")
async def cascade_approve_task(
    task_id: str, daemon: Daemon = Depends(get_daemon)
) -> dict[str, Any]:
    """Approve a task and all its descendants in dependency order (BFS).

    Cycle-safe: a visited-set guards the walk so a cyclic ``parent_task_id``
    cannot cause an infinite loop.  Each node is approved at most once.
    """
    task = await _require_task(daemon, task_id)
    approved_ids: list[str] = []
    visited: set[str] = {task.id}
    queue = [task]
    depth = 0
    _MAX_DEPTH = 1000
    while queue:
        depth += 1
        if depth > _MAX_DEPTH:
            logger.warning(
                "cascade_approve_task hit depth cap %d at task %s — tree may be malformed",
                _MAX_DEPTH, task_id,
            )
            break
        current = queue.pop(0)
        if current.state in (TaskState.inbox, TaskState.failed):
            await repo.set_task_state(daemon.db, current.id, TaskState.approved)
            await daemon.bus.emit(
                type=EventType.task_approved, task_id=current.id,
                payload={"cascade": True, "root": task_id},
            )
            approved_ids.append(current.id)
        children = await repo.get_child_tasks(daemon.db, current.id)
        for child in children:
            if child.id in visited:
                logger.warning(
                    "cascade_approve_task detected cycle at task %s (child of %s) — skipping",
                    child.id, current.id,
                )
                continue
            visited.add(child.id)
            queue.append(child)
    return {"approved": True, "cascade": True, "task_ids": approved_ids, "count": len(approved_ids)}


@router.get("/tasks/{task_id}/usage")
async def get_task_usage(
    task_id: str, daemon: Daemon = Depends(get_daemon)
) -> dict[str, Any]:
    """Return SQL-aggregated token-usage totals for a single task (cost intentionally omitted).

    All aggregation happens in SQL via ``COALESCE(SUM(...), 0)`` — a single aggregate row
    is returned regardless of how many usage rows exist (DoS hardening — WEB-1).
    """
    await _require_task(daemon, task_id)
    return await repo.get_task_usage(daemon.db, task_id)


@router.get("/tasks/{task_id}/verdicts")
async def list_task_verdicts(
    task_id: str,
    pagination: PaginationParams = Depends(_pagination),
    daemon: Daemon = Depends(get_daemon),
) -> list[VerdictRow]:
    return await repo.list_verdicts(
        daemon.db, task_id, limit=pagination.limit, offset=pagination.offset
    )


@router.get("/tasks/{task_id}/checkpoints")
async def list_task_checkpoints(
    task_id: str,
    attempt: int | None = None,
    daemon: Daemon = Depends(get_daemon),
) -> list[TaskCheckpoint]:
    await _require_task(daemon, task_id)
    return await repo.list_task_checkpoints(daemon.db, task_id, attempt=attempt)


@router.get("/tasks/{task_id}/diff")
async def get_task_diff(
    task_id: str,
    stat_only: bool = False,
    max_bytes: int = Query(DEFAULT_DIFF_MAX_BYTES, ge=1, le=MAX_DIFF_MAX_BYTES),
    daemon: Daemon = Depends(get_daemon),
) -> dict[str, Any]:
    """Read-only view of a task's change (live worktree or task branch)."""
    task = await _require_task(daemon, task_id)
    project = await _require_project(daemon, task.project_id)
    try:
        return await compute_task_diff(
            daemon.home,
            project,
            task,
            stat_only=stat_only,
            max_bytes=max_bytes,
        )
    except TaskDiffError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/tasks/{task_id}/compact")
async def compact_task_endpoint(
    task_id: str, daemon: Daemon = Depends(get_daemon)
) -> dict[str, Any]:
    task = await _require_task(daemon, task_id)
    result = await compact_task_context(daemon.db, task_id)
    await daemon.bus.emit(
        type=EventType.context_compacted,
        project_id=task.project_id,
        task_id=task.id,
        payload=result,
    )
    return result


# --- agent personas ---------------------------------------------------------


@router.get("/agents")
async def list_agents(
    project_id: str | None = None,
    pagination: PaginationParams = Depends(_pagination),
    daemon: Daemon = Depends(get_daemon),
) -> list[AgentPersona]:
    return await repo.list_agents(
        daemon.db, project_id, limit=pagination.limit, offset=pagination.offset
    )


@router.put("/agents/{name}")
async def upsert_agent(
    name: str, body: AgentUpsert, daemon: Daemon = Depends(get_daemon)
) -> AgentPersona:
    persona = await repo.upsert_agent(
        daemon.db,
        name=name,
        role=body.role,
        persona_md=body.persona_md,
        project_id=body.project_id,
    )
    await daemon.bus.emit(
        type=EventType.agent_upserted,
        project_id=body.project_id,
        payload={"agent": persona.model_dump(mode="json")},
    )
    if body.project_id is None:
        first_line = next(
            (
                line.lstrip("# ").strip()
                for line in body.persona_md.splitlines()
                if line.strip()
            ),
            name,
        )
        store_item = await repo.upsert_store_item(
            daemon.db,
            kind=StoreItemKind.agent,
            name=name,
            description=f"{first_line} ({body.role} role)",
            tags=[body.role, "agent"],
            payload={"name": name, "role": body.role, "persona_md": body.persona_md},
            built_in=name in DEFAULT_PERSONAS,
        )
        await daemon.bus.emit(
            type=EventType.store_upserted,
            payload={"item": store_item.model_dump(mode="json")},
        )
    return persona


@router.delete("/agents/{name}")
async def delete_agent(
    name: str,
    project_id: str | None = None,
    daemon: Daemon = Depends(get_daemon),
) -> dict[str, str]:
    deleted = await repo.delete_agent(daemon.db, name=name, project_id=project_id)
    if project_id is None and name in DEFAULT_PERSONAS:
        role, persona_md = DEFAULT_PERSONAS[name]
        reset_persona = await repo.upsert_agent(
            daemon.db, name=name, role=role.value, persona_md=persona_md, project_id=None
        )
        await daemon.bus.emit(
            type=EventType.agent_deleted,
            project_id=None,
            payload={"name": name, "reset_to_default": True},
        )
        await daemon.bus.emit(
            type=EventType.agent_upserted,
            project_id=None,
            payload={"agent": reset_persona.model_dump(mode="json")},
        )
        return {"reset_to_default": name}
    if not deleted:
        raise HTTPException(status_code=404, detail="agent not found")
    if project_id is None:
        existing_store = await repo.get_store_item_by_name(
            daemon.db, StoreItemKind.agent, name
        )
        if existing_store is not None and not existing_store.built_in:
            await repo.delete_store_item(daemon.db, existing_store.id)
            await daemon.bus.emit(
                type=EventType.store_deleted,
                payload={"id": existing_store.id, "name": name},
            )
    await daemon.bus.emit(
        type=EventType.agent_deleted,
        project_id=project_id,
        payload={"name": name, "project_id": project_id},
    )
    return {"deleted": name}


# --- global store (Workflows, Agents, Config Presets) -----------------------


@router.get("/store")
async def list_store(
    kind: StoreItemKind | None = None,
    pagination: PaginationParams = Depends(_pagination),
    daemon: Daemon = Depends(get_daemon),
) -> list[StoreItem]:
    return await repo.list_store_items(
        daemon.db, kind=kind, limit=pagination.limit, offset=pagination.offset
    )


@router.post("/store")
@router.put("/store")
async def publish_store_item(
    body: StoreItemUpsert,
    daemon: Daemon = Depends(get_daemon),
) -> StoreItem:
    clean_payload = normalize_config_keys(body.payload)
    if body.kind is StoreItemKind.workflow:
        try:
            WorkflowGraph.model_validate(clean_payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
    elif body.kind is StoreItemKind.config_preset:
        try:
            load_project_config(clean_payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
    elif body.kind is StoreItemKind.agent:
        role = str(clean_payload.get("role") or "conditional")
        persona_md = str(clean_payload.get("persona_md") or "").strip()
        if not persona_md:
            raise HTTPException(
                status_code=400, detail="agent payload must include non-empty persona_md"
            )
        persona = await repo.upsert_agent(
            daemon.db,
            name=body.name,
            role=role,
            persona_md=persona_md,
            project_id=None,
        )
        await daemon.bus.emit(
            type=EventType.agent_upserted,
            project_id=None,
            payload={"agent": persona.model_dump(mode="json")},
        )

    item = await repo.upsert_store_item(
        daemon.db,
        kind=body.kind,
        name=body.name,
        description=body.description,
        tags=body.tags,
        payload=clean_payload,
        built_in=False,
    )
    await daemon.bus.emit(
        type=EventType.store_upserted,
        payload={"item": item.model_dump(mode="json")},
    )
    return item


@router.delete("/store/{item_id}")
async def remove_store_item(
    item_id: str,
    daemon: Daemon = Depends(get_daemon),
) -> dict[str, str]:
    item = await repo.get_store_item(daemon.db, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="store item not found")
    if item.built_in:
        raise HTTPException(status_code=400, detail="cannot delete a built-in store item")
    await repo.delete_store_item(daemon.db, item_id)
    await daemon.bus.emit(
        type=EventType.store_deleted,
        payload={"id": item_id},
    )
    return {"deleted": item_id}


@router.post("/projects/{project_id}/store/install")
async def install_store_item(
    project_id: str,
    body: StoreInstallInput,
    daemon: Daemon = Depends(get_daemon),
) -> dict[str, Any]:
    project = await _require_project(daemon, project_id)
    item: StoreItem | None = None
    if body.item_id:
        item = await repo.get_store_item(daemon.db, body.item_id)
    elif body.kind and body.name:
        item = await repo.get_store_item_by_name(daemon.db, body.kind, body.name)
    if item is None:
        raise HTTPException(status_code=404, detail="store item not found")

    await daemon.bus.emit(
        type=EventType.store_upserted,
        payload={"item": item.model_dump(mode="json"), "project_id": project_id},
    )

    if item.kind is StoreItemKind.workflow:
        raw_cfg = dict(project.config)
        raw_cfg["workflow"] = item.payload
        resolved = load_project_config(raw_cfg)
        await repo.update_project_config(daemon.db, project_id, raw_cfg)
        wf_dump = (resolved.workflow or build_default_workflow()).model_dump(mode="json")
        updated_proj = await _require_project(daemon, project_id)
        await daemon.bus.emit(
            type=EventType.workflow_updated,
            project_id=project_id,
            payload=_workflow_payload(resolved),
        )
        await daemon.bus.emit(
            type=EventType.project_updated,
            project_id=project_id,
            payload={"project": updated_proj.model_dump(mode="json")},
        )
        return {
            "installed": item.name,
            "kind": item.kind.value,
            "workflow": wf_dump,
        }

    if item.kind is StoreItemKind.config_preset:
        merged = deep_merge(project.config, item.payload)
        resolved = load_project_config(merged)
        await repo.update_project_config(daemon.db, project_id, merged)
        updated_proj = await _require_project(daemon, project_id)
        await daemon.bus.emit(
            type=EventType.project_updated,
            project_id=project_id,
            payload={
                "project": updated_proj.model_dump(mode="json"),
                "config": resolved.model_dump(mode="json"),
            },
        )
        return {
            "installed": item.name,
            "kind": item.kind.value,
            "config": resolved.model_dump(mode="json"),
        }

    # StoreItemKind.agent
    role = str(item.payload.get("role") or "conditional")
    persona_md = str(item.payload.get("persona_md") or "")
    agent_name = str(item.payload.get("name") or item.name)
    persona = await repo.upsert_agent(
        daemon.db,
        name=agent_name,
        role=role,
        persona_md=persona_md,
        project_id=project_id,
    )
    await daemon.bus.emit(
        type=EventType.agent_upserted,
        project_id=project_id,
        payload={"agent": persona.model_dump(mode="json")},
    )
    return {
        "installed": item.name,
        "kind": item.kind.value,
        "agent": persona.model_dump(mode="json"),
    }


# --- parallel swarms for mass changes ---------------------------------------


@router.post("/projects/{project_id}/swarms")
async def create_swarm(
    project_id: str,
    body: SwarmCreateInput,
    daemon: Daemon = Depends(get_daemon),
) -> dict[str, Any]:
    await _require_project(daemon, project_id)
    try:
        return await daemon.launch_swarm(
            project_id,
            units=[u.model_dump(mode="python") for u in body.units],
            prompt=body.prompt,
            concurrency=body.concurrency,
            auto_approve=body.auto_approve,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/projects/{project_id}/swarms/{swarm_id}")
async def get_swarm_status(
    project_id: str,
    swarm_id: str,
    daemon: Daemon = Depends(get_daemon),
) -> dict[str, Any]:
    await _require_project(daemon, project_id)
    tasks = await repo.list_swarm_tasks(daemon.db, project_id, swarm_id)
    counts: dict[str, int] = {}
    for t in tasks:
        counts[t.state.value] = counts.get(t.state.value, 0) + 1
    return {
        "swarm_id": swarm_id,
        "project_id": project_id,
        "total": len(tasks),
        "counts": counts,
        "tasks": [t.model_dump(mode="json") for t in tasks],
    }


# --- agent-ception planning sessions -----------------------------------------


@router.get("/projects/{project_id}/planning/sessions")
async def list_planning_sessions(
    project_id: str,
    pagination: PaginationParams = Depends(_pagination),
    daemon: Daemon = Depends(get_daemon),
) -> list[PlanningSession]:
    await _require_project(daemon, project_id)
    return await repo.list_planning_sessions(
        daemon.db, project_id, limit=pagination.limit, offset=pagination.offset
    )


@router.post("/projects/{project_id}/planning/sessions")
async def create_planning_session(
    project_id: str, body: PlanningSessionCreate, daemon: Daemon = Depends(get_daemon)
) -> PlanningSession:
    await _require_project(daemon, project_id)
    return await daemon.planning_orchestrator.create_and_start(
        project_id=project_id,
        title=body.title,
        prompt=body.prompt,
        max_rounds=body.max_rounds,
    )


@router.get("/planning/sessions/{session_id}")
async def get_planning_session(
    session_id: str, daemon: Daemon = Depends(get_daemon)
) -> PlanningSession:
    return await _require_planning_session(daemon, session_id)


@router.get("/planning/sessions/{session_id}/messages")
async def list_planning_messages(
    session_id: str,
    pagination: PaginationParams = Depends(_pagination),
    daemon: Daemon = Depends(get_daemon),
) -> list[PlanningMessage]:
    await _require_planning_session(daemon, session_id)
    return await repo.list_planning_messages(
        daemon.db, session_id, limit=pagination.limit, offset=pagination.offset
    )


@router.post("/planning/sessions/{session_id}/messages")
async def add_planning_message(
    session_id: str, body: PlanningMessageInput, daemon: Daemon = Depends(get_daemon)
) -> PlanningMessage:
    await _require_planning_session(daemon, session_id)
    return await daemon.planning_orchestrator.add_human_message(
        session_id, body.content
    )


@router.get("/planning/sessions/{session_id}/tasks")
async def list_planning_task_nodes(
    session_id: str,
    pagination: PaginationParams = Depends(_pagination),
    daemon: Daemon = Depends(get_daemon),
) -> list[PlanningTaskNode]:
    await _require_planning_session(daemon, session_id)
    return await repo.list_planning_task_nodes(
        daemon.db, session_id, limit=pagination.limit, offset=pagination.offset
    )


@router.post("/planning/sessions/{session_id}/approve")
async def approve_planning_session(
    session_id: str, daemon: Daemon = Depends(get_daemon)
) -> dict[str, Any]:
    await _require_planning_session(daemon, session_id)
    try:
        task_ids = await daemon.planning_orchestrator.approve_session(session_id)
    except ValueError as exc:
        msg = str(exc)
        if "session is cancelled" in msg:
            raise HTTPException(status_code=409, detail=msg) from exc
        raise HTTPException(status_code=400, detail=msg) from exc
    return {"approved": True, "task_ids": task_ids, "count": len(task_ids)}


@router.post("/planning/sessions/{session_id}/cancel")
async def cancel_planning_session(
    session_id: str, daemon: Daemon = Depends(get_daemon)
) -> dict[str, bool]:
    await _require_planning_session(daemon, session_id)
    try:
        await daemon.planning_orchestrator.cancel_session(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"cancelled": True}


@router.post("/planning/sessions/{session_id}/compact")
async def compact_planning_session(
    session_id: str, daemon: Daemon = Depends(get_daemon)
) -> dict[str, Any]:
    session = await _require_planning_session(daemon, session_id)
    result = await compact_planning_session_context(daemon.db, session_id)
    await daemon.bus.emit(
        type=EventType.context_compacted,
        project_id=session.project_id,
        planning_session_id=session.id,
        payload=result,
    )
    return result


# --- observability & interaction logs ---------------------------------------


@router.get("/observability/stats")
async def get_observability_stats(
    project_id: str | None = None,
    daemon: Daemon = Depends(get_daemon),
) -> dict[str, Any]:
    norm_pid = project_id.strip() if project_id and project_id.strip() else None
    return await repo.interaction_stats(daemon.db, project_id=norm_pid)


@router.get("/observability/sessions")
async def list_observability_sessions(
    project_id: str | None = None,
    task_id: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    search: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    daemon: Daemon = Depends(get_daemon),
) -> list[dict[str, Any]]:
    norm_pid = project_id.strip() if project_id and project_id.strip() else None
    norm_tid = task_id.strip() if task_id and task_id.strip() else None
    norm_agent = agent.strip() if agent and agent.strip() else None
    norm_model = model.strip() if model and model.strip() else None
    rows = await repo.list_interaction_logs(
        daemon.db,
        project_id=norm_pid,
        task_id=norm_tid,
        agent=norm_agent,
        model=norm_model,
        search=search,
        limit=limit,
        offset=offset,
    )
    result: list[dict[str, Any]] = []
    for r in rows:
        item = r.model_dump(mode="json")
        item["message_count"] = r.message_count or len(r.messages)
        item["messages"] = []
        result.append(item)
    return result


@router.get("/observability/sessions/{session_id}")
async def get_observability_session(
    session_id: str,
    daemon: Daemon = Depends(get_daemon),
) -> dict[str, Any]:
    row = await repo.get_interaction_log(daemon.db, session_id)
    if row is None:
        raise HTTPException(status_code=404, detail="interaction session not found")
    return row.model_dump(mode="json")


@router.post("/observability/query")
async def query_observability_logs(
    body: ObservabilityQueryInput,
    daemon: Daemon = Depends(get_daemon),
) -> dict[str, Any]:
    try:
        return await repo.query_interaction_logs_readonly(
            daemon.db, body.sql, limit=body.limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/observability/export-parquet")
async def export_observability_parquet(
    daemon: Daemon = Depends(get_daemon),
) -> dict[str, Any]:
    return await export_interaction_logs_to_parquet(daemon.db)
