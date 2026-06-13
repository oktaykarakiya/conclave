"""Typed repository functions over :class:`Database`.

Plain async functions (not classes) keep call sites explicit and mypy-friendly.
Each returns domain row models from :mod:`conclave.db.models`.
"""

from __future__ import annotations

import json
from typing import Any

from ..util import new_id, now_iso
from .database import Database
from .models import (
    AgentPersona,
    Baseline,
    EngineProfileRow,
    EventRow,
    Project,
    ProjectMode,
    QuarantineEntry,
    RepoKnowledgeRow,
    Task,
    TaskOrigin,
    TaskState,
    VerdictRow,
)

# --- projects ---------------------------------------------------------------


async def create_project(
    db: Database, *, name: str, path: str, default_branch: str, config: dict[str, Any] | None = None
) -> Project:
    pid = new_id()
    ts = now_iso()
    await db.execute(
        "INSERT INTO projects(id, name, path, default_branch, mode, config_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            pid,
            name,
            path,
            default_branch,
            ProjectMode.task_queue.value,
            json.dumps(config or {}),
            ts,
        ),
    )
    project = await get_project(db, pid)
    assert project is not None
    return project


async def get_project(db: Database, project_id: str) -> Project | None:
    row = await db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
    return Project.from_row(row) if row else None


async def list_projects(db: Database) -> list[Project]:
    rows = await db.fetchall("SELECT * FROM projects ORDER BY created_at")
    return [Project.from_row(r) for r in rows]


async def update_project_config(db: Database, project_id: str, config: dict[str, Any]) -> None:
    await db.execute(
        "UPDATE projects SET config_json = ? WHERE id = ?", (json.dumps(config), project_id)
    )


async def set_project_mode(db: Database, project_id: str, mode: ProjectMode) -> None:
    await db.execute("UPDATE projects SET mode = ? WHERE id = ?", (mode.value, project_id))


async def delete_project(db: Database, project_id: str) -> None:
    await db.execute("DELETE FROM projects WHERE id = ?", (project_id,))


# --- tasks ------------------------------------------------------------------


async def create_task(
    db: Database,
    *,
    project_id: str,
    request: str,
    title: str = "",
    level: int | None = None,
    use_planner: bool | None = None,
    state: TaskState = TaskState.inbox,
    origin: TaskOrigin = TaskOrigin.operator,
) -> Task:
    tid = new_id()
    ts = now_iso()
    up = None if use_planner is None else int(use_planner)
    await db.execute(
        "INSERT INTO tasks(id, project_id, title, request, level, state, use_planner, origin, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (tid, project_id, title, request, level, state.value, up, origin.value, ts, ts),
    )
    task = await get_task(db, tid)
    assert task is not None
    return task


async def get_task(db: Database, task_id: str) -> Task | None:
    row = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    return Task.from_row(row) if row else None


async def list_tasks(
    db: Database, project_id: str, state: TaskState | None = None
) -> list[Task]:
    if state is None:
        rows = await db.fetchall(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
        )
    else:
        rows = await db.fetchall(
            "SELECT * FROM tasks WHERE project_id = ? AND state = ? ORDER BY created_at DESC",
            (project_id, state.value),
        )
    return [Task.from_row(r) for r in rows]


async def set_task_state(db: Database, task_id: str, state: TaskState) -> None:
    await db.execute(
        "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
        (state.value, now_iso(), task_id),
    )


async def update_task_fields(
    db: Database,
    task_id: str,
    *,
    title: str | None = None,
    level: int | None = None,
    plan: dict[str, Any] | None = None,
    branch: str | None = None,
    result_summary: str | None = None,
) -> None:
    sets: list[str] = []
    params: list[Any] = []
    if title is not None:
        sets.append("title = ?")
        params.append(title)
    if level is not None:
        sets.append("level = ?")
        params.append(level)
    if plan is not None:
        sets.append("plan_json = ?")
        params.append(json.dumps(plan))
    if branch is not None:
        sets.append("branch = ?")
        params.append(branch)
    if result_summary is not None:
        sets.append("result_summary = ?")
        params.append(result_summary)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(now_iso())
    params.append(task_id)
    await db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", tuple(params))


async def claim_next_approved(db: Database, project_id: str) -> Task | None:
    """Atomically pick the oldest ``approved`` task and mark it ``in_progress``."""
    cur = await db.conn.execute(
        "UPDATE tasks SET state = 'in_progress', updated_at = ? "
        "WHERE id = (SELECT id FROM tasks WHERE project_id = ? AND state = 'approved' "
        "ORDER BY created_at LIMIT 1) RETURNING *",
        (now_iso(), project_id),
    )
    row = await cur.fetchone()
    await db.conn.commit()
    return Task.from_row(row) if row else None


