"""Typed repository functions over :class:`Database`.

Plain async functions (not classes) keep call sites explicit and mypy-friendly.
Each returns domain row models from :mod:`conclave.db.models`.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite

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
from .planning_models import (
    PlanningMessage,
    PlanningNodeStatus,
    PlanningSession,
    PlanningSessionStatus,
    PlanningTaskNode,
)

logger = logging.getLogger("conclave.db")

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


async def list_projects(
    db: Database, limit: int | None = None, offset: int = 0
) -> list[Project]:
    if limit is not None:
        rows = await db.fetchall(
            "SELECT * FROM projects ORDER BY created_at LIMIT ? OFFSET ?", (limit, offset)
        )
    else:
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
    parent_task_id: str | None = None,
) -> Task:
    tid = new_id()
    ts = now_iso()
    up = None if use_planner is None else int(use_planner)
    await db.execute(
        "INSERT INTO tasks(id, project_id, title, request, level, state, use_planner, origin, "
        "parent_task_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tid, project_id, title, request, level, state.value, up, origin.value,
            parent_task_id, ts, ts,
        ),
    )
    task = await get_task(db, tid)
    assert task is not None
    return task


async def get_task(db: Database, task_id: str) -> Task | None:
    row = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    return Task.from_row(row) if row else None


async def list_tasks(
    db: Database,
    project_id: str,
    state: TaskState | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[Task]:
    if limit is not None:
        if state is None:
            rows = await db.fetchall(
                "SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at DESC "
                "LIMIT ? OFFSET ?",
                (project_id, limit, offset),
            )
        else:
            rows = await db.fetchall(
                "SELECT * FROM tasks WHERE project_id = ? AND state = ? "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (project_id, state.value, limit, offset),
            )
    elif state is None:
        rows = await db.fetchall(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
        )
    else:
        rows = await db.fetchall(
            "SELECT * FROM tasks WHERE project_id = ? AND state = ? ORDER BY created_at DESC",
            (project_id, state.value),
        )
    return [Task.from_row(r) for r in rows]


async def get_child_tasks(db: Database, parent_task_id: str) -> list[Task]:
    rows = await db.fetchall(
        "SELECT * FROM tasks WHERE parent_task_id = ? ORDER BY created_at", (parent_task_id,)
    )
    return [Task.from_row(r) for r in rows]


async def set_task_state(db: Database, task_id: str, state: TaskState) -> None:
    await db.execute(
        "UPDATE tasks SET state = ?, updated_at = ? WHERE id = ?",
        (state.value, now_iso(), task_id),
    )


async def approve_task(db: Database, task_id: str) -> bool:
    """Atomically set state to ``approved`` only if currently in an approvable state.

    The WHERE clause guarantees the state transition is conditional at the row
    level — a worker claiming the task between a prior read and this UPDATE cannot
    cause a duplicate run because the UPDATE simply won't match (zero rows affected).

    Returns ``True`` if a row was updated, ``False`` if the task was not in an
    approvable state (or doesn't exist).
    """
    async with db._write() as conn:
        cur = await conn.execute(
            "UPDATE tasks SET state = ?, updated_at = ? "
            "WHERE id = ? AND state IN ('inbox', 'failed')",
            (TaskState.approved.value, now_iso(), task_id),
        )
        return cur.rowcount > 0


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
    """Atomically pick the oldest claimable ``approved`` task and mark it ``in_progress``.

    Claimability model: a task is claimable iff it has no parent OR its parent has completed
    successfully (``state = 'done'``). Every other parent state keeps the child unclaimable —
    both not-yet-terminal (inbox/approved/in_progress), so a subtask can never run before its
    parent finishes, and terminal-non-success (failed/blocked/cancelled), so a doomed subtree
    is never executed. The ``parent_task_id IS NULL`` branch is load-bearing: when no 'done'
    rows exist the inner subquery is empty and ``IN (<empty>)`` is false, which would otherwise
    stop parentless tasks from ever being claimed.
    """
    # Still a single atomic UPDATE...RETURNING — the write lock only serializes its commit
    # against other writers so it cannot flush an open transaction() on the shared connection.
    async with db._write() as conn:
        cur = await conn.execute(
            "UPDATE tasks SET state = 'in_progress', updated_at = ? "
            "WHERE id = ("
            "  SELECT t.id FROM tasks t "
            "  WHERE t.project_id = ? AND t.state = 'approved' "
            "  AND ("
            "    t.parent_task_id IS NULL OR t.parent_task_id IN ("
            "      SELECT id FROM tasks WHERE state = 'done'"
            "    )"
            "  )"
            "  ORDER BY t.created_at LIMIT 1"
            ") RETURNING *",
            (now_iso(), project_id),
        )
        row = await cur.fetchone()
    return Task.from_row(row) if row else None


async def block_descendants(db: Database, task_id: str) -> int:
    """Recursively mark all descendant tasks as blocked. Returns count of blocked tasks.

    The whole BFS runs in one transaction so a crash mid-walk can never leave the subtree
    half-blocked (some children blocked, others still claimable).
    """
    async with db.transaction() as conn:
        return await _block_descendants(conn, task_id)


async def finalize_task(
    db: Database,
    task_id: str,
    *,
    state: TaskState,
    result_summary: str,
    block_children: bool = False,
) -> int:
    """Atomically set a task's terminal ``state`` + ``result_summary`` (one UPDATE) and,
    when ``block_children`` is set, block every active descendant — all in ONE transaction.

    Returns the number of descendants blocked (0 when ``block_children`` is False). Folding
    the state flip, the summary, and the cascade into a single transaction keeps a task's
    lifecycle row from ever being observed half-updated (e.g. state flipped to ``failed``
    but children still claimable) if a crash lands mid-sequence.
    """
    blocked = 0
    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE tasks SET state = ?, result_summary = ?, updated_at = ? WHERE id = ?",
            (state.value, result_summary, now_iso(), task_id),
        )
        if block_children:
            blocked = await _block_descendants(conn, task_id)
    return blocked


async def _block_descendants(conn: aiosqlite.Connection, task_id: str) -> int:
    """BFS that blocks every active descendant of ``task_id`` on an already-open transaction.

    Cycle-safe: a visited-set guards the walk so a cyclic ``parent_task_id`` cannot
    cause an infinite loop.  Each node is blocked at most once.

    Operates on a caller-supplied connection so it composes into a larger transaction
    (e.g. :func:`finalize_task`); the caller owns the surrounding BEGIN/COMMIT.
    """
    blocked = 0
    visited: set[str] = {task_id}
    queue = [task_id]
    depth = 0
    _MAX_DEPTH = 1000
    while queue:
        depth += 1
        if depth > _MAX_DEPTH:
            logger.warning(
                "_block_descendants hit depth cap %d at task %s — tree may be malformed",
                _MAX_DEPTH, task_id,
            )
            break
        parent = queue.pop(0)
        cur = await conn.execute(
            "SELECT id FROM tasks WHERE parent_task_id = ? "
            "AND state NOT IN ('done', 'failed', 'cancelled', 'blocked')",
            (parent,),
        )
        children = [r["id"] for r in await cur.fetchall()]
        for child_id in children:
            if child_id in visited:
                logger.warning(
                    "_block_descendants detected cycle at task %s (child of %s) — skipping",
                    child_id, parent,
                )
                continue
            visited.add(child_id)
            await conn.execute(
                "UPDATE tasks SET state = 'blocked', updated_at = ? WHERE id = ?",
                (now_iso(), child_id),
            )
            blocked += 1
            queue.append(child_id)
    return blocked


async def recover_in_progress(db: Database, project_id: str) -> tuple[int, int]:
    """Reset orphaned ``in_progress`` tasks to ``approved`` and re-block descendants
    of ``failed``/``blocked`` parents (crash recovery).

    Returns ``(recovered, reblocked)`` where *recovered* is the count of ``in_progress``
    tasks reset to ``approved`` and *reblocked* is the count of descendant tasks that
    were set to ``blocked`` because their parent is still ``failed``/``blocked``.

    Runs in a single transaction so an observer can never see recovered-but-not-yet-
    reblocked state — the recovery atomically resets orphans AND re-applies blocking.
    """
    recovered = 0
    reblocked = 0
    async with db.transaction() as conn:
        cur = await conn.execute(
            "UPDATE tasks SET state = 'approved', updated_at = ? "
            "WHERE project_id = ? AND state = 'in_progress'",
            (now_iso(), project_id),
        )
        recovered = cur.rowcount

        # Re-block descendants of every task currently in failed/blocked state so a
        # crash that strands a parent in failed/blocked with children in approved can
        # never leave those children claimable after recovery.
        cur = await conn.execute(
            "SELECT id FROM tasks WHERE project_id = ? AND state IN ('failed', 'blocked')",
            (project_id,),
        )
        parents = [r["id"] for r in await cur.fetchall()]
        for parent_id in parents:
            reblocked += await _block_descendants(conn, parent_id)

    return recovered, reblocked


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


async def list_verdicts(
    db: Database, task_id: str, limit: int | None = None, offset: int = 0
) -> list[VerdictRow]:
    if limit is not None:
        rows = await db.fetchall(
            "SELECT * FROM verdicts WHERE task_id = ? ORDER BY attempt, created_at "
            "LIMIT ? OFFSET ?",
            (task_id, limit, offset),
        )
    else:
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
    planning_session_id: str | None = None,
    agent: str | None = None,
    payload: dict[str, Any] | None = None,
) -> EventRow:
    ts = now_iso()
    async with db._write() as conn:
        cur = await conn.execute(
            "INSERT INTO events(project_id, task_id, planning_session_id, agent, type, "
            "payload_json, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (project_id, task_id, planning_session_id, agent, type, json.dumps(payload or {}), ts),
        )
        last_id = int(cur.lastrowid or 0)
    return EventRow(
        id=last_id,
        project_id=project_id,
        task_id=task_id,
        planning_session_id=planning_session_id,
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


async def gc_events(db: Database, project_id: str, keep: int = 10_000) -> None:
    """Prune ``events`` rows beyond the most-recent *keep* per project.

    Uses the same subquery-DELETE pattern as :func:`gc_baselines`: keeps the highest-id
    rows and drops the rest.  The DELETE is cheap when the row count is below *keep*
    (the subquery returns all ids, so none match the NOT IN).  The operation runs
    through the standard serialized write so it composes safely with concurrent appends.
    """
    await db.execute(
        "DELETE FROM events WHERE project_id = ? AND id NOT IN "
        "(SELECT id FROM events WHERE project_id = ? ORDER BY id DESC LIMIT ?)",
        (project_id, project_id, keep),
    )


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
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_creation_tokens: int | None = None,
) -> None:
    await db.execute(
        "INSERT INTO usage(id, project_id, task_id, agent, model_reported, cost_usd, "
        "num_turns, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id(), project_id, task_id, agent, model_reported, cost_usd, num_turns,
            input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, now_iso(),
        ),
    )


async def usage_summary(db: Database, project_id: str) -> dict[str, Any]:
    row = await db.fetchone(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0.0) AS total_cost "
        "FROM usage WHERE project_id = ?",
        (project_id,),
    )
    assert row is not None  # COUNT(*) always yields a row
    return {"calls": int(row["calls"]), "total_cost_usd": float(row["total_cost"])}


async def get_task_usage(db: Database, task_id: str) -> dict[str, Any]:
    """Return token-usage totals for a single task computed entirely in SQL (DoS hardening — WEB-1).

    Uses ``COALESCE(SUM(...), 0)`` so a single aggregate row is returned regardless of how many
    usage rows exist — no Python-side iteration over unbounded rows.
    """
    row = await db.fetchone(
        "SELECT COALESCE(SUM(num_turns), 0) AS total_turns, "
        "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
        "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
        "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
        "COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens, "
        "COUNT(*) AS agent_count "
        "FROM usage WHERE task_id = ?",
        (task_id,),
    )
    assert row is not None  # COUNT(*) always yields a row
    return {
        "task_id": task_id,
        "total_turns": int(row["total_turns"]),
        "input_tokens": int(row["input_tokens"]),
        "output_tokens": int(row["output_tokens"]),
        "cache_read_tokens": int(row["cache_read_tokens"]),
        "cache_creation_tokens": int(row["cache_creation_tokens"]),
        "agent_count": int(row["agent_count"]),
    }


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


async def list_quarantine(
    db: Database, project_id: str, limit: int | None = None, offset: int = 0
) -> list[QuarantineEntry]:
    if limit is not None:
        rows = await db.fetchall(
            "SELECT * FROM quarantine WHERE project_id = ? ORDER BY until LIMIT ? OFFSET ?",
            (project_id, limit, offset),
        )
    else:
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
    db: Database, project_id: str | None = None,
    limit: int | None = None, offset: int = 0,
) -> list[EngineProfileRow]:
    if limit is not None:
        rows = await db.fetchall(
            "SELECT * FROM engine_profiles WHERE project_id IS NULL OR project_id = ? "
            "ORDER BY name LIMIT ? OFFSET ?",
            (project_id, limit, offset),
        )
    else:
        rows = await db.fetchall(
            "SELECT * FROM engine_profiles WHERE project_id IS NULL OR project_id = ? "
            "ORDER BY name",
            (project_id,),
        )
    return [EngineProfileRow.from_row(r) for r in rows]


async def delete_engine_profile(db: Database, profile_id: str) -> None:
    await db.execute("DELETE FROM engine_profiles WHERE id = ?", (profile_id,))


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


async def list_agents(
    db: Database, project_id: str | None = None,
    limit: int | None = None, offset: int = 0,
) -> list[AgentPersona]:
    if limit is not None:
        rows = await db.fetchall(
            "SELECT * FROM agents WHERE project_id IS NULL OR project_id = ? "
            "ORDER BY name LIMIT ? OFFSET ?",
            (project_id, limit, offset),
        )
    else:
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
    ai_enriched: bool = False,
) -> RepoKnowledgeRow:
    prev = await db.fetchval(
        "SELECT COALESCE(MAX(version), 0) FROM repo_knowledge WHERE project_id = ?", (project_id,)
    )
    version = int(prev) + 1
    rid = new_id()
    await db.execute(
        "INSERT INTO repo_knowledge(id, project_id, version, sha, manifest_fingerprint, "
        "ai_enriched, knowledge_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rid, project_id, version, sha, manifest_fingerprint,
            int(ai_enriched), json.dumps(knowledge), now_iso(),
        ),
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


async def latest_ai_knowledge(db: Database, project_id: str) -> RepoKnowledgeRow | None:
    """Return the most recent AI-enriched knowledge row, or None."""
    row = await db.fetchone(
        "SELECT * FROM repo_knowledge WHERE project_id = ? AND ai_enriched = 1 "
        "ORDER BY version DESC LIMIT 1",
        (project_id,),
    )
    return RepoKnowledgeRow.from_row(row) if row else None


# --- planning sessions --------------------------------------------------------


async def create_planning_session(
    db: Database,
    *,
    project_id: str,
    title: str,
    prompt: str,
    max_rounds: int = 5,
) -> PlanningSession:
    sid = new_id()
    ts = now_iso()
    await db.execute(
        "INSERT INTO planning_sessions(id, project_id, title, prompt, status, "
        "turn_number, max_rounds, created_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
        (sid, project_id, title, prompt, PlanningSessionStatus.active.value, max_rounds, ts),
    )
    session = await get_planning_session(db, sid)
    assert session is not None
    return session


async def get_planning_session(db: Database, session_id: str) -> PlanningSession | None:
    row = await db.fetchone(
        "SELECT * FROM planning_sessions WHERE id = ?", (session_id,)
    )
    return PlanningSession.from_row(row) if row else None


async def list_planning_sessions(
    db: Database, project_id: str, limit: int | None = None, offset: int = 0
) -> list[PlanningSession]:
    if limit is not None:
        rows = await db.fetchall(
            "SELECT * FROM planning_sessions WHERE project_id = ? ORDER BY created_at DESC "
            "LIMIT ? OFFSET ?",
            (project_id, limit, offset),
        )
    else:
        rows = await db.fetchall(
            "SELECT * FROM planning_sessions WHERE project_id = ? ORDER BY created_at DESC",
            (project_id,),
        )
    return [PlanningSession.from_row(r) for r in rows]


async def update_planning_session_status(
    db: Database, session_id: str, status: PlanningSessionStatus
) -> None:
    completed_at = now_iso() if status in (
        PlanningSessionStatus.completed, PlanningSessionStatus.cancelled
    ) else None
    await db.execute(
        "UPDATE planning_sessions SET status = ?, completed_at = ? WHERE id = ?",
        (status.value, completed_at, session_id),
    )


async def update_planning_session_stabilization_reason(
    db: Database, session_id: str, reason: str
) -> None:
    """Persist a short description of why the session auto-stabilised."""
    await db.execute(
        "UPDATE planning_sessions SET stabilization_reason = ? WHERE id = ?",
        (reason, session_id),
    )


async def increment_planning_turn(db: Database, session_id: str) -> int:
    async with db._write() as conn:
        cur = await conn.execute(
            "UPDATE planning_sessions SET turn_number = turn_number + 1 WHERE id = ? "
            "RETURNING turn_number",
            (session_id,),
        )
        row = await cur.fetchone()
    return int(row["turn_number"]) if row else 0


# --- planning messages --------------------------------------------------------


async def add_planning_message(
    db: Database,
    *,
    session_id: str,
    agent: str,
    role: str,
    content: str,
    turn_number: int,
    parent_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> PlanningMessage:
    mid = new_id()
    await db.execute(
        "INSERT INTO planning_messages(id, session_id, agent, role, content, "
        "turn_number, parent_id, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (mid, session_id, agent, role, content, turn_number, parent_id,
         json.dumps(metadata or {}), now_iso()),
    )
    msg = await _get_planning_message(db, mid)
    assert msg is not None
    return msg


async def add_message_with_turn(
    db: Database,
    *,
    session_id: str,
    agent: str,
    role: str,
    content: str,
    parent_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> PlanningMessage:
    """Bump the session turn counter and insert a message stamped with that turn — in ONE
    transaction, so a turn number can never be consumed without its message landing (nor a
    message stored against a turn the counter never advanced to).
    """
    mid = new_id()
    async with db.transaction() as conn:
        cur = await conn.execute(
            "UPDATE planning_sessions SET turn_number = turn_number + 1 WHERE id = ? "
            "RETURNING turn_number",
            (session_id,),
        )
        row = await cur.fetchone()
        turn = int(row["turn_number"]) if row else 0
        await conn.execute(
            "INSERT INTO planning_messages(id, session_id, agent, role, content, "
            "turn_number, parent_id, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (mid, session_id, agent, role, content, turn, parent_id,
             json.dumps(metadata or {}), now_iso()),
        )
    msg = await _get_planning_message(db, mid)
    assert msg is not None
    return msg


async def _get_planning_message(db: Database, message_id: str) -> PlanningMessage | None:
    row = await db.fetchone(
        "SELECT * FROM planning_messages WHERE id = ?", (message_id,)
    )
    return PlanningMessage.from_row(row) if row else None


async def list_planning_messages(
    db: Database, session_id: str, limit: int | None = None, offset: int = 0
) -> list[PlanningMessage]:
    if limit is not None:
        rows = await db.fetchall(
            "SELECT * FROM planning_messages WHERE session_id = ? "
            "ORDER BY turn_number, id LIMIT ? OFFSET ?",
            (session_id, limit, offset),
        )
    else:
        rows = await db.fetchall(
            "SELECT * FROM planning_messages WHERE session_id = ? ORDER BY turn_number, id",
            (session_id,),
        )
    return [PlanningMessage.from_row(r) for r in rows]


# --- planning task nodes ------------------------------------------------------


async def add_planning_task_node(
    db: Database,
    *,
    session_id: str,
    parent_id: str | None,
    title: str,
    description: str,
    level: int,
    sort_order: int,
) -> PlanningTaskNode:
    nid = new_id()
    ts = now_iso()
    await db.execute(
        "INSERT INTO planning_task_nodes(id, session_id, parent_id, title, description, "
        "status, level, sort_order, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (nid, session_id, parent_id, title, description,
         PlanningNodeStatus.proposed.value, level, sort_order, ts, ts),
    )
    node = await get_planning_task_node(db, nid)
    assert node is not None
    return node


async def get_planning_task_node(db: Database, node_id: str) -> PlanningTaskNode | None:
    row = await db.fetchone(
        "SELECT * FROM planning_task_nodes WHERE id = ?", (node_id,)
    )
    return PlanningTaskNode.from_row(row) if row else None


async def update_planning_task_node(
    db: Database,
    *,
    node_id: str,
    title: str | None = None,
    description: str | None = None,
    status: str | None = None,
    task_id: str | None = None,
) -> None:
    sets: list[str] = []
    params: list[Any] = []
    if title is not None:
        sets.append("title = ?")
        params.append(title)
    if description is not None:
        sets.append("description = ?")
        params.append(description)
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if task_id is not None:
        sets.append("task_id = ?")
        params.append(task_id)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(now_iso())
    params.append(node_id)
    await db.execute(
        f"UPDATE planning_task_nodes SET {', '.join(sets)} WHERE id = ?", tuple(params)
    )


async def delete_planning_task_node(db: Database, node_id: str) -> None:
    # Null out parent references on children first
    await db.execute(
        "UPDATE planning_task_nodes SET parent_id = NULL WHERE parent_id = ?", (node_id,)
    )
    await db.execute("DELETE FROM planning_task_nodes WHERE id = ?", (node_id,))


async def list_planning_task_nodes(
    db: Database, session_id: str, limit: int | None = None, offset: int = 0
) -> list[PlanningTaskNode]:
    if limit is not None:
        rows = await db.fetchall(
            "SELECT * FROM planning_task_nodes WHERE session_id = ? "
            "ORDER BY level, sort_order LIMIT ? OFFSET ?",
            (session_id, limit, offset),
        )
    else:
        rows = await db.fetchall(
            "SELECT * FROM planning_task_nodes WHERE session_id = ? "
            "ORDER BY level, sort_order",
            (session_id,),
        )
    return [PlanningTaskNode.from_row(r) for r in rows]


async def list_planning_task_nodes_by_parent(
    db: Database, session_id: str, parent_id: str | None
) -> list[PlanningTaskNode]:
    if parent_id is None:
        rows = await db.fetchall(
            "SELECT * FROM planning_task_nodes WHERE session_id = ? AND parent_id IS NULL "
            "ORDER BY sort_order",
            (session_id,),
        )
    else:
        rows = await db.fetchall(
            "SELECT * FROM planning_task_nodes WHERE session_id = ? AND parent_id = ? "
            "ORDER BY sort_order",
            (session_id, parent_id),
        )
    return [PlanningTaskNode.from_row(r) for r in rows]
