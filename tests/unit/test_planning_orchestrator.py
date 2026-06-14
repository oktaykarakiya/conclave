"""Unit tests for the planning session orchestrator and repository layer."""

from __future__ import annotations

import asyncio
from pathlib import Path

from conclave.db import Database
from conclave.db import repositories as repo
from conclave.db.planning_models import PlanningNodeStatus, PlanningSessionStatus
from conclave.events import EventBus
from conclave.planning.session import PlanningOrchestrator
from conclave.providers import AgentResult
from conclave.runtime import Daemon

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


# --- shutdown behaviour (CON-2) ------------------------------------------------


class _BlockingProvider:
    """Provider stub whose ``run_agent`` blocks forever.

    Used to test that shutdown cancels and awaits a running session
    without touching a closed database.
    """

    async def run_agent(self, *, profile, prompt, timeout_seconds, cwd=None, on_chunk=None):
        await asyncio.Event().wait()  # never returns
        return AgentResult(ok=True, text="unreachable", model_reported="fake", cost_usd=0.0)


class _QuickFakeProvider:
    """Minimal deterministic provider for daemon-level shutdown tests.

    Returns sane responses for planning agents and backfill enrichment,
    enough to let a daemon start and a planning session complete.
    """

    _AI_KNOWLEDGE = (
        '```json\n'
        '{"languages": [], "frameworks": [], '
        '"commands": {}, '
        '"architecture_summary": "A minimal git repository with a README.", '
        '"conventions": [], "protected_globs": [], '
        '"layout": {"dirs": []}}\n'
        '```'
    )
    _PLANNER_INITIAL = """Here is the initial task breakdown.

```json
{
  "message": "Breakdown.",
  "task_changes": [
    {"action": "add", "parent_id": null, "title": "Task A", "description": "First"}
  ],
  "ready": false
}
```"""
    _PLANNER_REFINE = """Refined.

```json
{
  "message": "Done.",
  "task_changes": [],
  "ready": true
}
```"""
    _APPROVED = "APPROVED. The plan looks complete."

    _has_initial: bool

    def __init__(self) -> None:
        self._has_initial = False
        self.prompts: list[str] = []

    async def run_agent(self, *, profile, prompt, timeout_seconds, cwd=None, on_chunk=None):
        self.prompts.append(prompt)
        # Backfill enrichment
        if "Repository Analysis" in prompt and "AI Enrichment" in prompt:
            return AgentResult(
                ok=True, text=self._AI_KNOWLEDGE, model_reported="fake", cost_usd=0.0,
            )
        # Planning discussion — planner
        if "Planning Facilitator Agent" in prompt:
            if not self._has_initial:
                self._has_initial = True
                return AgentResult(
                    ok=True, text=self._PLANNER_INITIAL, model_reported="fake", cost_usd=0.0,
                )
            return AgentResult(
                ok=True, text=self._PLANNER_REFINE, model_reported="fake", cost_usd=0.0,
            )
        # Planning discussion — reviewer agents
        if any(
            tag in prompt
            for tag in ("Architect Agent", "Tester Agent", "Security Agent",
                        "Senior Reviewer Agent", "Risk Agent")
        ):
            return AgentResult(ok=True, text=self._APPROVED, model_reported="fake", cost_usd=0.0)
        # Fallback — generic agent response
        return AgentResult(
            ok=True, text="Done. VERDICT: PASS", model_reported="fake", cost_usd=0.01,
        )


async def test_planning_orchestrator_shutdown_awaits_active_session(db: Database) -> None:
    """Shutting down while a session is stuck in run_agent must cancel and await it cleanly."""
    project = await repo.create_project(
        db, name="shutdown-test", path="/tmp/shutdown-test", default_branch="main",
    )
    bus = EventBus(db)
    orchestrator = PlanningOrchestrator(db, bus, _BlockingProvider())

    # Start a session — the discussion loop will block inside run_agent forever.
    session = await orchestrator.create_and_start(
        project_id=project.id,
        title="Shutdown Test",
        prompt="Test shutdown while session is active.",
    )
    # Let the spawned task enter _run_discussion and block on run_agent.
    await asyncio.sleep(0)

    assert session.id in orchestrator._active_sessions

    # Shutdown must cancel and await the stuck task without raising.
    await orchestrator.shutdown()

    assert not orchestrator._active_sessions
    assert not orchestrator._bg_tasks


