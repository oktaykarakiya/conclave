"""Unit tests for the persistence layer."""

from __future__ import annotations

import pytest

from conclave.db import Database, ProjectMode, TaskOrigin, TaskState
from conclave.db import repositories as repo
from conclave.db.planning_models import PlanningNodeStatus, PlanningSessionStatus


async def test_migrations_apply(db: Database) -> None:
    version = await db.fetchval("SELECT MAX(version) FROM schema_version")
    assert version == 5
    # idempotent: re-running connect/migrate does not error or duplicate
    await db._apply_migrations()
    assert await db.fetchval("SELECT COUNT(*) FROM schema_version") == 5


async def test_project_crud_and_config(db: Database) -> None:
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    assert p.mode is ProjectMode.task_queue
    assert (await repo.get_project(db, p.id)) is not None

    await repo.update_project_config(db, p.id, {"execution": {"target_branch": "vibes"}})
    reloaded = await repo.get_project(db, p.id)
    assert reloaded is not None
    assert reloaded.config["execution"]["target_branch"] == "vibes"

    await repo.set_project_mode(db, p.id, ProjectMode.autonomous_bug_fixer)
    reloaded = await repo.get_project(db, p.id)
    assert reloaded is not None
    assert reloaded.mode is ProjectMode.autonomous_bug_fixer


async def test_task_lifecycle_claim_and_recover(db: Database) -> None:
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    t1 = await repo.create_task(db, project_id=p.id, request="first", state=TaskState.approved)
    await repo.create_task(db, project_id=p.id, request="second", state=TaskState.approved)

    # claim picks the oldest approved task and flips it to in_progress
    claimed = await repo.claim_next_approved(db, p.id)
    assert claimed is not None
    assert claimed.id == t1.id
    assert claimed.state is TaskState.in_progress

    # crash recovery returns it to approved
    assert await repo.recover_in_progress(db, p.id) == 1
    again = await repo.get_task(db, t1.id)
    assert again is not None
    assert again.state is TaskState.approved

    # update fields round-trips JSON + scalar columns
    await repo.update_task_fields(db, t1.id, branch="conclave/x", level=2, plan={"approach": "a"})
    updated = await repo.get_task(db, t1.id)
    assert updated is not None
    assert updated.branch == "conclave/x"
    assert updated.level == 2
    assert updated.plan == {"approach": "a"}


async def test_claim_never_reclaims_terminal_tasks(db: Database) -> None:
    """Regression: claim must only ever pick 'approved' tasks — never done/failed.

    A precedence bug once made the parent-skip clause match terminal tasks when no
    failed/blocked tasks existed, re-running completed work.
    """
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    done = await repo.create_task(db, project_id=p.id, request="done", state=TaskState.approved)
    await repo.set_task_state(db, done.id, TaskState.done)

    # No approved tasks remain (and zero failed/blocked) → claim must return None.
    assert await repo.claim_next_approved(db, p.id) is None

    # A child whose parent failed must be skipped, but a healthy approved task is claimed.
    parent = await repo.create_task(db, project_id=p.id, request="parent", state=TaskState.approved)
    await repo.set_task_state(db, parent.id, TaskState.failed)
    child = await repo.create_task(
        db, project_id=p.id, request="child", state=TaskState.approved, parent_task_id=parent.id
    )
    ok = await repo.create_task(db, project_id=p.id, request="ok", state=TaskState.approved)
    claimed = await repo.claim_next_approved(db, p.id)
    assert claimed is not None
    assert claimed.id == ok.id  # not the failed-parent child
    assert child.id != claimed.id


async def test_events_stream_after_id(db: Database) -> None:
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    t = await repo.create_task(db, project_id=p.id, request="x")
    e1 = await repo.append_event(db, type="task.started", project_id=p.id, task_id=t.id)
    e2 = await repo.append_event(
        db, type="agent.output_chunk", project_id=p.id, task_id=t.id, agent="developer",
        payload={"line": "hello"},
    )
    assert e2.id > e1.id
    after = await repo.list_events(db, task_id=t.id, after_id=e1.id)
    assert [e.id for e in after] == [e2.id]
    assert after[0].payload == {"line": "hello"}