async def recover_in_progress(db: Database, project_id: str) -> int:
    """Reset orphaned ``in_progress`` tasks to ``approved`` (crash recovery)."""
    cur = await db.conn.execute(
        "UPDATE tasks SET state = 'approved', updated_at = ? "
        "WHERE project_id = ? AND state = 'in_progress'",
        (now_iso(), project_id),
    )
    await db.conn.commit()
    return cur.rowcount


# --- attempts ---------------------------------------------------------------


async def start_attempt(db: Database, task_id: str, n: int) -> str:
    aid = new_id()
    await db.execute(
        "INSERT INTO attempts(id, task_id, n, started_at) VALUES (?, ?, ?, ?)",
        (aid, task_id, n, now_iso()),
    )
    return aid


async def end_attempt(db: Database, attempt_id: str, diff_stat: str | None = None) -> None:
    await db.execute(
        "UPDATE attempts SET ended_at = ?, diff_stat = ? WHERE id = ?",
        (now_iso(), diff_stat, attempt_id),
    )


# --- verdicts ---------------------------------------------------------------


async def add_verdict(
    db: Database,
    *,
    task_id: str,
    attempt: int,
    agent: str,
    verdict: str,
    reason: str = "",
    source: str = "none",
    grounded_count: int = 0,
    evidence: list[dict[str, Any]] | None = None,
) -> None:
    await db.execute(
        "INSERT INTO verdicts(id, task_id, attempt, agent, verdict, reason, source, "
        "grounded_count, evidence_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id(),
            task_id,
            attempt,
            agent,
            verdict,
            reason,
            source,
            grounded_count,
            json.dumps(evidence or []),
            now_iso(),
        ),
    )


async def list_verdicts(db: Database, task_id: str) -> list[VerdictRow]:
    rows = await db.fetchall(
        "SELECT * FROM verdicts WHERE task_id = ? ORDER BY attempt, created_at", (task_id,)
    )
    return [VerdictRow.from_row(r) for r in rows]


# --- events -----------------------------------------------------------------


async def append_event(
    db: Database,
    *,
    type: str,
    project_id: str | None = None,
    task_id: str | None = None,
    agent: str | None = None,
    payload: dict[str, Any] | None = None,
) -> EventRow:
    ts = now_iso()
    cur = await db.conn.execute(
        "INSERT INTO events(project_id, task_id, agent, type, payload_json, ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (project_id, task_id, agent, type, json.dumps(payload or {}), ts),
    )
    await db.conn.commit()
    return EventRow(
        id=int(cur.lastrowid or 0),
        project_id=project_id,
        task_id=task_id,
        agent=agent,
        type=type,
        payload=payload or {},
        ts=ts,
    )


async def list_events(
    db: Database,
    *,
    task_id: str | None = None,
    project_id: str | None = None,
    after_id: int = 0,
    limit: int = 500,
) -> list[EventRow]:
    if task_id is not None:
        rows = await db.fetchall(
            "SELECT * FROM events WHERE task_id = ? AND id > ? ORDER BY id LIMIT ?",
            (task_id, after_id, limit),
        )
    elif project_id is not None:
        rows = await db.fetchall(
            "SELECT * FROM events WHERE project_id = ? AND id > ? ORDER BY id LIMIT ?",
            (project_id, after_id, limit),
        )
    else:
        rows = await db.fetchall(
            "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)
        )
    return [EventRow.from_row(r) for r in rows]


# --- usage ------------------------------------------------------------------


async def add_usage(
    db: Database,
    *,
    agent: str,
    project_id: str | None = None,
    task_id: str | None = None,
    model_reported: str | None = None,
    cost_usd: float | None = None,
    num_turns: int | None = None,
) -> None:
    await db.execute(
        "INSERT INTO usage(id, project_id, task_id, agent, model_reported, cost_usd, "
        "num_turns, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (new_id(), project_id, task_id, agent, model_reported, cost_usd, num_turns, now_iso()),
    )


async def usage_summary(db: Database, project_id: str) -> dict[str, Any]:
    row = await db.fetchone(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0.0) AS total_cost "
        "FROM usage WHERE project_id = ?",
        (project_id,),
    )
    assert row is not None  # COUNT(*) always yields a row
    return {"calls": int(row["calls"]), "total_cost_usd": float(row["total_cost"])}


