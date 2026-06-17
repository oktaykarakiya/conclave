"""Unit tests for the persistence layer."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from conclave.db import (
    BUG_STATUS_TRANSITIONS,
    BugCandidate,
    BugStatus,
    CoverageRegion,
    Database,
    IllegalBugTransition,
    ProjectMode,
    TaskOrigin,
    TaskState,
)
from conclave.db import repositories as repo
from conclave.db.migrations import MIGRATIONS, Migration
from conclave.db.planning_models import PlanningNodeStatus, PlanningSessionStatus

# The 7-state machine the Bug-Fixer controller drives over bug_candidates.status. The column
# is permissive TEXT (validation lives in the enum/Pydantic layer, like tasks.state), so this
# tuple documents the canonical vocabulary and the migration test proves each value persists.
BUG_CANDIDATE_STATUSES = (
    "discovered",
    "reproduced",
    "fixing",
    "fixed",
    "dismissed_false_positive",
    "declined_needs_human",
    "deferred",
)

# An INDEPENDENT restatement of the pinned 7-state machine — deliberately not derived from
# BUG_STATUS_TRANSITIONS, so an accidental edit to the production table is caught (test and code
# must agree) rather than silently tracked. Each pair is one legal (from → to) edge.
_LEGAL_BUG_EDGES = (
    (BugStatus.discovered, BugStatus.reproduced),
    (BugStatus.discovered, BugStatus.dismissed_false_positive),
    (BugStatus.discovered, BugStatus.declined_needs_human),
    (BugStatus.discovered, BugStatus.deferred),
    (BugStatus.reproduced, BugStatus.fixing),
    (BugStatus.reproduced, BugStatus.dismissed_false_positive),
    (BugStatus.reproduced, BugStatus.declined_needs_human),
    (BugStatus.reproduced, BugStatus.deferred),
    (BugStatus.fixing, BugStatus.fixed),
    (BugStatus.fixing, BugStatus.reproduced),
    (BugStatus.deferred, BugStatus.discovered),
    (BugStatus.deferred, BugStatus.reproduced),
)

# A representative sample of edges that must be REJECTED: skipping reproduction, leaving a
# terminal sink, and re-entering fixing without going through reproduced.
_ILLEGAL_BUG_EDGES = (
    (BugStatus.discovered, BugStatus.fixing),  # cannot fix what isn't reproduced
    (BugStatus.discovered, BugStatus.fixed),  # cannot skip straight to fixed
    (BugStatus.reproduced, BugStatus.fixed),  # fixed only via fixing
    (BugStatus.fixed, BugStatus.reproduced),  # terminal: no way out
    (BugStatus.dismissed_false_positive, BugStatus.discovered),  # terminal
    (BugStatus.declined_needs_human, BugStatus.fixing),  # terminal handoff
    (BugStatus.fixing, BugStatus.deferred),  # fixing only lands fixed or retries
    (BugStatus.discovered, BugStatus.discovered),  # self-loop is not an advance
)


async def _seed_candidate(
    db: Database, project_id: str, *, fingerprint: str, status: BugStatus = BugStatus.discovered
) -> str:
    """Insert a candidate already parked at ``status`` (raw, bypassing the transition guard).

    Lets the transition tests drop a candidate directly into any source state without having to
    navigate the whole machine to reach it. Returns the new row id.
    """
    cid = f"seed-{fingerprint}"
    await db.execute(
        "INSERT INTO bug_candidates (id, project_id, fingerprint, claim, status, "
        "discovered_at, last_examined_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (cid, project_id, fingerprint, "seeded claim", status.value, "2020-01-01", "2020-01-01"),
    )
    return cid


async def test_migrations_apply(db: Database) -> None:
    version = await db.fetchval("SELECT MAX(version) FROM schema_version")
    assert version == 8
    # idempotent: re-running connect/migrate does not error or duplicate
    await db._apply_migrations()
    assert await db.fetchval("SELECT COUNT(*) FROM schema_version") == 8


async def test_migration_failure_is_atomic(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A migration that fails partway must roll back its partial DDL and NOT advance the
    schema_version.

    Otherwise the partial DDL is committed but the version isn't bumped, so the next boot
    replays the same migration and wedges on a duplicate-column error. The fixture DB is
    already at version 8; we inject a v9 migration whose first statement succeeds (creates
    a probe table) and whose second fails — ``stabilization_reason`` was already added by
    migration 7, so re-adding it raises a duplicate-column error — which is exactly that wedge.
    """
    failing = Migration(
        version=9,
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
    assert await db.fetchval("SELECT MAX(version) FROM schema_version") == 8
    # ...and the partial DDL (the probe table from the first statement) was rolled back.
    assert (
        await db.fetchval(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'atomic_probe'"
        )
        == 0
    )


async def test_migration_8_bug_candidate_ledger_schema(db: Database) -> None:
    """Migration 8 reshapes bug_candidates into the 7-state ledger and extends coverage.

    A fresh DB is at version 8. The old two-field status model (status DEFAULT 'candidate'
    plus a separate ``reproduced`` boolean) is gone, replaced by a single permissive
    ``status`` TEXT defaulting to 'discovered', alongside the reproduction/consensus columns;
    coverage gains priority + examined_count.
    """
    assert await db.fetchval("SELECT MAX(version) FROM schema_version") == 8

    cols = {r["name"]: r for r in await db.fetchall("PRAGMA table_info(bug_candidates)")}
    # New columns are present...
    for added in (
        "region", "repro_test_path", "repro_test_body", "repro_test_hash",
        "attempts", "decline_reason", "consensus_json",
    ):
        assert added in cols, f"bug_candidates is missing {added}"
    # ...the retired boolean is gone, and the reshaped columns carry the documented defaults.
    assert "reproduced" not in cols
    assert cols["status"]["notnull"] == 1
    assert cols["status"]["dflt_value"] == "'discovered'"
    assert cols["attempts"]["notnull"] == 1
    assert cols["attempts"]["dflt_value"] == "0"
    assert cols["consensus_json"]["notnull"] == 1
    assert cols["consensus_json"]["dflt_value"] == "'{}'"
    # Preserved columns survive the rebuild.
    for kept in (
        "fingerprint", "file", "symbol", "claim", "severity", "task_id",
        "notes", "discovered_at", "last_examined_at", "fixed_at",
    ):
        assert kept in cols

    # The fingerprint unique index is recreated with the table.
    indexes = {r["name"] for r in await db.fetchall("PRAGMA index_list('bug_candidates')")}
    assert "idx_bug_fingerprint" in indexes

    # coverage gains its scheduler columns with the documented NOT NULL defaults.
    cov = {r["name"]: r for r in await db.fetchall("PRAGMA table_info(coverage)")}
    assert cov["priority"]["notnull"] == 1 and cov["priority"]["dflt_value"] == "0"
    assert cov["examined_count"]["notnull"] == 1 and cov["examined_count"]["dflt_value"] == "0"

    # Each of the 7 status strings inserts cleanly — the column is permissive TEXT (no CHECK),
    # so the ledger can hold any state the controller advances a candidate through.
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    for i, status in enumerate(BUG_CANDIDATE_STATUSES):
        await db.execute(
            "INSERT INTO bug_candidates (id, project_id, fingerprint, claim, status, "
            "discovered_at, last_examined_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"bc{i}", p.id, f"fp{i}", "off-by-one in slice", status, "2026-01-01", "2026-01-01"),
        )
    persisted = {r["status"] for r in await db.fetchall("SELECT status FROM bug_candidates")}
    assert persisted == set(BUG_CANDIDATE_STATUSES)

    # Omitting status applies the 'discovered' default.
    await db.execute(
        "INSERT INTO bug_candidates (id, project_id, fingerprint, claim, "
        "discovered_at, last_examined_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("bc-default", p.id, "fp-default", "claim", "2026-01-01", "2026-01-01"),
    )
    assert (
        await db.fetchval("SELECT status FROM bug_candidates WHERE id = 'bc-default'")
        == "discovered"
    )


async def test_migration_8_recreates_seeded_bug_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The v8 DROP/recreate runs cleanly against a DB where the v1 scaffold physically exists.

    A fresh DB never materializes the old bug_candidates shape, so we seed a DB at version 1
    only — where the retired ``status DEFAULT 'candidate'`` + ``reproduced`` columns are real —
    then migrate forward and confirm it reaches version 8 with the new schema. This exercises
    the DROP against an actual prior table rather than a no-op on a missing one.
    """
    # Seed at version 1: expose only the initial-schema migration to the runner.
    monkeypatch.setattr("conclave.db.database.MIGRATIONS", [MIGRATIONS[0]])
    database = Database(tmp_path / "seeded.db")
    await database.connect()
    try:
        assert await database.fetchval("SELECT MAX(version) FROM schema_version") == 1
        v1_cols = {
            r["name"] for r in await database.fetchall("PRAGMA table_info(bug_candidates)")
        }
        assert "reproduced" in v1_cols  # the old scaffold is genuinely present

        # Expose the full migration set and roll the seeded DB forward to head.
        monkeypatch.setattr("conclave.db.database.MIGRATIONS", MIGRATIONS)
        await database._apply_migrations()

        assert await database.fetchval("SELECT MAX(version) FROM schema_version") == 8
        v8_cols = {
            r["name"] for r in await database.fetchall("PRAGMA table_info(bug_candidates)")
        }
        assert "reproduced" not in v8_cols  # rebuilt without the retired boolean
        assert {"region", "repro_test_path", "attempts", "consensus_json"} <= v8_cols
        cov_cols = {r["name"] for r in await database.fetchall("PRAGMA table_info(coverage)")}
        assert {"priority", "examined_count"} <= cov_cols
    finally:
        await database.close()


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


async def test_bug_candidate_from_row_happy_path(db: Database) -> None:
    """A fully-populated bug_candidates row round-trips through BugCandidate.from_row.

    The enum status parses, consensus_json decodes to a dict, and every reproduction/handoff
    column maps onto its model field.
    """
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    await db.execute(
        "INSERT INTO bug_candidates ("
        "id, project_id, fingerprint, file, symbol, region, claim, severity, status, "
        "repro_test_path, repro_test_body, repro_test_hash, attempts, decline_reason, "
        "consensus_json, task_id, notes, discovered_at, last_examined_at, fixed_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "bc-happy", p.id, "fp-1", "src/x.py", "foo", "src/", "off-by-one in slice", "high",
            "reproduced", "tests/test_x.py", "def test_x(): assert False", "deadbeef", 2, None,
            '{"reviewers": 3, "agree": true}', "t-1", "a note", "2026-01-01", "2026-01-02", None,
        ),
    )

    row = await db.fetchone("SELECT * FROM bug_candidates WHERE id = 'bc-happy'")
    bc = BugCandidate.from_row(row)

    assert bc.id == "bc-happy"
    assert bc.project_id == p.id
    assert bc.fingerprint == "fp-1"
    assert bc.file == "src/x.py"
    assert bc.symbol == "foo"
    assert bc.region == "src/"
    assert bc.claim == "off-by-one in slice"
    assert bc.severity == "high"
    assert bc.status is BugStatus.reproduced
    assert bc.repro_test_path == "tests/test_x.py"
    assert bc.repro_test_body == "def test_x(): assert False"
    assert bc.repro_test_hash == "deadbeef"
    assert bc.attempts == 2
    assert bc.decline_reason is None
    assert bc.consensus == {"reviewers": 3, "agree": True}
    assert bc.task_id == "t-1"
    assert bc.notes == "a note"
    assert bc.discovered_at == "2026-01-01"
    assert bc.last_examined_at == "2026-01-02"
    assert bc.fixed_at is None


async def test_bug_candidate_from_row_applies_column_defaults(db: Database) -> None:
    """A minimal insert leans on the schema defaults: status 'discovered', attempts 0,
    consensus '{}'. The model surfaces them as the natural initial state — distinct from the
    corrupt-row sink exercised below."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    await db.execute(
        "INSERT INTO bug_candidates (id, project_id, fingerprint, claim, "
        "discovered_at, last_examined_at) VALUES (?, ?, ?, ?, ?, ?)",
        ("bc-min", p.id, "fp-min", "claim", "2026-01-01", "2026-01-01"),
    )
    bc = BugCandidate.from_row(
        await db.fetchone("SELECT * FROM bug_candidates WHERE id = 'bc-min'")
    )
    assert bc.status is BugStatus.discovered
    assert bc.attempts == 0
    assert bc.consensus == {}
    assert bc.region is None
    assert bc.repro_test_path is None


