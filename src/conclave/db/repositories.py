"""Typed repository functions over :class:`Database`.

Plain async functions (not classes) keep call sites explicit and mypy-friendly.
Each returns domain row models from :mod:`conclave.db.models`.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from typing import Any

import aiosqlite

from ..util import new_id, now_iso
from .database import Database
from .models import (
    BUG_STATUS_TRANSITIONS,
    AgentPersona,
    Baseline,
    BugCandidate,
    BugStatus,
    CheckpointStatus,
    CoverageRegion,
    EventRow,
    IdempotencyRecord,
    IllegalBugTransition,
    InteractionLog,
    Project,
    ProjectMode,
    QuarantineEntry,
    StoreItem,
    StoreItemKind,
    Task,
    TaskCheckpoint,
    TaskOrigin,
    TaskState,
    VerdictRow,
    normalize_dict_keys,
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
    norm_cfg = normalize_dict_keys(config or {})
    await db.execute(
        "INSERT INTO projects(id, name, path, default_branch, mode, config_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            pid,
            name,
            path,
            default_branch,
            ProjectMode.task_queue.value,
            json.dumps(norm_cfg),
            ts,
        ),
    )
    project = await get_project(db, pid)
    assert project is not None
    return project


async def get_project(db: Database, project_id: str) -> Project | None:
    row = await db.fetchone("SELECT * FROM projects WHERE id = ?", (project_id,))
    return Project.from_row(row) if row else None


async def get_project_by_path(db: Database, path: str) -> Project | None:
    row = await db.fetchone(
        "SELECT * FROM projects WHERE path = ? ORDER BY created_at LIMIT 1", (path,)
    )
    return Project.from_row(row) if row else None


async def update_project(
    db: Database,
    project_id: str,
    *,
    name: str | None = None,
    default_branch: str | None = None,
    config: dict[str, Any] | None = None,
) -> Project | None:
    sets: list[str] = []
    params: list[Any] = []
    if name is not None:
        sets.append("name = ?")
        params.append(name)
    if default_branch is not None:
        sets.append("default_branch = ?")
        params.append(default_branch)
    if config is not None:
        sets.append("config_json = ?")
        params.append(json.dumps(normalize_dict_keys(config)))
    if sets:
        params.append(project_id)
        await db.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", tuple(params))
    return await get_project(db, project_id)


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
        "UPDATE projects SET config_json = ? WHERE id = ?",
        (json.dumps(normalize_dict_keys(config)), project_id),
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
    swarm_id: str | None = None,
    source_branch: str | None = None,
) -> Task:
    tid = new_id()
    ts = now_iso()
    up = None if use_planner is None else int(use_planner)
    norm_source = source_branch.strip() if source_branch and source_branch.strip() else None
    await db.execute(
        "INSERT INTO tasks(id, project_id, title, request, level, state, use_planner, origin, "
        "parent_task_id, swarm_id, source_branch, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            tid, project_id, title, request, level, state.value, up, origin.value,
            parent_task_id, swarm_id, norm_source, ts, ts,
        ),
    )
    task = await get_task(db, tid)
    assert task is not None
    return task


async def list_swarm_tasks(db: Database, project_id: str, swarm_id: str) -> list[Task]:
    rows = await db.fetchall(
        "SELECT * FROM tasks WHERE project_id = ? AND swarm_id = ? ORDER BY created_at ASC",
        (project_id, swarm_id),
    )
    return [Task.from_row(r) for r in rows]


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


async def delete_task(db: Database, task_id: str) -> bool:
    """Atomically delete a task and its orphaned child rows.

    ``attempts``, ``verdicts``, and ``task_checkpoints`` auto-cascade via
    ``ON DELETE CASCADE`` FK. ``events`` and ``usage`` have no FK — they are
    deleted explicitly inside the same transaction so nothing is left dangling.

    The ``WHERE state != 'in_progress'`` guard is defense-in-depth against a
    TOCTOU between the API-layer check and this delete: if the task was claimed
    between the read and the write the DELETE simply won't match (zero rows).

    Returns ``True`` if the task row was deleted, ``False`` if it was
    ``in_progress`` (or didn't exist).
    """
    async with db.transaction() as conn:
        # Remove orphan-prone child rows first — events and usage lack FK cascades.
        await conn.execute("DELETE FROM events WHERE task_id = ?", (task_id,))
        await conn.execute("DELETE FROM usage WHERE task_id = ?", (task_id,))
        # Delete the task; attempts, verdicts, and task_checkpoints cascade via FK.
        cur = await conn.execute(
            "DELETE FROM tasks WHERE id = ? AND state != 'in_progress'",
            (task_id,),
        )
        return cur.rowcount > 0


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
            "WHERE id = ? AND state IN ('inbox', 'failed', 'waiting_for_approval')",
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
    source_branch: str | None = None,
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
    if source_branch is not None:
        sets.append("source_branch = ?")
        params.append(source_branch)
    if result_summary is not None:
        sets.append("result_summary = ?")
        params.append(result_summary)
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(now_iso())
    params.append(task_id)
    await db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", tuple(params))


async def claim_next_approved(
    db: Database, project_id: str, *, max_parallel: int = 1
) -> Task | None:
    """Atomically pick the oldest claimable ``approved`` task and mark it ``in_progress``.

    Claimability model: a task is claimable iff it has no parent OR its parent has completed
    successfully (``state = 'done'``). Every other parent state keeps the child unclaimable —
    both not-yet-terminal (inbox/approved/in_progress), so a subtask can never run before its
    parent finishes, and terminal-non-success (failed/blocked/cancelled), so a doomed subtree
    is never executed. The ``parent_task_id IS NULL`` branch is load-bearing: when no 'done'
    rows exist the inner subquery is empty and ``IN (<empty>)`` is false, which would otherwise
    stop parentless tasks from ever being claimed.

    Concurrency invariant: at most ``max_parallel`` tasks per project may be ``in_progress``
    simultaneously (default ``1`` enforces single-flight execution; ``> 1`` enables Swarm
    parallelism across dedicated isolated worktrees).
    """
    cap = max(1, int(max_parallel))
    async with db._write() as conn:
        cur = await conn.execute(
            "UPDATE tasks SET state = 'in_progress', updated_at = ? "
            "WHERE id = ("
            "  SELECT t.id FROM tasks t "
            "  WHERE t.project_id = ? AND t.state = 'approved' "
            "  AND ("
            "    SELECT COUNT(*) FROM tasks r "
            "    WHERE r.project_id = t.project_id AND r.state = 'in_progress'"
            "  ) < ? "
            "  AND ("
            "    t.parent_task_id IS NULL OR t.parent_task_id IN ("
            "      SELECT id FROM tasks WHERE state = 'done'"
            "    )"
            "  )"
            "  ORDER BY t.created_at LIMIT 1"
            ") RETURNING *",
            (now_iso(), project_id, cap),
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


async def get_in_progress_tasks(db: Database, project_id: str) -> list[Task]:
    """Return all tasks currently ``in_progress`` for a project.

    Used by :meth:`Daemon.cleanup_in_progress_work` when detaching a project so the
    caller can tear down worktrees before the project row (and its FK-cascaded
    children) are deleted.
    """
    rows = await db.fetchall(
        "SELECT * FROM tasks WHERE project_id = ? AND state = 'in_progress'",
        (project_id,),
    )
    return [Task.from_row(r) for r in rows]


async def recover_in_progress(db: Database, project_id: str) -> tuple[int, int]:
    """Reset orphaned ``in_progress`` tasks to ``approved``, reset ``fixing`` bug
    candidates back to ``reproduced``, and re-block descendants of ``failed``/``blocked``
    parents (crash recovery).

    Returns ``(recovered, reblocked)`` where *recovered* is the count of ``in_progress``
    tasks reset to ``approved`` and *reblocked* is the count of descendant tasks that
    were set to ``blocked`` because their parent is still ``failed``/``blocked``.

    Runs in a single transaction so an observer can never see recovered-but-not-yet-
    reblocked state — the recovery atomically resets orphans AND re-applies blocking.
    """
    recovered = 0
    reblocked = 0
    async with db.transaction() as conn:
        ts = now_iso()
        cur = await conn.execute(
            "UPDATE tasks SET state = 'approved', updated_at = ? "
            "WHERE project_id = ? AND state = 'in_progress'",
            (ts, project_id),
        )
        recovered = cur.rowcount

        await conn.execute(
            "UPDATE bug_candidates SET status = 'reproduced', last_examined_at = ? "
            "WHERE project_id = ? AND status = 'fixing'",
            (ts, project_id),
        )

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


async def delete_agent(
    db: Database, name: str, project_id: str | None = None
) -> bool:
    scope = project_id or ""
    async with db._write() as conn:
        cur = await conn.execute(
            "DELETE FROM agents WHERE IFNULL(project_id, '') = ? AND name = ?",
            (scope, name),
        )
        return cur.rowcount > 0


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


# --- bug candidates (Bug-Fixer ledger) --------------------------------------


async def create_bug_candidate(
    db: Database,
    *,
    project_id: str,
    fingerprint: str,
    claim: str,
    file: str | None = None,
    symbol: str | None = None,
    region: str | None = None,
    severity: str | None = None,
    notes: str | None = None,
) -> BugCandidate:
    """Insert a new candidate, or return the existing one on a (project_id, fingerprint) clash.

    Dedupe is structural: ``idx_bug_fingerprint`` makes (project_id, fingerprint) unique and
    ``ON CONFLICT DO NOTHING`` turns a re-report of the same fingerprint into a true no-op that
    preserves the original row — its id, status and accumulated history are untouched. The
    follow-up SELECT runs in the SAME transaction so a concurrent creator can't interleave, and
    it returns whichever row now owns that fingerprint, so callers get a stable handle without
    having to inspect rowcount or distinguish insert-from-conflict.
    """
    ts = now_iso()
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT INTO bug_candidates (id, project_id, fingerprint, file, symbol, region, "
            "claim, severity, notes, discovered_at, last_examined_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project_id, fingerprint) DO NOTHING",
            (new_id(), project_id, fingerprint, file, symbol, region, claim, severity, notes,
             ts, ts),
        )
        cur = await conn.execute(
            "SELECT * FROM bug_candidates WHERE project_id = ? AND fingerprint = ?",
            (project_id, fingerprint),
        )
        row = await cur.fetchone()
    assert row is not None
    return BugCandidate.from_row(row)


async def get_bug_candidate(db: Database, candidate_id: str) -> BugCandidate | None:
    row = await db.fetchone("SELECT * FROM bug_candidates WHERE id = ?", (candidate_id,))
    return BugCandidate.from_row(row) if row else None


async def get_bug_candidate_by_fingerprint(
    db: Database, project_id: str, fingerprint: str
) -> BugCandidate | None:
    """Look up a candidate by its (project_id, fingerprint) identity, or ``None``.

    The discovery routine reads this BEFORE a create to decide whether a parsed candidate is
    genuinely new (worth a ``bug.discovered``) or a re-report of one already in the ledger (a
    silent no-op). Structural dedupe still lives in :func:`create_bug_candidate`; this is only
    the event gate, so a found row here means "do not re-announce", not "do not store".
    """
    row = await db.fetchone(
        "SELECT * FROM bug_candidates WHERE project_id = ? AND fingerprint = ?",
        (project_id, fingerprint),
    )
    return BugCandidate.from_row(row) if row else None


async def list_bug_candidates(
    db: Database,
    project_id: str,
    status: BugStatus | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[BugCandidate]:
    """List a project's candidates newest-first, optionally filtered to a single status.

    ``status`` is bound as a parameter (never interpolated) so the permissive TEXT column is
    queried safely; the enum's ``.value`` is the only thing that reaches SQLite.
    """
    clauses = ["project_id = ?"]
    params: list[Any] = [project_id]
    if status is not None:
        clauses.append("status = ?")
        params.append(status.value)
    sql = f"SELECT * FROM bug_candidates WHERE {' AND '.join(clauses)} ORDER BY discovered_at DESC"
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    rows = await db.fetchall(sql, tuple(params))
    return [BugCandidate.from_row(r) for r in rows]


async def list_needs_human(db: Database, project_id: str) -> list[BugCandidate]:
    """The human work-queue: candidates parked in ``declined_needs_human`` for this project."""
    return await list_bug_candidates(db, project_id, status=BugStatus.declined_needs_human)


async def set_repro_artifacts(
    db: Database, candidate_id: str, *, path: str, body: str, hash: str
) -> None:
    """Attach the reproduction-test artifact (path, body, content hash) to a candidate.

    Refreshes ``last_examined_at`` too — capturing a repro is itself an examination of the
    candidate. The status is deliberately NOT touched here: advancing discovered → reproduced is
    the controller's explicit :func:`transition_bug_status` call, kept separate so the artifact
    write and the guarded state change remain independently auditable.
    """
    await db.execute(
        "UPDATE bug_candidates SET repro_test_path = ?, repro_test_body = ?, "
        "repro_test_hash = ?, last_examined_at = ? WHERE id = ?",
        (path, body, hash, now_iso(), candidate_id),
    )


async def transition_bug_status(
    db: Database,
    candidate_id: str,
    target: BugStatus,
    *,
    task_id: str | None = None,
    decline_reason: str | None = None,
) -> BugCandidate:
    """Advance a candidate to ``target``, guarded by :data:`BUG_STATUS_TRANSITIONS`.

    The whole read-check-write runs in one transaction so the guard sees a consistent current
    status and two coroutines can't both drive the same candidate off one stale observation. The
    current status is read through :meth:`BugCandidate.from_row`, so a corrupt stored status has
    already degraded to the terminal ``declined_needs_human`` and every onward edge is rejected —
    a garbled candidate can never be auto-advanced.

    Side effects stamped atomically with the status flip:

    * ``last_examined_at`` is always refreshed — a transition is activity on the candidate.
    * entering ``fixing`` bumps ``attempts`` (one in-flight auto-fix == one attempt); this is the
      counter the controller meters to decide when ``reproduced`` must escalate to a human.
    * reaching ``fixed`` stamps ``fixed_at``.
    * ``task_id`` / ``decline_reason`` are linked when supplied — the driving task, and the
      human-handoff note that rides along the reproduced → declined_needs_human edge.

    Raises :class:`IllegalBugTransition` when the candidate is missing or the edge is not in the
    table; an illegal edge is a controller bug, surfaced loudly rather than silently dropped.
    """
    async with db.transaction() as conn:
        cur = await conn.execute(
            "SELECT * FROM bug_candidates WHERE id = ?", (candidate_id,)
        )
        row = await cur.fetchone()
        if row is None:
            raise IllegalBugTransition(f"bug candidate {candidate_id!r} does not exist")
        current = BugCandidate.from_row(row)
        if target not in BUG_STATUS_TRANSITIONS[current.status]:
            raise IllegalBugTransition(
                f"illegal bug transition {current.status.value} → {target.value}"
            )

        ts = now_iso()
        # Only fixed column-assignment fragments are joined into the SQL (no caller value is ever
        # formatted in); every value travels as a bound parameter, appended in lockstep with its
        # fragment so the placeholder order stays aligned. ``attempts = attempts + 1`` carries no
        # placeholder, so it is appended to the fragments WITHOUT a matching param.
        sets = ["status = ?", "last_examined_at = ?"]
        params: list[Any] = [target.value, ts]
        if target is BugStatus.fixed:
            sets.append("fixed_at = ?")
            params.append(ts)
        if target is BugStatus.fixing:
            sets.append("attempts = attempts + 1")
        if task_id is not None:
            sets.append("task_id = ?")
            params.append(task_id)
        if decline_reason is not None:
            sets.append("decline_reason = ?")
            params.append(decline_reason)
        params.append(candidate_id)
        await conn.execute(
            f"UPDATE bug_candidates SET {', '.join(sets)} WHERE id = ?", tuple(params)
        )

        cur = await conn.execute(
            "SELECT * FROM bug_candidates WHERE id = ?", (candidate_id,)
        )
        updated = await cur.fetchone()
    assert updated is not None
    return BugCandidate.from_row(updated)


# --- coverage regions (Bug-Fixer region scheduler) --------------------------


async def upsert_coverage_region(
    db: Database,
    *,
    project_id: str,
    region: str,
    priority: int | None = None,
    last_examined_at: str | None = None,
    examined_count: int | None = None,
) -> CoverageRegion:
    """Register a coverage region or update its scheduler fields, keyed on (project_id, region).

    Read-modify-write under one transaction (``idx_coverage_region`` keeps the pair unique). On
    a fresh region the supplied values seed it, falling back to the column defaults (priority 0,
    examined_count 0, last_examined_at NULL); on an existing one only the explicitly-supplied
    (non-``None``) fields are overwritten, so a caller can bump priority without clobbering the
    examination history, or stamp ``last_examined_at`` without resetting priority.
    """
    async with db.transaction() as conn:
        cur = await conn.execute(
            "SELECT id FROM coverage WHERE project_id = ? AND region = ?",
            (project_id, region),
        )
        existing = await cur.fetchone()
        if existing is None:
            await conn.execute(
                "INSERT INTO coverage (id, project_id, region, last_examined_at, priority, "
                "examined_count) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    new_id(),
                    project_id,
                    region,
                    last_examined_at,
                    priority if priority is not None else 0,
                    examined_count if examined_count is not None else 0,
                ),
            )
        else:
            sets: list[str] = []
            params: list[Any] = []
            if priority is not None:
                sets.append("priority = ?")
                params.append(priority)
            if last_examined_at is not None:
                sets.append("last_examined_at = ?")
                params.append(last_examined_at)
            if examined_count is not None:
                sets.append("examined_count = ?")
                params.append(examined_count)
            if sets:
                params.append(existing["id"])
                await conn.execute(
                    f"UPDATE coverage SET {', '.join(sets)} WHERE id = ?", tuple(params)
                )
        cur = await conn.execute(
            "SELECT * FROM coverage WHERE project_id = ? AND region = ?",
            (project_id, region),
        )
        row = await cur.fetchone()
    assert row is not None
    return CoverageRegion.from_row(row)


async def select_next_region(db: Database, project_id: str) -> CoverageRegion | None:
    """Pick the single region to examine next, or ``None`` when the project has no regions.

    Ordering is least-recently-examined first, highest-priority breaking ties. SQLite sorts
    NULLs first on an ascending key, so never-examined regions (``last_examined_at IS NULL``)
    naturally lead — the scheduler sweeps unexplored ground before revisiting. ``region`` is the
    final tiebreak purely to make the choice deterministic when both keys are equal.
    """
    row = await db.fetchone(
        "SELECT * FROM coverage WHERE project_id = ? "
        "ORDER BY last_examined_at ASC, priority DESC, region ASC LIMIT 1",
        (project_id,),
    )
    return CoverageRegion.from_row(row) if row else None


async def list_coverage_regions(db: Database, project_id: str) -> list[CoverageRegion]:
    """Every coverage region for a project, in :func:`select_next_region`'s canonical order.

    Same ordering key (least-recently-examined first, highest priority breaking ties, region
    name as the deterministic final tiebreak) — this is the full-list form the region scheduler
    walks when it must drop ``ignore_patterns`` matches that the single-row ``LIMIT 1`` SQL of
    :func:`select_next_region` cannot express.
    """
    rows = await db.fetchall(
        "SELECT * FROM coverage WHERE project_id = ? "
        "ORDER BY last_examined_at ASC, priority DESC, region ASC",
        (project_id,),
    )
    return [CoverageRegion.from_row(r) for r in rows]


# --- task checkpoints -------------------------------------------------------


async def save_task_checkpoint(
    db: Database,
    *,
    task_id: str,
    node_id: str,
    attempt: int = 1,
    status: CheckpointStatus = CheckpointStatus.completed,
    git_tree_sha: str | None = None,
    artifact: dict[str, Any] | None = None,
) -> TaskCheckpoint:
    ts = now_iso()
    payload = artifact or {}
    await db.execute(
        "INSERT OR REPLACE INTO task_checkpoints("
        "task_id, node_id, attempt, status, git_tree_sha, artifact_json, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?)",
        (task_id, node_id, attempt, status.value, git_tree_sha, json.dumps(payload), ts),
    )
    return TaskCheckpoint(
        task_id=task_id,
        node_id=node_id,
        attempt=attempt,
        status=status,
        git_tree_sha=git_tree_sha,
        artifact=payload,
        updated_at=ts,
    )


async def list_task_checkpoints(
    db: Database, task_id: str, attempt: int | None = None
) -> list[TaskCheckpoint]:
    if attempt is not None:
        rows = await db.fetchall(
            "SELECT * FROM task_checkpoints WHERE task_id = ? AND attempt = ? "
            "ORDER BY attempt ASC, updated_at ASC",
            (task_id, attempt),
        )
    else:
        rows = await db.fetchall(
            "SELECT * FROM task_checkpoints WHERE task_id = ? "
            "ORDER BY attempt ASC, updated_at ASC",
            (task_id,),
        )
    return [TaskCheckpoint.from_row(r) for r in rows]


async def get_task_checkpoint(
    db: Database, task_id: str, node_id: str, attempt: int = 1
) -> TaskCheckpoint | None:
    row = await db.fetchone(
        "SELECT * FROM task_checkpoints WHERE task_id = ? AND node_id = ? AND attempt = ?",
        (task_id, node_id, attempt),
    )
    return TaskCheckpoint.from_row(row) if row else None


async def clear_task_checkpoints(
    db: Database, task_id: str, *, after_attempt: int | None = None
) -> None:
    if after_attempt is None:
        await db.execute("DELETE FROM task_checkpoints WHERE task_id = ?", (task_id,))
    else:
        await db.execute(
            "DELETE FROM task_checkpoints WHERE task_id = ? AND attempt >= ?",
            (task_id, after_attempt),
        )


# --- idempotency keys -------------------------------------------------------


async def get_idempotency_record(db: Database, key: str) -> IdempotencyRecord | None:
    row = await db.fetchone("SELECT * FROM idempotency_keys WHERE key = ?", (key,))
    return IdempotencyRecord.from_row(row) if row else None


async def save_idempotency_record(
    db: Database,
    *,
    key: str,
    method: str,
    path: str,
    status_code: int,
    response: Any,
) -> IdempotencyRecord:
    ts = now_iso()
    async with db.transaction() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO idempotency_keys("
            "key, method, path, status_code, response_json, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (key, method, path, status_code, json.dumps(response), ts),
        )
        await conn.execute(
            "DELETE FROM idempotency_keys WHERE key NOT IN ("
            "SELECT key FROM idempotency_keys ORDER BY created_at DESC LIMIT 1000"
            ")"
        )
    return IdempotencyRecord(
        key=key,
        method=method,
        path=path,
        status_code=status_code,
        response=response,
        created_at=ts,
    )


# --- global store items (Workflows, Agents, Config Presets) -----------------


async def upsert_store_item(
    db: Database,
    *,
    kind: StoreItemKind,
    name: str,
    description: str = "",
    tags: list[str] | None = None,
    payload: dict[str, Any],
    built_in: bool = False,
) -> StoreItem:
    ts = now_iso()
    norm_payload = normalize_dict_keys(payload)
    existing = await db.fetchone(
        "SELECT id, created_at FROM store_items WHERE kind = ? AND name = ?",
        (kind.value, name),
    )
    if existing is not None:
        sid = str(existing["id"])
        await db.execute(
            "UPDATE store_items SET description = ?, tags_json = ?, payload_json = ?, "
            "built_in = ?, updated_at = ? WHERE id = ?",
            (
                description,
                json.dumps(tags or []),
                json.dumps(norm_payload),
                int(built_in),
                ts,
                sid,
            ),
        )
    else:
        sid = new_id()
        await db.execute(
            "INSERT INTO store_items("
            "id, kind, name, description, tags_json, payload_json, built_in, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sid,
                kind.value,
                name,
                description,
                json.dumps(tags or []),
                json.dumps(norm_payload),
                int(built_in),
                ts,
                ts,
            ),
        )
    row = await db.fetchone("SELECT * FROM store_items WHERE id = ?", (sid,))
    assert row is not None
    return StoreItem.from_row(row)


async def get_store_item(db: Database, item_id: str) -> StoreItem | None:
    row = await db.fetchone("SELECT * FROM store_items WHERE id = ?", (item_id,))
    return StoreItem.from_row(row) if row else None


async def get_store_item_by_name(
    db: Database, kind: StoreItemKind, name: str
) -> StoreItem | None:
    row = await db.fetchone(
        "SELECT * FROM store_items WHERE kind = ? AND name = ?",
        (kind.value, name),
    )
    return StoreItem.from_row(row) if row else None


async def list_store_items(
    db: Database,
    *,
    kind: StoreItemKind | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[StoreItem]:
    if kind is not None:
        rows = await db.fetchall(
            "SELECT * FROM store_items WHERE kind = ? "
            "ORDER BY built_in DESC, name ASC LIMIT ? OFFSET ?",
            (kind.value, limit, offset),
        )
    else:
        rows = await db.fetchall(
            "SELECT * FROM store_items "
            "ORDER BY kind ASC, built_in DESC, name ASC LIMIT ? OFFSET ?",
            (limit, offset),
        )
    return [StoreItem.from_row(r) for r in rows]


async def delete_store_item(db: Database, item_id: str) -> bool:
    async with db._write() as conn:
        cur = await conn.execute("DELETE FROM store_items WHERE id = ?", (item_id,))
        return bool(cur.rowcount > 0)


# --- interaction logs (agent / opencode session ledger) ---------------------

_FORBIDDEN_SQL_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|DETACH|PRAGMA|VACUUM|REPLACE)\b",
    re.IGNORECASE,
)
_ALLOWED_START_RE = re.compile(r"^(SELECT|WITH)\b", re.IGNORECASE)


async def record_interaction_log(
    db: Database,
    *,
    session_id: str | None = None,
    title: str = "",
    directory: str = "",
    path: str = "",
    slug: str = "",
    model: str = "",
    agent: str = "",
    cost: float = 0.0,
    tokens_input: int = 0,
    tokens_output: int = 0,
    tokens_reasoning: int = 0,
    tokens_cache_read: int = 0,
    tokens_cache_write: int = 0,
    time_created: int | None = None,
    time_updated: int | None = None,
    created_at_str: str | None = None,
    updated_at_str: str | None = None,
    duration: float = 0.0,
    tool_calls_count: int = 0,
    message_count: int = 0,
    user_prompt: str = "",
    tool_breakdown: dict[str, int] | None = None,
    messages: list[dict[str, Any]] | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    node_id: str | None = None,
    source: str = "conclave",
) -> InteractionLog:
    sid = session_id or new_id()
    now_ms = int(time.time() * 1000)
    ts_created = time_created if time_created is not None else now_ms
    ts_updated = time_updated if time_updated is not None else ts_created
    iso = now_iso()
    c_str = created_at_str if created_at_str is not None else iso
    u_str = updated_at_str if updated_at_str is not None else c_str
    breakdown_payload = tool_breakdown or {}
    messages_payload = messages or []
    await db.execute(
        "INSERT OR REPLACE INTO interaction_logs("
        "session_id, title, directory, path, slug, model, agent, cost, "
        "tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write, "
        "time_created, time_updated, created_at_str, updated_at_str, duration, "
        "tool_calls_count, message_count, user_prompt, tool_breakdown, messages, "
        "project_id, task_id, node_id, source"
        ") VALUES ("
        "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?"
        ")",
        (
            sid,
            title,
            directory,
            path,
            slug,
            model,
            agent,
            cost,
            tokens_input,
            tokens_output,
            tokens_reasoning,
            tokens_cache_read,
            tokens_cache_write,
            ts_created,
            ts_updated,
            c_str,
            u_str,
            duration,
            tool_calls_count,
            message_count,
            user_prompt,
            json.dumps(breakdown_payload),
            json.dumps(messages_payload),
            project_id,
            task_id,
            node_id,
            source,
        ),
    )
    row = await get_interaction_log(db, sid)
    assert row is not None
    return row


async def get_interaction_log(db: Database, session_id: str) -> InteractionLog | None:
    row = await db.fetchone(
        "SELECT * FROM interaction_logs WHERE session_id = ?", (session_id,)
    )
    return InteractionLog.from_row(row) if row else None


async def list_interaction_logs(
    db: Database,
    *,
    project_id: str | None = None,
    task_id: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[InteractionLog]:
    clauses: list[str] = []
    params: list[Any] = []
    if project_id is not None:
        clauses.append("project_id = ?")
        params.append(project_id)
    if task_id is not None:
        clauses.append("task_id = ?")
        params.append(task_id)
    if agent is not None:
        clauses.append("agent = ?")
        params.append(agent)
    if model is not None:
        clauses.append("model = ?")
        params.append(model)
    if search is not None and search.strip():
        pattern = f"%{search.strip()}%"
        clauses.append(
            "(title LIKE ? OR user_prompt LIKE ? OR session_id LIKE ? "
            "OR slug LIKE ? OR agent LIKE ? OR model LIKE ? OR messages LIKE ?)"
        )
        params.extend([pattern] * 7)
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    params.extend([limit, offset])
    rows = await db.fetchall(
        f"SELECT * FROM interaction_logs {where}"
        "ORDER BY time_updated DESC, session_id DESC LIMIT ? OFFSET ?",
        tuple(params),
    )
    return [InteractionLog.from_row(r) for r in rows]


async def interaction_stats(
    db: Database, *, project_id: str | None = None
) -> dict[str, Any]:
    where = "WHERE project_id = ?" if project_id is not None else ""
    params: tuple[Any, ...] = (project_id,) if project_id is not None else ()
    row = await db.fetchone(
        "SELECT "
        "COUNT(*) AS total_sessions, "
        "COALESCE(SUM(cost), 0.0) AS total_cost, "
        "COALESCE(SUM(tokens_input), 0) AS total_input_tokens, "
        "COALESCE(SUM(tokens_output), 0) AS total_output_tokens, "
        "COALESCE(SUM(tokens_reasoning), 0) AS total_reasoning_tokens, "
        "COALESCE(SUM(tokens_cache_read), 0) AS total_cache_read_tokens, "
        "COALESCE(SUM(tokens_cache_write), 0) AS total_cache_write_tokens, "
        "COALESCE(SUM(tool_calls_count), 0) AS total_tool_calls, "
        "COALESCE(SUM(message_count), 0) AS total_messages, "
        "COALESCE(AVG(duration), 0.0) AS avg_duration "
        f"FROM interaction_logs {where}",
        params,
    )
    assert row is not None

    model_where = (
        "WHERE project_id = ? AND model != ''"
        if project_id is not None
        else "WHERE model != ''"
    )
    model_rows = await db.fetchall(
        "SELECT model, COUNT(*) AS count, COALESCE(SUM(cost), 0.0) AS cost, "
        "COALESCE(SUM(tokens_input), 0) AS tokens_input, "
        "COALESCE(SUM(tokens_output), 0) AS tokens_output "
        f"FROM interaction_logs {model_where} "
        "GROUP BY model ORDER BY count DESC, model ASC",
        params,
    )
    models: list[dict[str, Any]] = [
        {
            "model": str(r["model"]),
            "count": int(r["count"]),
            "sessions": int(r["count"]),
            "cost": float(r["cost"]),
            "tokens_input": int(r["tokens_input"]),
            "tokens_output": int(r["tokens_output"]),
        }
        for r in model_rows
    ]

    agent_where = (
        "WHERE project_id = ? AND agent != ''"
        if project_id is not None
        else "WHERE agent != ''"
    )
    agent_rows = await db.fetchall(
        "SELECT agent, COUNT(*) AS count, COALESCE(SUM(cost), 0.0) AS cost, "
        "COALESCE(SUM(tokens_input), 0) AS tokens_input, "
        "COALESCE(SUM(tokens_output), 0) AS tokens_output, "
        "COALESCE(SUM(tool_calls_count), 0) AS tool_calls "
        f"FROM interaction_logs {agent_where} "
        "GROUP BY agent ORDER BY count DESC, agent ASC",
        params,
    )
    agents: list[dict[str, Any]] = [
        {
            "agent": str(r["agent"]),
            "count": int(r["count"]),
            "sessions": int(r["count"]),
            "cost": float(r["cost"]),
            "tokens_input": int(r["tokens_input"]),
            "tokens_output": int(r["tokens_output"]),
            "tool_calls": int(r["tool_calls"]),
        }
        for r in agent_rows
    ]

    return {
        "total_sessions": int(row["total_sessions"]),
        "total_cost": float(row["total_cost"]),
        "total_input_tokens": int(row["total_input_tokens"]),
        "total_output_tokens": int(row["total_output_tokens"]),
        "total_reasoning_tokens": int(row["total_reasoning_tokens"]),
        "total_cache_read_tokens": int(row["total_cache_read_tokens"]),
        "total_cache_write_tokens": int(row["total_cache_write_tokens"]),
        "total_tool_calls": int(row["total_tool_calls"]),
        "total_messages": int(row["total_messages"]),
        "avg_duration": float(row["avg_duration"]),
        "models": models,
        "agents": agents,
    }


async def query_interaction_logs_readonly(
    db: Database, sql: str, limit: int = 200
) -> dict[str, Any]:
    cleaned = sql.strip()
    if not cleaned:
        raise ValueError("SQL query must not be empty")
    if ";" in cleaned:
        raise ValueError("Multiple statements (';') are not allowed in read-only queries")
    if not _ALLOWED_START_RE.match(cleaned):
        raise ValueError("Read-only query must start with SELECT or WITH")
    forbidden = _FORBIDDEN_SQL_RE.search(cleaned)
    if forbidden is not None:
        raise ValueError(
            f"Forbidden SQL keyword in read-only query: {forbidden.group(1).upper()}"
        )

    effective_limit = max(1, int(limit))
    try:
        cur = await db.conn.execute(cleaned)
        try:
            columns = [str(col[0]) for col in (cur.description or [])]
            raw_rows = await cur.fetchmany(effective_limit)
        finally:
            await cur.close()
    except sqlite3.Error as exc:
        raise ValueError(f"Invalid SQL query: {exc}") from exc

    rows = [dict(zip(columns, tuple(r), strict=False)) for r in raw_rows]
    return {
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
    }

