"""Unit tests for M-PLAN hardening — safe task_changes parsing and session-scoping.

Covers five defect classes from the HAR-2 analysis plus the max-rounds
stabilisation reason persistence.
"""

from __future__ import annotations

import asyncio

from conclave.db import Database
from conclave.db import repositories as repo
from conclave.db.planning_models import PlanningSessionStatus
from conclave.events import EventBus
from conclave.planning.session import PlanningOrchestrator
from conclave.providers import AgentResult

# ---------------------------------------------------------------------------
# stub provider — never called by _apply_task_changes, only needed for
# orchestrator construction and tests that exercise the discussion loop.
# ---------------------------------------------------------------------------


class _StubProvider:
    """Provider stub that returns a bare response.

    ``_agent_turn`` calls ``run_agent`` for every turn, but the hardening
    tests often call ``_apply_task_changes`` directly.  This stub satisfies
    the constructor requirement.
    """

    async def run_agent(self, *, profile, prompt, timeout_seconds, cwd=None, on_chunk=None):
        return AgentResult(ok=True, text="ok", model_reported="fake", cost_usd=0.0)


def _make_orchestrator(db: Database) -> PlanningOrchestrator:
    """Convenience factory for tests that call ``_apply_task_changes`` directly."""
    return PlanningOrchestrator(db, EventBus(db), _StubProvider())


# ---------------------------------------------------------------------------
# 1. Missing / unknown id
# ---------------------------------------------------------------------------


async def test_apply_task_changes_skips_change_with_missing_id(db: Database) -> None:
    """A change dict with no 'id' key for update/remove is skipped — no KeyError."""
    project = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    session = await repo.create_planning_session(
        db, project_id=project.id, title="T", prompt="Do X",
    )
    node = await repo.add_planning_task_node(
        db, session_id=session.id, parent_id=None,
        title="Task 1", description="desc", level=0, sort_order=0,
    )

    orchestrator = _make_orchestrator(db)
    changes = [
        {"action": "update", "title": "New title"},  # no "id"
        {"action": "remove"},                         # no "id"
    ]
    await orchestrator._apply_task_changes(session.id, project.id, changes)

    # Node is untouched
    refreshed = await repo.get_planning_task_node(db, node.id)
    assert refreshed is not None
    assert refreshed.title == "Task 1"


async def test_apply_task_changes_skips_change_with_unknown_id(db: Database) -> None:
    """A change targeting a non-existent node id is skipped after fetch returns None."""
    project = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    session = await repo.create_planning_session(
        db, project_id=project.id, title="T", prompt="Do X",
    )

    orchestrator = _make_orchestrator(db)
    changes = [
        {"action": "update", "id": "nonexistent-abc", "title": "Ghost"},
        {"action": "remove", "id": "nonexistent-xyz"},
    ]
    # Must not raise
    await orchestrator._apply_task_changes(session.id, project.id, changes)


# ---------------------------------------------------------------------------
# 2. Session-scoped update / remove
# ---------------------------------------------------------------------------


async def test_apply_task_changes_update_scoped_to_session(db: Database) -> None:
    """An update targeting a node from a DIFFERENT session is skipped — no cross-session
    mutation."""
    project = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    sa = await repo.create_planning_session(
        db, project_id=project.id, title="Session A", prompt="Do X",
    )
    sb = await repo.create_planning_session(
        db, project_id=project.id, title="Session B", prompt="Do Y",
    )
    node_b = await repo.add_planning_task_node(
        db, session_id=sb.id, parent_id=None,
        title="B Node", description="desc", level=0, sort_order=0,
    )

    orchestrator = _make_orchestrator(db)
    changes = [{"action": "update", "id": node_b.id, "title": "Hijacked"}]
    await orchestrator._apply_task_changes(sa.id, project.id, changes)

    # Node B must be unchanged — session A cannot mutate it.
    refreshed = await repo.get_planning_task_node(db, node_b.id)
    assert refreshed is not None
    assert refreshed.title == "B Node"


async def test_apply_task_changes_remove_scoped_to_session(db: Database) -> None:
    """A remove targeting another session's node is skipped — no cross-session deletion."""
    project = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    sa = await repo.create_planning_session(
        db, project_id=project.id, title="Session A", prompt="Do X",
    )
    sb = await repo.create_planning_session(
        db, project_id=project.id, title="Session B", prompt="Do Y",
    )
    node_b = await repo.add_planning_task_node(
        db, session_id=sb.id, parent_id=None,
        title="B Node", description="desc", level=0, sort_order=0,
    )

    orchestrator = _make_orchestrator(db)
    changes = [{"action": "remove", "id": node_b.id}]
    await orchestrator._apply_task_changes(sa.id, project.id, changes)

    # Node B must still exist.
    refreshed = await repo.get_planning_task_node(db, node_b.id)
    assert refreshed is not None