async def test_corrupt_bug_candidate_row_loads_with_safe_defaults(
    db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """A bug_candidates row with an unknown status and corrupt consensus JSON must load —
    not raise out of from_row and stall the controller's ledger scan.

    The unknown status degrades to ``declined_needs_human`` (non-actionable, human-routed)
    so a corrupt candidate is never auto-picked for an auto-fix; corrupt consensus_json
    decodes to ``{}``. Both fallbacks log a warning rather than raising.
    """
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    await db.execute(
        "INSERT INTO bug_candidates (id, project_id, fingerprint, claim, status, "
        "consensus_json, discovered_at, last_examined_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("bc-bad", p.id, "fp-bad", "claim", "totally-bogus", "{not json", "2026-01-01",
         "2026-01-01"),
    )

    with caplog.at_level(logging.WARNING, logger="conclave.db.models"):
        bc = BugCandidate.from_row(
            await db.fetchone("SELECT * FROM bug_candidates WHERE id = 'bc-bad'")
        )

    # Falls back to the safe, non-actionable sink — never an auto-pickable state.
    assert bc.status is BugStatus.declined_needs_human
    assert bc.status not in {BugStatus.discovered, BugStatus.reproduced}
    assert bc.consensus == {}
    # Both corruptions were logged (no raise), so the bad row is observable, not silent.
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "unknown BugStatus" in messages
    assert "corrupt JSON column" in messages


async def test_coverage_region_from_row(db: Database) -> None:
    """A coverage row round-trips through CoverageRegion.from_row, including the scheduler
    columns added by migration 8; omitting them applies the NOT NULL defaults (priority 0,
    examined_count 0)."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    await db.execute(
        "INSERT INTO coverage (id, project_id, region, last_examined_at, priority, "
        "examined_count) VALUES (?, ?, ?, ?, ?, ?)",
        ("cov-1", p.id, "src/conclave/db", "2026-01-03", 5, 7),
    )
    cov = CoverageRegion.from_row(
        await db.fetchone("SELECT * FROM coverage WHERE id = 'cov-1'")
    )
    assert cov.id == "cov-1"
    assert cov.project_id == p.id
    assert cov.region == "src/conclave/db"
    assert cov.last_examined_at == "2026-01-03"
    assert cov.priority == 5
    assert cov.examined_count == 7

    # A region that has never been examined leans on the defaults.
    await db.execute(
        "INSERT INTO coverage (id, project_id, region) VALUES (?, ?, ?)",
        ("cov-fresh", p.id, "src/conclave/web"),
    )
    fresh = CoverageRegion.from_row(
        await db.fetchone("SELECT * FROM coverage WHERE id = 'cov-fresh'")
    )
    assert fresh.last_examined_at is None
    assert fresh.priority == 0
    assert fresh.examined_count == 0


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


# --- Bug-Fixer ledger: create / dedupe / list -------------------------------


async def test_create_bug_candidate_dedupes_on_fingerprint(db: Database) -> None:
    """A second create with the same (project_id, fingerprint) is a no-op that returns row 1.

    Dedupe must preserve the original row wholesale — same id, original claim/status — and never
    spawn a duplicate; a genuinely new fingerprint still yields a distinct row.
    """
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")

    first = await repo.create_bug_candidate(
        db, project_id=p.id, fingerprint="fp-1", claim="off-by-one in slice", severity="high"
    )
    assert first.status is BugStatus.discovered

    # Re-report the SAME fingerprint with different payload → no-op returning the original row.
    second = await repo.create_bug_candidate(
        db, project_id=p.id, fingerprint="fp-1", claim="totally different claim", severity="low"
    )
    assert second.id == first.id
    assert second.claim == "off-by-one in slice"  # row 1 preserved, the re-report ignored
    assert second.severity == "high"

    # Exactly one row exists for that fingerprint.
    assert len(await repo.list_bug_candidates(db, p.id)) == 1

    # A new fingerprint is a genuinely distinct candidate.
    other = await repo.create_bug_candidate(
        db, project_id=p.id, fingerprint="fp-2", claim="another bug"
    )
    assert other.id != first.id
    assert len(await repo.list_bug_candidates(db, p.id)) == 2


async def test_get_and_list_bug_candidates_filter_by_status(db: Database) -> None:
    """get returns one row (or None); list filters by project and, optionally, status."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    assert await repo.get_bug_candidate(db, "nope") is None

    a = await repo.create_bug_candidate(db, project_id=p.id, fingerprint="fp-a", claim="a")
    b = await repo.create_bug_candidate(db, project_id=p.id, fingerprint="fp-b", claim="b")

    got = await repo.get_bug_candidate(db, a.id)
    assert got is not None and got.id == a.id

    # Advance one candidate so the status filter has something to discriminate.
    await repo.transition_bug_status(db, b.id, BugStatus.reproduced)

    all_ids = {c.id for c in await repo.list_bug_candidates(db, p.id)}
    assert all_ids == {a.id, b.id}

    discovered = await repo.list_bug_candidates(db, p.id, status=BugStatus.discovered)
    assert [c.id for c in discovered] == [a.id]
    reproduced = await repo.list_bug_candidates(db, p.id, status=BugStatus.reproduced)
    assert [c.id for c in reproduced] == [b.id]
    assert await repo.list_bug_candidates(db, p.id, status=BugStatus.fixed) == []


async def test_set_repro_artifacts(db: Database) -> None:
    """set_repro_artifacts attaches the repro test columns and refreshes last_examined_at."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    cid = await _seed_candidate(db, p.id, fingerprint="fp-1")  # last_examined_at = 2020-01-01

    await repo.set_repro_artifacts(
        db, cid, path="tests/test_x.py", body="def test_x(): assert False", hash="deadbeef"
    )

    bc = await repo.get_bug_candidate(db, cid)
    assert bc is not None
    assert bc.repro_test_path == "tests/test_x.py"
    assert bc.repro_test_body == "def test_x(): assert False"
    assert bc.repro_test_hash == "deadbeef"
    assert bc.status is BugStatus.discovered  # status is untouched here
    assert bc.last_examined_at != "2020-01-01"  # refreshed to now


# --- Bug-Fixer ledger: the guarded status machine ---------------------------


def test_bug_status_transition_table_matches_independent_spec() -> None:
    """The production table equals the test's independent restatement — neither drifts silently.

    Folds _LEGAL_BUG_EDGES back into a {state: {targets}} map (terminals → empty sets) and
    asserts equality with BUG_STATUS_TRANSITIONS, pinning the exact shape of the machine.
    """
    expected: dict[BugStatus, set[BugStatus]] = {state: set() for state in BugStatus}
    for src, dst in _LEGAL_BUG_EDGES:
        expected[src].add(dst)
    actual = {state: set(targets) for state, targets in BUG_STATUS_TRANSITIONS.items()}
    assert actual == expected


async def test_bug_status_every_legal_transition_is_accepted(db: Database) -> None:
    """Every edge in the pinned table is accepted and lands the candidate in the target state."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    for i, (src, dst) in enumerate(_LEGAL_BUG_EDGES):
        cid = await _seed_candidate(db, p.id, fingerprint=f"legal-{i}", status=src)
        updated = await repo.transition_bug_status(db, cid, dst)
        assert updated.status is dst, f"{src.value} → {dst.value} should be legal"


async def test_bug_status_illegal_transitions_are_rejected(db: Database) -> None:
    """Every sampled illegal edge raises IllegalBugTransition and leaves the row unchanged."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    for i, (src, dst) in enumerate(_ILLEGAL_BUG_EDGES):
        cid = await _seed_candidate(db, p.id, fingerprint=f"illegal-{i}", status=src)
        with pytest.raises(IllegalBugTransition):
            await repo.transition_bug_status(db, cid, dst)
        # The rejected write rolled back: the candidate is still parked at the source state.
        bc = await repo.get_bug_candidate(db, cid)
        assert bc is not None and bc.status is src


async def test_bug_status_missing_candidate_raises(db: Database) -> None:
    """Transitioning a non-existent candidate is an illegal transition, not a silent no-op."""
    with pytest.raises(IllegalBugTransition):
        await repo.transition_bug_status(db, "ghost", BugStatus.reproduced)


async def test_bug_status_fix_retry_loop_and_attempts(db: Database) -> None:
    """The fixing → reproduced retry edge re-arms a fix; each entry into fixing bumps attempts.

    Walks discovered → reproduced → fixing → reproduced → fixing → fixed, asserting attempts
    increments only on entering fixing (1, then 2) and that fixed_at is stamped at the end.
    """
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    bc = await repo.create_bug_candidate(db, project_id=p.id, fingerprint="fp-1", claim="bug")
    assert bc.attempts == 0

    reproduced = await repo.transition_bug_status(db, bc.id, BugStatus.reproduced)
    assert reproduced.status is BugStatus.reproduced
    assert reproduced.attempts == 0  # reproduction is not a fix attempt

    fixing1 = await repo.transition_bug_status(db, bc.id, BugStatus.fixing)
    assert fixing1.status is BugStatus.fixing
    assert fixing1.attempts == 1  # entering fixing bumps attempts

    # The fix attempt failed — fall back along the retry edge. Attempts is NOT bumped here.
    retried = await repo.transition_bug_status(db, bc.id, BugStatus.reproduced)
    assert retried.status is BugStatus.reproduced
    assert retried.attempts == 1

    fixing2 = await repo.transition_bug_status(db, bc.id, BugStatus.fixing)
    assert fixing2.attempts == 2  # second auto-fix attempt

    assert fixing2.fixed_at is None
    fixed = await repo.transition_bug_status(db, bc.id, BugStatus.fixed)
    assert fixed.status is BugStatus.fixed
    assert fixed.attempts == 2  # reaching fixed does not bump attempts
    assert fixed.fixed_at is not None


async def test_bug_status_exhausted_attempts_escalates_to_human(db: Database) -> None:
    """reproduced → declined_needs_human is the exhausted-attempts escalation edge.

    After a failed fix returns the candidate to reproduced, the controller escalates to a human;
    the row records the handoff reason and surfaces in list_needs_human. The sink is terminal —
    a further transition is rejected.
    """
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    bc = await repo.create_bug_candidate(db, project_id=p.id, fingerprint="fp-1", claim="bug")
    await repo.transition_bug_status(db, bc.id, BugStatus.reproduced)
    await repo.transition_bug_status(db, bc.id, BugStatus.fixing)
    await repo.transition_bug_status(db, bc.id, BugStatus.reproduced)  # fix failed, back to repro

    declined = await repo.transition_bug_status(
        db, bc.id, BugStatus.declined_needs_human, decline_reason="auto-fix exhausted"
    )
    assert declined.status is BugStatus.declined_needs_human
    assert declined.decline_reason == "auto-fix exhausted"

    needs_human = await repo.list_needs_human(db, p.id)
    assert [c.id for c in needs_human] == [bc.id]

    # The handoff sink is terminal — no onward auto-edge.
    with pytest.raises(IllegalBugTransition):
        await repo.transition_bug_status(db, bc.id, BugStatus.fixing)


async def test_bug_status_links_task_and_refreshes_last_examined(db: Database) -> None:
    """A transition links the driving task_id and refreshes the stale last_examined_at stamp."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    cid = await _seed_candidate(db, p.id, fingerprint="fp-1")  # last_examined_at = 2020-01-01

    updated = await repo.transition_bug_status(
        db, cid, BugStatus.reproduced, task_id="task-42"
    )
    assert updated.task_id == "task-42"
    assert updated.last_examined_at != "2020-01-01"  # any transition is activity → refreshed


async def test_corrupt_status_candidate_cannot_be_transitioned(db: Database) -> None:
    """A candidate whose stored status is garbage degrades to declined_needs_human (terminal),
    so the guard rejects every onward edge — a corrupt row can never be auto-advanced."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    await db.execute(
        "INSERT INTO bug_candidates (id, project_id, fingerprint, claim, status, "
        "discovered_at, last_examined_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("bc-bad", p.id, "fp-bad", "claim", "totally-bogus", "2026-01-01", "2026-01-01"),
    )
    with pytest.raises(IllegalBugTransition):
        await repo.transition_bug_status(db, "bc-bad", BugStatus.reproduced)


# --- Bug-Fixer scheduler: coverage regions ----------------------------------


async def test_upsert_coverage_region_inserts_then_updates(db: Database) -> None:
    """First upsert inserts with defaults; later upserts patch only the supplied fields."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")

    created = await repo.upsert_coverage_region(db, project_id=p.id, region="src/a", priority=5)
    assert created.priority == 5
    assert created.examined_count == 0
    assert created.last_examined_at is None

    # Bump priority only — examination history must be left intact (no clobber to defaults).
    bumped = await repo.upsert_coverage_region(db, project_id=p.id, region="src/a", priority=9)
    assert bumped.id == created.id  # same row, not a duplicate
    assert bumped.priority == 9
    assert bumped.examined_count == 0

    # Record an examination without touching priority.
    examined = await repo.upsert_coverage_region(
        db, project_id=p.id, region="src/a", last_examined_at="2026-06-01", examined_count=3
    )
    assert examined.priority == 9  # preserved
    assert examined.last_examined_at == "2026-06-01"
    assert examined.examined_count == 3

    # Still exactly one row for the (project, region) pair.
    rows = await db.fetchall("SELECT id FROM coverage WHERE project_id = ?", (p.id,))
    assert len(rows) == 1


