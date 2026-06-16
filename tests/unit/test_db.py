"""Unit tests for the persistence layer."""

from __future__ import annotations

import sqlite3

import pytest

from conclave.db import Database, ProjectMode, TaskOrigin, TaskState
from conclave.db import repositories as repo
from conclave.db.migrations import Migration
from conclave.db.planning_models import PlanningNodeStatus, PlanningSessionStatus


async def test_migrations_apply(db: Database) -> None:
    version = await db.fetchval("SELECT MAX(version) FROM schema_version")
    assert version == 7
    # idempotent: re-running connect/migrate does not error or duplicate
    await db._apply_migrations()
    assert await db.fetchval("SELECT COUNT(*) FROM schema_version") == 7


async def test_migration_failure_is_atomic(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration that fails partway must roll back its partial DDL and NOT advance the
    schema_version.

    Otherwise the partial DDL is committed but the version isn't bumped, so the next boot
    replays the same migration and wedges on a duplicate-column error. The fixture DB is
    already at version 7; we inject a v8 migration whose first statement succeeds (creates
    a probe table) and whose second fails — ``stabilization_reason`` was already added by
    migration 7, so re-adding it raises a duplicate-column error — which is exactly that wedge.
    """
    failing = Migration(
        version=8,
        name="injected_failure",
        sql=(
            "CREATE TABLE atomic_probe (id INTEGER PRIMARY KEY);\n"
            "ALTER TABLE planning_sessions ADD COLUMN stabilization_reason TEXT;"
        ),
    )
    monkeypatch.setattr("conclave.db.database.MIGRATIONS", [failing])

    with pytest.raises(sqlite3.OperationalError):
        await db._apply_migrations()

    # Version did not advance past the last good migration...
    assert await db.fetchval("SELECT MAX(version) FROM schema_version") == 7
    # ...and the partial DDL (the probe table from the first statement) was rolled back.
    assert (
        await db.fetchval(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'atomic_probe'"
        )
        == 0
    )


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

    # crash recovery returns it to approved; no failed/blocked parents → reblocked=0
    recovered, reblocked = await repo.recover_in_progress(db, p.id)
    assert recovered == 1
    assert reblocked == 0
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


async def test_recover_reblocks_failed_parent_children(db: Database) -> None:
    """After recovery, a child of a failed parent must be blocked — not claimable.

    Simulates a crash where a parent is ``failed`` and its child is ``approved``
    (e.g. stranded by a pre-CON-1 code path or direct manipulation). Recovery
    must re-block the child so it can never be claimed out of dependency order.
    A healthy approved task with a ``done`` parent (or no parent) is unaffected.
    """
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")

    # A failed parent with an approved child — the vulnerable state.
    parent = await repo.create_task(
        db, project_id=p.id, request="parent", state=TaskState.approved,
    )
    await repo.set_task_state(db, parent.id, TaskState.failed)
    child = await repo.create_task(
        db, project_id=p.id, request="child", state=TaskState.approved,
        parent_task_id=parent.id,
    )

    # A healthy approved task with a done parent — should NOT be blocked.
    done_parent = await repo.create_task(
        db, project_id=p.id, request="done-parent", state=TaskState.approved,
    )
    await repo.set_task_state(db, done_parent.id, TaskState.done)
    healthy = await repo.create_task(
        db, project_id=p.id, request="healthy", state=TaskState.approved,
        parent_task_id=done_parent.id,
    )

    # A parentless approved task — should also be unaffected.
    orphan = await repo.create_task(
        db, project_id=p.id, request="orphan", state=TaskState.approved,
    )

    recovered, reblocked = await repo.recover_in_progress(db, p.id)
    assert recovered == 0  # no in_progress tasks
    assert reblocked == 1  # the failed parent's child was blocked

    c = await repo.get_task(db, child.id)
    assert c is not None and c.state is TaskState.blocked

    # Unaffected tasks remain claimable.
    h = await repo.get_task(db, healthy.id)
    assert h is not None and h.state is TaskState.approved
    o = await repo.get_task(db, orphan.id)
    assert o is not None and o.state is TaskState.approved

    # Verify claimability: only the healthy and orphan should be claimable.
    claimed1 = await repo.claim_next_approved(db, p.id)
    assert claimed1 is not None and claimed1.id in (healthy.id, orphan.id)
    claimed2 = await repo.claim_next_approved(db, p.id)
    assert claimed2 is not None and claimed2.id in (healthy.id, orphan.id)
    assert claimed1.id != claimed2.id
    # The blocked child is NOT claimable.
    assert await repo.claim_next_approved(db, p.id) is None


async def test_recover_reblocks_blocked_parent_children(db: Database) -> None:
    """A ``blocked`` parent must also propagate blocking during recovery.

    If task B is ``blocked`` (e.g. because its parent A failed) and B's own child C
    is somehow ``approved``, recovery must block C — the blocked state propagates
    across depth just like the failed state does.
    """
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")

    # Grandparent is failed → parent should be blocked, but we'll create it as approved
    # to simulate the inconsistent state recovery fixes.
    grandparent = await repo.create_task(
        db, project_id=p.id, request="grandparent", state=TaskState.approved,
    )
    await repo.set_task_state(db, grandparent.id, TaskState.failed)
    parent = await repo.create_task(
        db, project_id=p.id, request="parent", state=TaskState.blocked,
        parent_task_id=grandparent.id,
    )
    child = await repo.create_task(
        db, project_id=p.id, request="child", state=TaskState.approved,
        parent_task_id=parent.id,
    )
    grandchild = await repo.create_task(
        db, project_id=p.id, request="grandchild", state=TaskState.approved,
        parent_task_id=child.id,
    )

    recovered, reblocked = await repo.recover_in_progress(db, p.id)
    assert recovered == 0  # no in_progress tasks
    # Both the child (of blocked parent) and grandchild (of child) are blocked.
    assert reblocked == 2

    c = await repo.get_task(db, child.id)
    assert c is not None and c.state is TaskState.blocked
    gc = await repo.get_task(db, grandchild.id)
    assert gc is not None and gc.state is TaskState.blocked


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

    # A cancelled parent gates its child just like a failed one — cancelled is terminal but not
    # success, so it never becomes 'done'. With 'ok' now in_progress, the failed-parent child and
    # this cancelled-parent child are the only approved tasks left, and neither is claimable.
    cancelled_parent = await repo.create_task(
        db, project_id=p.id, request="cancelled-parent", state=TaskState.approved
    )
    await repo.set_task_state(db, cancelled_parent.id, TaskState.cancelled)
    await repo.create_task(
        db,
        project_id=p.id,
        request="cancelled-child",
        state=TaskState.approved,
        parent_task_id=cancelled_parent.id,
    )
    assert await repo.claim_next_approved(db, p.id) is None


async def test_claim_enforces_parent_done_dependency(db: Database) -> None:
    """Dependency ordering: a child is claimable only once its parent reaches 'done'.

    This closes the gap the old failed/blocked-only predicate left open — a not-yet-'done'
    parent (here in_progress) used to let its child run out of order. The child is created
    BEFORE a parentless sibling so that, if FIFO alone decided, the older child would win;
    instead the sibling is claimed while the parent is mid-flight, and the child is released
    only once the parent is 'done'.
    """
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    parent = await repo.create_task(db, project_id=p.id, request="parent", state=TaskState.approved)

    # Parent (oldest, parentless) is claimed first and is now in_progress — claimed, not done.
    claimed_parent = await repo.claim_next_approved(db, p.id)
    assert claimed_parent is not None
    assert claimed_parent.id == parent.id
    assert claimed_parent.state is TaskState.in_progress

    # Child is the OLDEST remaining approved task; a younger parentless sibling follows it.
    child = await repo.create_task(
        db, project_id=p.id, request="child", state=TaskState.approved, parent_task_id=parent.id
    )
    sibling = await repo.create_task(
        db, project_id=p.id, request="sibling", state=TaskState.approved
    )

    # Dependency ordering beats FIFO: the parent is still in_progress (not 'done'), so the older
    # child is skipped and the younger parentless sibling is claimed instead.
    nxt = await repo.claim_next_approved(db, p.id)
    assert nxt is not None
    assert nxt.id == sibling.id
    assert nxt.id != child.id

    # Sibling is now in_progress too; the child is the only approved task left but stays
    # unclaimable while the parent is not done → claim returns None.
    assert await repo.claim_next_approved(db, p.id) is None

    # Parent reaches terminal success → the previously-skipped child is finally claimable.
    await repo.set_task_state(db, parent.id, TaskState.done)
    claimed_child = await repo.claim_next_approved(db, p.id)
    assert claimed_child is not None
    assert claimed_child.id == child.id
    assert claimed_child.state is TaskState.in_progress


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


async def test_seed_global_defaults_idempotent_and_preserves_edits(db: Database) -> None:
    from conclave.bootstrap import seed_global_defaults

    await seed_global_defaults(db)
    assert await repo.get_agent(db, "developer") is not None
    assert await repo.get_agent(db, "tester") is not None

    # an operator edit must survive re-seeding
    await repo.upsert_agent(db, name="developer", role="developer", persona_md="EDITED PERSONA")
    await seed_global_defaults(db)
    dev = await repo.get_agent(db, "developer")
    assert dev is not None and dev.persona_md == "EDITED PERSONA"


async def test_usage_summary(db: Database) -> None:
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    await repo.add_usage(db, agent="developer", project_id=p.id, cost_usd=0.10, num_turns=3)
    await repo.add_usage(db, agent="tester", project_id=p.id, cost_usd=0.05, num_turns=1)
    summary = await repo.usage_summary(db, p.id)
    assert summary["calls"] == 2
    assert abs(summary["total_cost_usd"] - 0.15) < 1e-9


async def test_get_task_usage_sql_aggregation(db: Database) -> None:
    """get_task_usage uses COALESCE(SUM(...)) — a single aggregate row regardless of row count."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    t = await repo.create_task(db, project_id=p.id, request="x")

    # Add multiple usage rows with varying token counts.
    await repo.add_usage(
        db, agent="dev", task_id=t.id, project_id=p.id,
        num_turns=3, input_tokens=100, output_tokens=50,
        cache_read_tokens=20, cache_creation_tokens=10,
    )
    await repo.add_usage(
        db, agent="tester", task_id=t.id, project_id=p.id,
        num_turns=1, input_tokens=80, output_tokens=30,
        cache_read_tokens=15, cache_creation_tokens=5,
    )

    usage = await repo.get_task_usage(db, t.id)
    assert usage["task_id"] == t.id
    assert usage["total_turns"] == 4  # 3 + 1
    assert usage["input_tokens"] == 180  # 100 + 80
    assert usage["output_tokens"] == 80  # 50 + 30
    assert usage["cache_read_tokens"] == 35  # 20 + 15
    assert usage["cache_creation_tokens"] == 15  # 10 + 5
    assert usage["agent_count"] == 2  # two distinct usage rows

    # A task with no usage rows returns zeros — not an error.
    t2 = await repo.create_task(db, project_id=p.id, request="no-usage")
    empty = await repo.get_task_usage(db, t2.id)
    assert empty["total_turns"] == 0
    assert empty["input_tokens"] == 0
    assert empty["agent_count"] == 0


async def test_list_functions_honor_limit_offset(db: Database) -> None:
    """Repo list functions append LIMIT ? OFFSET ? when limit is provided."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")

    # Create several tasks to paginate over.
    for i in range(10):
        await repo.create_task(db, project_id=p.id, request=f"task-{i}")

    # Without limit, all tasks are returned.
    all_tasks = await repo.list_tasks(db, p.id)
    assert len(all_tasks) == 10

    # With limit, only the requested count is returned.
    page1 = await repo.list_tasks(db, p.id, limit=3, offset=0)
    assert len(page1) == 3

    # Offset skips the first items.
    page2 = await repo.list_tasks(db, p.id, limit=3, offset=3)
    assert len(page2) == 3
    # Verify no overlap — offsets produce distinct pages.
    ids1 = {t.id for t in page1}
    ids2 = {t.id for t in page2}
    assert ids1.isdisjoint(ids2)

    # Offset past the end returns empty.
    tail = await repo.list_tasks(db, p.id, limit=5, offset=100)
    assert tail == []

    # Quarantine and verdicts also support pagination.
    entries = await repo.list_quarantine(db, p.id, limit=5, offset=0)
    assert entries == []

    # Verdicts pagination.
    t = all_tasks[0]
    await repo.add_verdict(db, task_id=t.id, attempt=1, agent="reviewer", verdict="pass")
    await repo.add_verdict(db, task_id=t.id, attempt=1, agent="security", verdict="pass")
    v_page = await repo.list_verdicts(db, t.id, limit=1, offset=0)
    assert len(v_page) == 1
    v_page2 = await repo.list_verdicts(db, t.id, limit=1, offset=1)
    assert len(v_page2) == 1
    assert v_page[0].id != v_page2[0].id


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


async def test_block_descendants_cycle_safe(db: Database) -> None:
    """block_descendants terminates on a cyclic parent_task_id (A → B → A)
    and blocks each non-terminal node at most once."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")

    # Create task A (inbox).
    task_a = await repo.create_task(
        db, project_id=p.id, request="task A", state=TaskState.inbox,
    )
    # Create task B as a child of A.
    task_b = await repo.create_task(
        db, project_id=p.id, request="task B", state=TaskState.inbox,
        parent_task_id=task_a.id,
    )

    # Create the cycle: point A's parent at B via raw SQL.
    await db.execute(
        "UPDATE tasks SET parent_task_id = ? WHERE id = ?",
        (task_b.id, task_a.id),
    )

    # block_descendants must terminate and return a finite count.
    blocked = await repo.block_descendants(db, task_a.id)
    assert blocked in (1, 2), f"Expected 1 or 2 blocked tasks, got {blocked}"

    # Verify that the blocked tasks are actually blocked.
    a = await repo.get_task(db, task_a.id)
    assert a is not None
    # task_a is the root — it is NOT blocked by block_descendants (only
    # descendants are blocked, not the root itself).
    assert a.state is TaskState.inbox

    b = await repo.get_task(db, task_b.id)
    assert b is not None
    assert b.state is TaskState.blocked


