"""REST API — the sole control surface for Conclave.

Thin handlers over the repository layer + runtime. Model/provider selection and auth
are owned by opencode; repo context comes from each project's AGENTS.md, so there are
no engine-profile, secret, or repo-knowledge endpoints here.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError

from .. import __version__
from ..config import config_schema, load_project_config
from ..db import (
    AgentPersona,
    EventRow,
    Project,
    QuarantineEntry,
    Task,
    TaskState,
    VerdictRow,
)
from ..db import repositories as repo
from ..db.planning_models import PlanningMessage, PlanningSession, PlanningTaskNode
from ..events import EventType
from ..runtime import Daemon
from ..verification import quarantine_integrity
from .deps import get_daemon
from .schemas import (
    AgentUpsert,
    ConfigPatch,
    PaginationParams,
    PlanningMessageInput,
    PlanningSessionCreate,
    ProjectCreate,
    QuarantineInput,
    TaskCreate,
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


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get("/config/schema")
async def get_config_schema() -> dict[str, Any]:
    return config_schema()


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
    project = await repo.create_project(
        daemon.db, name=body.name, path=str(path), default_branch=body.default_branch
    )
    await daemon.start_worker(project.id)
    return await _require_project(daemon, project.id)


@router.get("/projects/{project_id}")
async def get_project(project_id: str, daemon: Daemon = Depends(get_daemon)) -> Project:
    return await _require_project(daemon, project_id)


@router.delete("/projects/{project_id}")
async def detach_project(project_id: str, daemon: Daemon = Depends(get_daemon)) -> dict[str, str]:
    await daemon.stop_worker(project_id)
    await daemon.cleanup_in_progress_work(project_id)
    await repo.delete_project(daemon.db, project_id)
    return {"detached": project_id}


@router.get("/projects/{project_id}/config")
async def get_config(project_id: str, daemon: Daemon = Depends(get_daemon)) -> dict[str, Any]:
    project = await _require_project(daemon, project_id)
    return load_project_config(project.config).model_dump(mode="json")


@router.patch("/projects/{project_id}/config")
async def patch_config(
    project_id: str, body: ConfigPatch, daemon: Daemon = Depends(get_daemon)
) -> dict[str, bool]:
    await _require_project(daemon, project_id)
    try:
        load_project_config(body.config)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors()) from exc
    await repo.update_project_config(daemon.db, project_id, body.config)
    return {"ok": True}


@router.post("/projects/{project_id}/pause")
async def pause_project(project_id: str, daemon: Daemon = Depends(get_daemon)) -> dict[str, bool]:
    return {"paused": daemon.set_paused(project_id, True)}


@router.post("/projects/{project_id}/resume")
async def resume_project(project_id: str, daemon: Daemon = Depends(get_daemon)) -> dict[str, bool]:
    return {"running": daemon.set_paused(project_id, False)}


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
    )
    await daemon.bus.emit(
        type=EventType.task_created, project_id=project_id, task_id=task.id,
        payload={"title": task.title, "auto_approve": body.auto_approve},
    )
    return task


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, daemon: Daemon = Depends(get_daemon)) -> Task:
    return await _require_task(daemon, task_id)


@router.post("/tasks/{task_id}/approve")
async def approve_task(task_id: str, daemon: Daemon = Depends(get_daemon)) -> dict[str, str]:
    await _require_task(daemon, task_id)
    updated = await repo.approve_task(daemon.db, task_id)
    if not updated:
        raise HTTPException(
            status_code=409,
            detail=(
                "task is not in an approvable state "
                "(only inbox or failed tasks can be approved)"
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
    if task.state == TaskState.in_progress:
        cancelled = await daemon.request_cancel(task_id)
        if cancelled:
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
    return await repo.upsert_agent(
        daemon.db, name=name, role=body.role, persona_md=body.persona_md, project_id=body.project_id
    )


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
