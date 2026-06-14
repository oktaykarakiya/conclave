"""Unit tests for the planning session orchestrator and repository layer."""

from __future__ import annotations

from conclave.db import Database
from conclave.db import repositories as repo
from conclave.db.planning_models import (
    PlanningNodeStatus,
    PlanningSessionStatus,
)
from conclave.events import EventBus

# --- repository layer ---------------------------------------------------------


async def test_planning_session_crud(db: Database) -> None:
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")

    # Create
    session = await repo.create_planning_session(
        db, project_id=p.id, title="Add auth", prompt="Implement OAuth2 login",
    )
    assert session.project_id == p.id
    assert session.status == PlanningSessionStatus.active
    assert session.turn_number == 0
    assert session.max_rounds == 5

    # Get
    fetched = await repo.get_planning_session(db, session.id)
    assert fetched is not None
    assert fetched.title == "Add auth"

    # List
    sessions = await repo.list_planning_sessions(db, p.id)
    assert len(sessions) == 1
    assert sessions[0].id == session.id

    # Update status
    await repo.update_planning_session_status(
        db, session.id, PlanningSessionStatus.completed,
    )
    updated = await repo.get_planning_session(db, session.id)
    assert updated is not None
    assert updated.status == PlanningSessionStatus.completed
    assert updated.completed_at is not None

    # Increment turn
    turn = await repo.increment_planning_turn(db, session.id)
    assert turn == 1
    again = await repo.get_planning_session(db, session.id)
    assert again is not None
    assert again.turn_number == 1


async def test_planning_messages(db: Database) -> None:
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    session = await repo.create_planning_session(
        db, project_id=p.id, title="T", prompt="Do X",
    )

    m1 = await repo.add_planning_message(
        db, session_id=session.id, agent="planner", role="agent",
        content="Here is the plan.", turn_number=1,
    )
    m2 = await repo.add_planning_message(
        db, session_id=session.id, agent="human", role="human",
        content="Looks good.", turn_number=2,
    )
    assert m1.agent == "planner"
    assert m2.role == "human"

    msgs = await repo.list_planning_messages(db, session.id)
    assert len(msgs) == 2
    assert msgs[0].turn_number == 1
    assert msgs[1].turn_number == 2


async def test_planning_task_nodes(db: Database) -> None:
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    session = await repo.create_planning_session(
        db, project_id=p.id, title="T", prompt="Do X",
    )

    # Add root nodes
    n1 = await repo.add_planning_task_node(
        db, session_id=session.id, parent_id=None,
        title="Task 1", description="First task", level=0, sort_order=0,
    )
    await repo.add_planning_task_node(
        db, session_id=session.id, parent_id=None,
        title="Task 2", description="Second task", level=0, sort_order=1,
    )
    assert n1.status == PlanningNodeStatus.proposed
    assert n1.level == 0

    # Add child node
    n3 = await repo.add_planning_task_node(
        db, session_id=session.id, parent_id=n1.id,
        title="Sub-task 1a", description="Sub task", level=1, sort_order=0,
    )
    assert n3.parent_id == n1.id
    assert n3.level == 1

    # List all
    nodes = await repo.list_planning_task_nodes(db, session.id)
    assert len(nodes) == 3

    # List by parent
    roots = await repo.list_planning_task_nodes_by_parent(db, session.id, None)
    assert len(roots) == 2

    children = await repo.list_planning_task_nodes_by_parent(db, session.id, n1.id)
    assert len(children) == 1
    assert children[0].id == n3.id

    # Update
    await repo.update_planning_task_node(
        db, node_id=n1.id, title="Updated Task 1", status=PlanningNodeStatus.refined.value,
    )
    updated = await repo.get_planning_task_node(db, n1.id)
    assert updated is not None
    assert updated.title == "Updated Task 1"
    assert updated.status == PlanningNodeStatus.refined

    # Delete
    await repo.delete_planning_task_node(db, n3.id)
    assert await repo.get_planning_task_node(db, n3.id) is None
    remaining = await repo.list_planning_task_nodes(db, session.id)
    assert len(remaining) == 2


async def test_events_with_planning_session_id(db: Database) -> None:
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    session = await repo.create_planning_session(
        db, project_id=p.id, title="T", prompt="Do X",
    )

    ev = await repo.append_event(
        db, type="planning.session_started", project_id=p.id,
        planning_session_id=session.id,
        payload={"planning_session_id": session.id},
    )
    assert ev.planning_session_id == session.id
    assert ev.type == "planning.session_started"


# --- event bus filtering ------------------------------------------------------


async def test_event_filter_planning_session(db: Database) -> None:
    bus = EventBus(db)
    subscriber = bus.subscribe(planning_session_id="sess-1")
    assert subscriber._filter.planning_session_id == "sess-1"


# --- orchestrator task tree rendering -----------------------------------------


async def test_render_task_tree(db: Database) -> None:
    from conclave.planning.session import PlanningOrchestrator

    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    session = await repo.create_planning_session(
        db, project_id=p.id, title="T", prompt="Do X",
    )
    n1 = await repo.add_planning_task_node(
        db, session_id=session.id, parent_id=None,
        title="Root 1", description="First", level=0, sort_order=0,
    )
    await repo.add_planning_task_node(
        db, session_id=session.id, parent_id=n1.id,
        title="Child 1a", description="Child", level=1, sort_order=0,
    )

    nodes = await repo.list_planning_task_nodes(db, session.id)
    rendered = PlanningOrchestrator._render_task_tree(nodes)
    assert "Root 1" in rendered
    assert "Child 1a" in rendered
    assert "[proposed]" in rendered