# --- baselines --------------------------------------------------------------


async def get_baseline(db: Database, project_id: str, sha: str) -> Baseline | None:
    row = await db.fetchone(
        "SELECT * FROM baselines WHERE project_id = ? AND sha = ?", (project_id, sha)
    )
    return Baseline.from_row(row) if row else None


async def save_baseline(db: Database, project_id: str, sha: str, output: str) -> None:
    await db.execute(
        "INSERT OR REPLACE INTO baselines(project_id, sha, output, created_at) VALUES (?, ?, ?, ?)",
        (project_id, sha, output, now_iso()),
    )


async def gc_baselines(db: Database, project_id: str, keep: int = 20) -> None:
    await db.execute(
        "DELETE FROM baselines WHERE project_id = ? AND sha NOT IN "
        "(SELECT sha FROM baselines WHERE project_id = ? ORDER BY created_at DESC LIMIT ?)",
        (project_id, project_id, keep),
    )


# --- quarantine -------------------------------------------------------------


async def add_quarantine(
    db: Database,
    *,
    project_id: str,
    pattern: str,
    reason: str,
    until: str,
    created_by: str = "operator",
) -> QuarantineEntry:
    qid = new_id()
    await db.execute(
        "INSERT INTO quarantine(id, project_id, pattern, reason, until, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (qid, project_id, pattern, reason, until, created_by, now_iso()),
    )
    row = await db.fetchone("SELECT * FROM quarantine WHERE id = ?", (qid,))
    assert row is not None
    return QuarantineEntry.from_row(row)


async def list_quarantine(db: Database, project_id: str) -> list[QuarantineEntry]:
    rows = await db.fetchall(
        "SELECT * FROM quarantine WHERE project_id = ? ORDER BY until", (project_id,)
    )
    return [QuarantineEntry.from_row(r) for r in rows]


async def active_quarantine(
    db: Database, project_id: str, today: str
) -> list[QuarantineEntry]:
    """Non-expired entries only (``until`` on/after ``today``); enforces expiry in code."""
    rows = await db.fetchall(
        "SELECT * FROM quarantine WHERE project_id = ? AND until >= ? ORDER BY until",
        (project_id, today),
    )
    return [QuarantineEntry.from_row(r) for r in rows]


async def delete_quarantine(db: Database, entry_id: str) -> None:
    await db.execute("DELETE FROM quarantine WHERE id = ?", (entry_id,))


# --- secrets (values are write-only via the API; never returned to the UI) ---


async def set_secret(db: Database, name: str, value: str) -> str:
    existing = await db.fetchone("SELECT id FROM secrets WHERE name = ?", (name,))
    if existing is not None:
        await db.execute("UPDATE secrets SET value = ? WHERE name = ?", (value, name))
        return str(existing["id"])
    sid = new_id()
    await db.execute(
        "INSERT INTO secrets(id, name, value, created_at) VALUES (?, ?, ?, ?)",
        (sid, name, value, now_iso()),
    )
    return sid


async def get_secret_value(db: Database, secret_id: str) -> str | None:
    value = await db.fetchval("SELECT value FROM secrets WHERE id = ?", (secret_id,))
    return None if value is None else str(value)


async def list_secret_names(db: Database) -> list[str]:
    rows = await db.fetchall("SELECT name FROM secrets ORDER BY name")
    return [r["name"] for r in rows]


# --- engine profiles --------------------------------------------------------


async def upsert_engine_profile(
    db: Database,
    *,
    name: str,
    project_id: str | None = None,
    arg_mode: str = "inherit",
    base_url: str | None = None,
    model: str | None = None,
    subagent_model: str | None = None,
    effort: str | None = None,
    auth_secret_id: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> EngineProfileRow:
    scope = project_id or ""
    existing = await db.fetchone(
        "SELECT id FROM engine_profiles WHERE IFNULL(project_id, '') = ? AND name = ?",
        (scope, name),
    )
    env_json = json.dumps(extra_env or {})
    if existing is not None:
        await db.execute(
            "UPDATE engine_profiles SET arg_mode = ?, base_url = ?, model = ?, subagent_model = ?, "
            "effort = ?, auth_secret_id = ?, extra_env_json = ? WHERE id = ?",
            (arg_mode, base_url, model, subagent_model, effort, auth_secret_id, env_json,
             existing["id"]),
        )
        pid = str(existing["id"])
    else:
        pid = new_id()
        await db.execute(
            "INSERT INTO engine_profiles(id, project_id, name, arg_mode, base_url, model, "
            "subagent_model, effort, auth_secret_id, extra_env_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (pid, project_id, name, arg_mode, base_url, model, subagent_model, effort,
             auth_secret_id, env_json, now_iso()),
        )
    row = await db.fetchone("SELECT * FROM engine_profiles WHERE id = ?", (pid,))
    assert row is not None
    return EngineProfileRow.from_row(row)