async def test_planning_orchestrator_shutdown_cleans_bg_tasks(db: Database) -> None:
    """Shutdown must cancel and await tracked background tasks (agent-turn continuations)."""
    bus = EventBus(db)
    orchestrator = PlanningOrchestrator(db, bus, _BlockingProvider())

    # Manually inject a stuck background task to simulate an agent-turn continuation.
    async def stuck_bg() -> None:
        await asyncio.Event().wait()

    bg = asyncio.create_task(stuck_bg())
    orchestrator._bg_tasks.add(bg)
    bg.add_done_callback(orchestrator._bg_tasks.discard)

    assert len(orchestrator._bg_tasks) == 1

    await orchestrator.shutdown()

    assert not orchestrator._bg_tasks


async def test_daemon_shutdown_cancels_backfill_tasks(db: Database, tmp_path: Path) -> None:
    """Daemon.shutdown must cancel any in-flight backfill tasks before db.close()."""
    home = tmp_path / "conclave-home"
    home.mkdir(parents=True, exist_ok=True)

    project = await repo.create_project(
        db, name="backfill-test", path=str(home / "backfill-repo"), default_branch="main",
    )
    # Save a knowledge row that is NOT ai_enriched so latest_ai_knowledge
    # returns None (it only returns ai_enriched=1 rows), triggering backfill.
    knowledge: dict = {
        "languages": ["python"],
        "frameworks": [],
        "commands": {},
        "architecture_summary": "Test repo for backfill shutdown.",
        "conventions": [],
        "protected_globs": [],
        "layout": {"dirs": []},
    }
    await repo.save_repo_knowledge(
        db,
        project_id=project.id,
        knowledge=knowledge,
        sha="abc123",
        manifest_fingerprint="fp",
        ai_enriched=False,
    )

    daemon = Daemon(db, home, _QuickFakeProvider(), workers_enabled=True)
    await daemon.start()

    # Let the backfill task begin execution.
    await asyncio.sleep(0)

    # Shutdown must clean up without raising.
    await daemon.shutdown()

    # Background tasks must be cleared after shutdown.
    assert not daemon._bg_tasks
    # The orchestrator should have been shut down too.
    assert not daemon.planning_orchestrator._active_sessions
    assert not daemon.planning_orchestrator._bg_tasks


async def test_daemon_shutdown_cascades_to_planning_orchestrator(
    db: Database, tmp_path: Path,
) -> None:
    """Daemon.shutdown() must invoke planning_orchestrator.shutdown()."""
    home = tmp_path / "conclave-home"
    home.mkdir(parents=True, exist_ok=True)

    await repo.create_project(
        db, name="cascade-test", path=str(home / "cascade-repo"), default_branch="main",
    )

    daemon = Daemon(db, home, _QuickFakeProvider(), workers_enabled=True)
    await daemon.start()

    # Start a planning session to populate the orchestrator's active sessions.
    projects = await repo.list_projects(db)
    session = await daemon.planning_orchestrator.create_and_start(
        project_id=projects[0].id,
        title="Cascade Test",
        prompt="Test daemon shutdown cascades to planning orchestrator.",
    )
    await asyncio.sleep(0)

    # Sanity: session is active.
    assert session.id in daemon.planning_orchestrator._active_sessions

    # Shutdown — must cascade into the planning orchestrator.
    await daemon.shutdown()

    assert not daemon._bg_tasks
    assert not daemon.planning_orchestrator._active_sessions
    assert not daemon.planning_orchestrator._bg_tasks