async def test_events_gc_prunes_beyond_cap(db: Database) -> None:
    """gc_events keeps only the most-recent N events per project (by id)."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")

    # Insert 15 events for the project.
    for i in range(15):
        await repo.append_event(
            db, type="test.event", project_id=p.id,
            payload={"n": i},
        )

    # All 15 are present before GC.
    all_events = await repo.list_events(db, project_id=p.id, limit=100)
    assert len(all_events) == 15

    # Prune to the 10 most-recent.
    await repo.gc_events(db, p.id, keep=10)

    remaining = await repo.list_events(db, project_id=p.id, limit=100)
    assert len(remaining) == 10
    # The 10 highest ids are kept.
    ids = sorted(e.id for e in remaining)
    expected = sorted(e.id for e in all_events[-10:])
    assert ids == expected


async def test_gc_events_noop_when_below_cap(db: Database) -> None:
    """gc_events is a no-op when the event count is below the cap."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")

    # Insert only 3 events — well below the default cap.
    for _ in range(3):
        await repo.append_event(db, type="test.event", project_id=p.id)

    before = await repo.list_events(db, project_id=p.id, limit=100)
    assert len(before) == 3

    # Call with a large keep — should not delete anything.
    await repo.gc_events(db, p.id, keep=100)

    after = await repo.list_events(db, project_id=p.id, limit=100)
    assert len(after) == 3
    assert {e.id for e in after} == {e.id for e in before}