async def get_engine_profile(
    db: Database, name: str, project_id: str | None = None
) -> EngineProfileRow | None:
    """Project-scoped profile if present, else the global profile of that name."""
    if project_id is not None:
        row = await db.fetchone(
            "SELECT * FROM engine_profiles WHERE project_id = ? AND name = ?", (project_id, name)
        )
        if row is not None:
            return EngineProfileRow.from_row(row)
    row = await db.fetchone(
        "SELECT * FROM engine_profiles WHERE project_id IS NULL AND name = ?", (name,)
    )
    return EngineProfileRow.from_row(row) if row else None


async def list_engine_profiles(
    db: Database, project_id: str | None = None
) -> list[EngineProfileRow]:
    rows = await db.fetchall(
        "SELECT * FROM engine_profiles WHERE project_id IS NULL OR project_id = ? ORDER BY name",
        (project_id,),
    )
    return [EngineProfileRow.from_row(r) for r in rows]


# --- agent personas ---------------------------------------------------------


async def upsert_agent(
    db: Database, *, name: str, role: str, persona_md: str, project_id: str | None = None
) -> AgentPersona:
    scope = project_id or ""
    existing = await db.fetchone(
        "SELECT id FROM agents WHERE IFNULL(project_id, '') = ? AND name = ?", (scope, name)
    )
    if existing is not None:
        await db.execute(
            "UPDATE agents SET role = ?, persona_md = ? WHERE id = ?",
            (role, persona_md, existing["id"]),
        )
        aid = str(existing["id"])
    else:
        aid = new_id()
        await db.execute(
            "INSERT INTO agents(id, project_id, name, role, persona_md, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (aid, project_id, name, role, persona_md, now_iso()),
        )
    row = await db.fetchone("SELECT * FROM agents WHERE id = ?", (aid,))
    assert row is not None
    return AgentPersona.from_row(row)


async def get_agent(
    db: Database, name: str, project_id: str | None = None
) -> AgentPersona | None:
    if project_id is not None:
        row = await db.fetchone(
            "SELECT * FROM agents WHERE project_id = ? AND name = ?", (project_id, name)
        )
        if row is not None:
            return AgentPersona.from_row(row)
    row = await db.fetchone(
        "SELECT * FROM agents WHERE project_id IS NULL AND name = ?", (name,)
    )
    return AgentPersona.from_row(row) if row else None


async def list_agents(db: Database, project_id: str | None = None) -> list[AgentPersona]:
    rows = await db.fetchall(
        "SELECT * FROM agents WHERE project_id IS NULL OR project_id = ? ORDER BY name",
        (project_id,),
    )
    return [AgentPersona.from_row(r) for r in rows]


# --- repo knowledge ---------------------------------------------------------


async def save_repo_knowledge(
    db: Database,
    *,
    project_id: str,
    knowledge: dict[str, Any],
    sha: str | None = None,
    manifest_fingerprint: str | None = None,
) -> RepoKnowledgeRow:
    prev = await db.fetchval(
        "SELECT COALESCE(MAX(version), 0) FROM repo_knowledge WHERE project_id = ?", (project_id,)
    )
    version = int(prev) + 1
    rid = new_id()
    await db.execute(
        "INSERT INTO repo_knowledge(id, project_id, version, sha, manifest_fingerprint, "
        "knowledge_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (rid, project_id, version, sha, manifest_fingerprint, json.dumps(knowledge), now_iso()),
    )
    row = await db.fetchone("SELECT * FROM repo_knowledge WHERE id = ?", (rid,))
    assert row is not None
    return RepoKnowledgeRow.from_row(row)


async def current_repo_knowledge(db: Database, project_id: str) -> RepoKnowledgeRow | None:
    row = await db.fetchone(
        "SELECT * FROM repo_knowledge WHERE project_id = ? ORDER BY version DESC LIMIT 1",
        (project_id,),
    )
    return RepoKnowledgeRow.from_row(row) if row else None