# ---------------------------------------------------------------------------
# 3. Non-list task_changes
# ---------------------------------------------------------------------------


async def test_apply_task_changes_handles_non_list(db: Database) -> None:
    """Passing a dict, string, or None as changes is logged and returns without crash."""
    project = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    session = await repo.create_planning_session(
        db, project_id=project.id, title="T", prompt="Do X",
    )

    orchestrator = _make_orchestrator(db)

    # None
    await orchestrator._apply_task_changes(session.id, project.id, None)  # type: ignore[arg-type]

    # dict
    await orchestrator._apply_task_changes(session.id, project.id, {"key": "val"})  # type: ignore[arg-type]

    # string
    await orchestrator._apply_task_changes(session.id, project.id, "not a list")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 4. project_id in planning_task_proposed
# ---------------------------------------------------------------------------


async def test_apply_task_changes_emits_proposed_with_project_id(db: Database) -> None:
    """planning_task_proposed event carries the real project_id (not None)."""
    project = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    session = await repo.create_planning_session(
        db, project_id=project.id, title="T", prompt="Do X",
    )

    orchestrator = _make_orchestrator(db)
    changes = [
        {"action": "add", "parent_id": None, "title": "New Task", "description": "Desc"},
    ]
    await orchestrator._apply_task_changes(session.id, project.id, changes)

    # Assert the persisted event row has the correct project_id.
    rows = await db.fetchall(
        "SELECT * FROM events WHERE type = ? AND planning_session_id = ? "
        "ORDER BY id DESC LIMIT 1",
        ("planning.task_proposed", session.id),
    )
    assert len(rows) > 0
    event_row = rows[0]
    assert event_row["project_id"] == project.id, (
        f"Expected project_id={project.id}, got {event_row['project_id']}"
    )


# ---------------------------------------------------------------------------
# 5. Max-rounds stabilisation reason persistence
# ---------------------------------------------------------------------------


async def test_max_rounds_stabilization_persists_reason(db: Database) -> None:
    """After max rounds are exhausted, session status=stable and
    stabilization_reason='max_rounds_reached'."""
    project = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")

    bus = EventBus(db)
    orchestrator = PlanningOrchestrator(db, bus, _StubProvider())

    session = await orchestrator.create_and_start(
        project_id=project.id,
        title="Max Rounds Test",
        prompt="Test max rounds stabilization reason.",
        max_rounds=0,
    )

    # Wait for the background discussion loop to finish (Round 0 runs, then
    # the empty for-loop hits the max-rounds branch immediately).
    for _ in range(100):
        refreshed = await repo.get_planning_session(db, session.id)
        assert refreshed is not None
        if refreshed.status in (PlanningSessionStatus.stable, PlanningSessionStatus.completed):
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("session never reached stable/completed")

    session_after = await repo.get_planning_session(db, session.id)
    assert session_after is not None
    assert session_after.status == PlanningSessionStatus.stable
    assert session_after.stabilization_reason == "max_rounds_reached"


# ---------------------------------------------------------------------------
# 6. Update within same session still works (positive-path regression guard)
# ---------------------------------------------------------------------------


async def test_apply_task_changes_update_within_session_succeeds(db: Database) -> None:
    """An update targeting a node in the SAME session succeeds — regression guard."""
    project = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    session = await repo.create_planning_session(
        db, project_id=project.id, title="T", prompt="Do X",
    )
    node = await repo.add_planning_task_node(
        db, session_id=session.id, parent_id=None,
        title="Old Title", description="Old desc", level=0, sort_order=0,
    )

    orchestrator = _make_orchestrator(db)
    changes = [
        {"action": "update", "id": node.id, "title": "New Title", "description": "New desc"},
    ]
    await orchestrator._apply_task_changes(session.id, project.id, changes)

    refreshed = await repo.get_planning_task_node(db, node.id)
    assert refreshed is not None
    assert refreshed.title == "New Title"
    assert refreshed.description == "New desc"


async def test_apply_task_changes_remove_within_session_succeeds(db: Database) -> None:
    """A remove targeting a node in the SAME session succeeds — regression guard."""
    project = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    session = await repo.create_planning_session(
        db, project_id=project.id, title="T", prompt="Do X",
    )
    node = await repo.add_planning_task_node(
        db, session_id=session.id, parent_id=None,
        title="Removable", description="desc", level=0, sort_order=0,
    )

    orchestrator = _make_orchestrator(db)
    changes = [{"action": "remove", "id": node.id}]
    await orchestrator._apply_task_changes(session.id, project.id, changes)

    refreshed = await repo.get_planning_task_node(db, node.id)
    assert refreshed is None  # node was deleted