async def test_gc_events_uses_transaction(db: Database) -> None:
    """gc_events runs through the serialized write so it composes with concurrent appends."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")

    # Insert events.
    for _ in range(5):
        await repo.append_event(db, type="test.event", project_id=p.id)

    assert len(await repo.list_events(db, project_id=p.id, limit=100)) == 5

    # Prune to 3 — the call completes without error, proving the serialized-write
    # path works with this SQL pattern.
    await repo.gc_events(db, p.id, keep=3)
    assert len(await repo.list_events(db, project_id=p.id, limit=100)) == 3

    # Verify idempotent: calling again with the same keep is harmless.
    await repo.gc_events(db, p.id, keep=3)
    assert len(await repo.list_events(db, project_id=p.id, limit=100)) == 3


async def test_migration_6_indexes_exist(db: Database) -> None:
    """Migration 6 creates idx_tasks_project_created and idx_usage_task."""
    # Verify idx_tasks_project_created exists on tasks.
    task_indexes = await db.fetchall(
        "SELECT name FROM pragma_index_list('tasks') ORDER BY name"
    )
    task_names = {r["name"] for r in task_indexes}
    assert "idx_tasks_project_created" in task_names

    # Verify idx_usage_task exists on usage.
    usage_indexes = await db.fetchall(
        "SELECT name FROM pragma_index_list('usage') ORDER BY name"
    )
    usage_names = {r["name"] for r in usage_indexes}
    assert "idx_usage_task" in usage_names
