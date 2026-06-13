"""REST API — the sole control surface for Conclave.

Thin handlers over the repository layer + runtime. Secrets are write-only (never
returned). Engine profiles can be tested before saving via /api/profiles/test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError

from .. import __version__
from ..config import ArgMode, config_schema, load_project_config
from ..db import (
    AgentPersona,
    EngineProfileRow,
    EventRow,
    Project,
    QuarantineEntry,
    RepoKnowledgeRow,
    Task,
    TaskState,
    VerdictRow,
)
from ..db import repositories as repo
from ..events import EventType
from ..providers import ProfileTestResult, ResolvedProfile, probe_profile
from ..repo_intel import onboard
from ..runtime import Daemon
from ..verification import quarantine_integrity
from .deps import get_daemon
from .schemas import (
    AgentUpsert,
    ConfigPatch,
    ProfileInput,
    ProjectCreate,
    QuarantineInput,
    SecretInput,
    TaskCreate,
)

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


# --- meta -------------------------------------------------------------------


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get("/config/schema")
async def get_config_schema() -> dict[str, Any]:
    return config_schema()


# --- projects ---------------------------------------------------------------


@router.get("/projects")
async def list_projects(daemon: Daemon = Depends(get_daemon)) -> list[Project]:
    return await repo.list_projects(daemon.db)


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
    await onboard(daemon.db, daemon.bus, project)
    await daemon.start_worker(project.id)
    return await _require_project(daemon, project.id)


@router.get("/projects/{project_id}")
async def get_project(project_id: str, daemon: Daemon = Depends(get_daemon)) -> Project:
    return await _require_project(daemon, project_id)


@router.delete("/projects/{project_id}")
async def detach_project(project_id: str, daemon: Daemon = Depends(get_daemon)) -> dict[str, str]:
    await daemon.stop_worker(project_id)
    await repo.delete_project(daemon.db, project_id)
    return {"detached": project_id}


@router.post("/projects/{project_id}/onboard")
async def reonboard(project_id: str, daemon: Daemon = Depends(get_daemon)) -> RepoKnowledgeRow:
    project = await _require_project(daemon, project_id)
    return await onboard(daemon.db, daemon.bus, project, force=True)


@router.get("/projects/{project_id}/knowledge")
async def get_knowledge(project_id: str, daemon: Daemon = Depends(get_daemon)) -> dict[str, Any]:
    row = await repo.current_repo_knowledge(daemon.db, project_id)
    return row.knowledge if row else {}


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
    project_id: str, daemon: Daemon = Depends(get_daemon)
) -> list[QuarantineEntry]:
    return await repo.list_quarantine(daemon.db, project_id)


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
    project_id: str, state: str | None = None, daemon: Daemon = Depends(get_daemon)
) -> list[Task]:
    task_state = TaskState(state) if state else None
    return await repo.list_tasks(daemon.db, project_id, task_state)


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
    await repo.set_task_state(daemon.db, task_id, TaskState.approved)
    await daemon.bus.emit(type=EventType.task_approved, task_id=task_id)
    return {"approved": task_id}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, daemon: Daemon = Depends(get_daemon)) -> dict[str, Any]:
    task = await _require_task(daemon, task_id)
    if task.state in (TaskState.inbox, TaskState.approved):
        await repo.set_task_state(daemon.db, task_id, TaskState.cancelled)
        await daemon.bus.emit(type=EventType.task_cancelled, task_id=task_id)
        return {"cancelled": True}
    return {"cancelled": False, "note": "in-progress cancellation is not supported in the MVP"}


@router.get("/tasks/{task_id}/events")
async def list_task_events(
    task_id: str, after_id: int = 0, daemon: Daemon = Depends(get_daemon)
) -> list[EventRow]:
    return await repo.list_events(daemon.db, task_id=task_id, after_id=after_id)


@router.get("/tasks/{task_id}/verdicts")
async def list_task_verdicts(
    task_id: str, daemon: Daemon = Depends(get_daemon)
) -> list[VerdictRow]:
    return await repo.list_verdicts(daemon.db, task_id)


# --- engine profiles --------------------------------------------------------


@router.get("/profiles")
async def list_profiles(
    project_id: str | None = None, daemon: Daemon = Depends(get_daemon)
) -> list[EngineProfileRow]:
    return await repo.list_engine_profiles(daemon.db, project_id)


@router.post("/profiles")
async def upsert_profile(
    body: ProfileInput, daemon: Daemon = Depends(get_daemon)
) -> EngineProfileRow:
    _validate_arg_mode(body.arg_mode)
    existing = await repo.get_engine_profile(daemon.db, body.name, body.project_id)
    auth_secret_id = existing.auth_secret_id if existing else None
    if body.auth_token:
        scope = body.project_id or "global"
        auth_secret_id = await repo.set_secret(
            daemon.db, f"engine_profile:{scope}:{body.name}", body.auth_token
        )
    return await repo.upsert_engine_profile(
        daemon.db,
        name=body.name,
        project_id=body.project_id,
        arg_mode=body.arg_mode,
        base_url=body.base_url,
        model=body.model,
        subagent_model=body.subagent_model,
        effort=body.effort,
        auth_secret_id=auth_secret_id,
        extra_env=body.extra_env,
    )


@router.delete("/profiles/{profile_id}")
async def delete_profile(profile_id: str, daemon: Daemon = Depends(get_daemon)) -> dict[str, str]:
    await repo.delete_engine_profile(daemon.db, profile_id)
    return {"deleted": profile_id}


@router.post("/profiles/test")
async def test_profile(
    body: ProfileInput, daemon: Daemon = Depends(get_daemon)
) -> ProfileTestResult:
    _validate_arg_mode(body.arg_mode)
    auth = body.auth_token
    if auth is None:
        existing = await repo.get_engine_profile(daemon.db, body.name, body.project_id)
        if existing is not None and existing.auth_secret_id:
            auth = await repo.get_secret_value(daemon.db, existing.auth_secret_id)
    profile = ResolvedProfile(
        name=body.name,
        arg_mode=ArgMode(body.arg_mode),
        base_url=body.base_url,
        auth_token=auth,
        model=body.model,
        subagent_model=body.subagent_model,
        effort=body.effort,
        extra_env=body.extra_env,
    )
    return await probe_profile(daemon.provider, profile, timeout_seconds=120)


# --- secrets (write-only) ---------------------------------------------------


@router.get("/secrets")
async def list_secrets(daemon: Daemon = Depends(get_daemon)) -> list[str]:
    return await repo.list_secret_names(daemon.db)


@router.post("/secrets")
async def set_secret(body: SecretInput, daemon: Daemon = Depends(get_daemon)) -> dict[str, Any]:
    await repo.set_secret(daemon.db, body.name, body.value)
    return {"name": body.name, "stored": True}


# --- agent personas ---------------------------------------------------------


@router.get("/agents")
async def list_agents(
    project_id: str | None = None, daemon: Daemon = Depends(get_daemon)
) -> list[AgentPersona]:
    return await repo.list_agents(daemon.db, project_id)


@router.put("/agents/{name}")
async def upsert_agent(
    name: str, body: AgentUpsert, daemon: Daemon = Depends(get_daemon)
) -> AgentPersona:
    return await repo.upsert_agent(
        daemon.db, name=name, role=body.role, persona_md=body.persona_md, project_id=body.project_id
    )


def _validate_arg_mode(value: str) -> None:
    try:
        ArgMode(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid arg_mode: {value}") from exc