async def test_select_next_region_ordering(db: Database) -> None:
    """select_next_region returns least-recently-examined first, priority breaking ties.

    Never-examined regions (NULL last_examined_at) lead, then oldest examined; among regions
    examined at the same time, the higher priority wins.
    """
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    assert await repo.select_next_region(db, p.id) is None  # empty project

    # never-examined (NULL); two examined on the same old day (priority tiebreak); one recent.
    await repo.upsert_coverage_region(db, project_id=p.id, region="never", priority=0)
    await repo.upsert_coverage_region(
        db, project_id=p.id, region="old-hi", priority=9, last_examined_at="2026-01-01"
    )
    await repo.upsert_coverage_region(
        db, project_id=p.id, region="old-lo", priority=1, last_examined_at="2026-01-01"
    )
    await repo.upsert_coverage_region(
        db, project_id=p.id, region="recent", priority=9, last_examined_at="2026-06-01"
    )

    # NULL last_examined_at sorts first → the never-examined region is picked.
    nxt = await repo.select_next_region(db, p.id)
    assert nxt is not None and nxt.region == "never"

    # Mark it examined most-recently; now the two old regions lead, priority breaks their tie.
    await repo.upsert_coverage_region(
        db, project_id=p.id, region="never", last_examined_at="2026-06-02"
    )
    assert (await repo.select_next_region(db, p.id)).region == "old-hi"  # type: ignore[union-attr]

    # Age out old-hi → the equally-old but lower-priority region is next.
    await repo.upsert_coverage_region(
        db, project_id=p.id, region="old-hi", last_examined_at="2026-06-03"
    )
    assert (await repo.select_next_region(db, p.id)).region == "old-lo"  # type: ignore[union-attr]

    # Age out old-lo → the only remaining stale region (2026-06-01) is next.
    await repo.upsert_coverage_region(
        db, project_id=p.id, region="old-lo", last_examined_at="2026-06-04"
    )
    assert (await repo.select_next_region(db, p.id)).region == "recent"  # type: ignore[union-attr]