async def test_baselines_and_gc(db: Database) -> None:
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    await repo.save_baseline(db, p.id, "sha1", "output-1")
    assert (await repo.get_baseline(db, p.id, "sha1")).output == "output-1"  # type: ignore[union-attr]
    # overwrite same sha
    await repo.save_baseline(db, p.id, "sha1", "output-1b")
    assert (await repo.get_baseline(db, p.id, "sha1")).output == "output-1b"  # type: ignore[union-attr]


async def test_quarantine_expiry(db: Database) -> None:
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    await repo.add_quarantine(
        db, project_id=p.id, pattern="tests/a.test.js", reason="flaky", until="2999-01-01"
    )
    await repo.add_quarantine(
        db, project_id=p.id, pattern="tests/b.test.js", reason="expired", until="2000-01-01"
    )
    assert len(await repo.list_quarantine(db, p.id)) == 2
    active = await repo.active_quarantine(db, p.id, today="2026-06-13")
    assert [q.pattern for q in active] == ["tests/a.test.js"]


async def test_engine_profile_scope_resolution(db: Database) -> None:
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    await repo.upsert_engine_profile(db, name="system-default", arg_mode="inherit")
    await repo.upsert_engine_profile(
        db, name="deepseek", arg_mode="env", base_url="https://api.deepseek.com/anthropic",
        model="deepseek-v4-pro",
    )
    # project override of the same name shadows the global
    await repo.upsert_engine_profile(
        db, name="deepseek", project_id=p.id, arg_mode="env", model="deepseek-v4-flash"
    )
    resolved = await repo.get_engine_profile(db, "deepseek", project_id=p.id)
    assert resolved is not None
    assert resolved.model == "deepseek-v4-flash"
    glob = await repo.get_engine_profile(db, "deepseek")
    assert glob is not None
    assert glob.model == "deepseek-v4-pro"


async def test_secrets_are_store_only(db: Database) -> None:
    sid = await repo.set_secret(db, "deepseek_key", "sk-secret")
    assert await repo.get_secret_value(db, sid) == "sk-secret"
    # upsert by name returns the same id and updates the value
    sid2 = await repo.set_secret(db, "deepseek_key", "sk-new")
    assert sid2 == sid
    assert await repo.get_secret_value(db, sid) == "sk-new"
    assert await repo.list_secret_names(db) == ["deepseek_key"]


async def test_repo_knowledge_versioning(db: Database) -> None:
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    v1 = await repo.save_repo_knowledge(db, project_id=p.id, knowledge={"languages": ["py"]})
    v2 = await repo.save_repo_knowledge(db, project_id=p.id, knowledge={"languages": ["py", "ts"]})
    assert v1.version == 1
    assert v2.version == 2
    current = await repo.current_repo_knowledge(db, p.id)
    assert current is not None
    assert current.version == 2


async def test_usage_summary(db: Database) -> None:
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    await repo.add_usage(db, agent="developer", project_id=p.id, cost_usd=0.10, num_turns=3)
    await repo.add_usage(db, agent="tester", project_id=p.id, cost_usd=0.05, num_turns=1)
    summary = await repo.usage_summary(db, p.id)
    assert summary["calls"] == 2
    assert abs(summary["total_cost_usd"] - 0.15) < 1e-9