async def test_coverage_region_scoped_per_project(db: Database) -> None:
    """select_next_region never crosses project boundaries."""
    p1 = await repo.create_project(db, name="p1", path="/tmp/p1", default_branch="main")
    p2 = await repo.create_project(db, name="p2", path="/tmp/p2", default_branch="main")
    await repo.upsert_coverage_region(db, project_id=p1.id, region="only-p1")

    assert (await repo.select_next_region(db, p1.id)).region == "only-p1"  # type: ignore[union-attr]
    assert await repo.select_next_region(db, p2.id) is None


async def test_list_coverage_regions_canonical_order_and_scope(db: Database) -> None:
    """list_coverage_regions returns every region in select_next_region's order, project-scoped."""
    p1 = await repo.create_project(db, name="p1", path="/tmp/p1", default_branch="main")
    p2 = await repo.create_project(db, name="p2", path="/tmp/p2", default_branch="main")
    assert await repo.list_coverage_regions(db, p1.id) == []  # empty project

    # never-examined (NULL); two examined on the same old day (priority tiebreak); one recent.
    await repo.upsert_coverage_region(db, project_id=p1.id, region="never", priority=0)
    await repo.upsert_coverage_region(
        db, project_id=p1.id, region="old-hi", priority=9, last_examined_at="2026-01-01"
    )
    await repo.upsert_coverage_region(
        db, project_id=p1.id, region="old-lo", priority=1, last_examined_at="2026-01-01"
    )
    await repo.upsert_coverage_region(
        db, project_id=p1.id, region="recent", priority=9, last_examined_at="2026-06-01"
    )
    # A region in another project must not leak into p1's list.
    await repo.upsert_coverage_region(db, project_id=p2.id, region="only-p2")

    ordered = [r.region for r in await repo.list_coverage_regions(db, p1.id)]
    assert ordered == ["never", "old-hi", "old-lo", "recent"]
    # The first element always equals the single-row picker.
    head = await repo.select_next_region(db, p1.id)
    assert head is not None and head.region == ordered[0]