async def test_transaction_commits_all_on_success(db: Database) -> None:
    """Every statement inside a transaction() persists once the block exits cleanly."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    t1 = await repo.create_task(db, project_id=p.id, request="a", state=TaskState.inbox)
    t2 = await repo.create_task(db, project_id=p.id, request="b", state=TaskState.inbox)

    async with db.transaction() as conn:
        await conn.execute(
            "UPDATE tasks SET state = ? WHERE id = ?", (TaskState.approved.value, t1.id)
        )
        await conn.execute(
            "UPDATE tasks SET state = ? WHERE id = ?", (TaskState.approved.value, t2.id)
        )

    r1 = await repo.get_task(db, t1.id)
    r2 = await repo.get_task(db, t2.id)
    assert r1 is not None and r1.state is TaskState.approved
    assert r2 is not None and r2.state is TaskState.approved


async def test_transaction_rolls_back_all_on_error(db: Database) -> None:
    """Raising inside transaction() rolls back ALL of its statements, not just the last.

    This fails against any non-transactional path (e.g. autocommit with no BEGIN): there
    both UPDATEs would persist and the tasks would have advanced past ``inbox``.
    """
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    t1 = await repo.create_task(db, project_id=p.id, request="a", state=TaskState.inbox)
    t2 = await repo.create_task(db, project_id=p.id, request="b", state=TaskState.inbox)

    with pytest.raises(RuntimeError):
        async with db.transaction() as conn:
            await conn.execute(
                "UPDATE tasks SET state = ? WHERE id = ?", (TaskState.approved.value, t1.id)
            )
            await conn.execute(
                "UPDATE tasks SET state = ? WHERE id = ?", (TaskState.approved.value, t2.id)
            )
            raise RuntimeError("boom")

    # Both rows retain their pre-transaction state — the first UPDATE rolled back too.
    r1 = await repo.get_task(db, t1.id)
    r2 = await repo.get_task(db, t2.id)
    assert r1 is not None and r1.state is TaskState.inbox
    assert r2 is not None and r2.state is TaskState.inbox

    # The lock is released after a rolled-back transaction, so the connection is usable.
    await repo.set_task_state(db, t1.id, TaskState.approved)
    again = await repo.get_task(db, t1.id)
    assert again is not None and again.state is TaskState.approved


async def test_corrupt_task_row_loads_with_safe_defaults(db: Database) -> None:
    """A task row with an unknown enum and corrupt JSON must load — not 500 the list.

    A single bad row used to raise ValueError/JSONDecodeError out of ``Task.from_row``,
    taking down the whole task-list endpoint and stalling the worker's claim loop.
    """
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    await db.execute(
        "INSERT INTO tasks (id, project_id, title, request, state, plan_json, origin, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("bad", p.id, "t", "do x", "weird", "{not json", "alien", "2026-01-01", "2026-01-01"),
    )

    # Both the direct fetch and the list path (worker claim loop / list endpoint) survive.
    task = await repo.get_task(db, "bad")
    assert task is not None
    assert task.state is TaskState.inbox  # non-claimable, so a corrupt task is never run
    assert task.origin is TaskOrigin.operator
    assert task.plan is None
    assert any(t.id == "bad" for t in await repo.list_tasks(db, p.id))


async def test_corrupt_project_row_loads_with_safe_defaults(db: Database) -> None:
    """A project row with an unknown mode and corrupt config JSON survives list_projects."""
    await db.execute(
        "INSERT INTO projects (id, name, path, default_branch, mode, config_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("bad", "bad", "/tmp/bad", "main", "wild", "{bad json", "2026-01-01"),
    )
    bad = next(p for p in await repo.list_projects(db) if p.id == "bad")
    assert bad.mode is ProjectMode.task_queue
    assert bad.config == {}


async def test_corrupt_event_row_loads_with_empty_payload(db: Database) -> None:
    """An event row with corrupt payload JSON loads with an empty payload — no raise."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    await db.execute(
        "INSERT INTO events (project_id, type, payload_json, ts) VALUES (?, ?, ?, ?)",
        (p.id, "agent.output_chunk", "{not json", "2026-01-01"),
    )
    events = await repo.list_events(db, project_id=p.id)
    assert len(events) == 1
    assert events[0].payload == {}


async def test_corrupt_planning_rows_load_with_safe_defaults(db: Database) -> None:
    """Planning session/node rows with unknown status strings fall back to safe defaults."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    await db.execute(
        "INSERT INTO planning_sessions (id, project_id, title, prompt, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("sess", p.id, "t", "plan it", "bogus", "2026-01-01"),
    )
    await db.execute(
        "INSERT INTO planning_task_nodes (id, session_id, title, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("node", "sess", "a node", "nonsense", "2026-01-01", "2026-01-01"),
    )
    sess = await repo.get_planning_session(db, "sess")
    assert sess is not None
    assert sess.status is PlanningSessionStatus.active
    nodes = await repo.list_planning_task_nodes(db, "sess")
    assert len(nodes) == 1
    assert nodes[0].status is PlanningNodeStatus.proposed
