"""Unit tests for the planning session orchestrator and repository layer."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from conclave.db import Database
from conclave.db import repositories as repo
from conclave.db.planning_models import (
    PlanningNodeStatus,
    PlanningSessionStatus,
    PlanningTaskNode,
)
from conclave.events import EventBus
from conclave.planning.session import _SPECIALIST_AGENTS, PlanningOrchestrator
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


async def test_render_task_tree_cycle_safe() -> None:
    """_render_task_tree terminates on a cyclic parent_id triangle and surfaces
    every node exactly once with a [cycle] marker on unrooted nodes."""
    from conclave.planning.session import PlanningOrchestrator

    # Triangle cycle: A → B → C → A — none has parent_id=None, forming a
    # pure 3-cycle.  The root-anchored pass finds nothing; the post-pass
    # renders all three at root level with [cycle] markers.
    nodes = [
        PlanningTaskNode(
            id="A", session_id="s1", parent_id="C",
            title="Task A", description="Alpha task", level=0, sort_order=0,
            status=PlanningNodeStatus.proposed,
            created_at="2026-01-01", updated_at="2026-01-01",
        ),
        PlanningTaskNode(
            id="B", session_id="s1", parent_id="A",
            title="Task B", description="Beta task", level=0, sort_order=1,
            status=PlanningNodeStatus.refined,
            created_at="2026-01-01", updated_at="2026-01-01",
        ),
        PlanningTaskNode(
            id="C", session_id="s1", parent_id="B",
            title="Task C", description="Gamma task", level=0, sort_order=2,
            status=PlanningNodeStatus.proposed,
            created_at="2026-01-01", updated_at="2026-01-01",
        ),
    ]

    rendered = PlanningOrchestrator._render_task_tree(nodes)

    # Each task appears exactly once.
    assert rendered.count("(id=A)") == 1
    assert rendered.count("(id=B)") == 1
    assert rendered.count("(id=C)") == 1

    # Task titles are present.
    assert "Task A" in rendered
    assert "Task B" in rendered
    assert "Task C" in rendered

    # Every node carries the [cycle] marker (none was reachable from a root).
    assert "[cycle]" in rendered
    assert rendered.count("[cycle]") == 3


# --- shutdown behaviour (CON-2) ------------------------------------------------


class _BlockingProvider:
    """Provider stub whose ``run_agent`` blocks forever.

    Used to test that shutdown cancels and awaits a running session
    without touching a closed database.
    """

    async def run_agent(self, *, profile, prompt, timeout_seconds, cwd=None, on_chunk=None):
        await asyncio.Event().wait()  # never returns
        return AgentResult(ok=True, text="unreachable", model_reported="fake", cost_usd=0.0)


class _OneShotThenBlockProvider:
    """Returns one planner response with a task breakdown, then blocks forever.

    Used to test approve_session on active sessions: the initial planner turn
    creates task nodes, but the session stays active because the provider never
    returns again, letting us verify that approve_session cancels the loop
    before creating real tasks.
    """

    _PLANNER_INITIAL = """Here is the initial task breakdown.

```json
{
  "message": "Breakdown with tasks.",
  "task_changes": [
    {"action": "add", "parent_id": null, "title": "Task Alpha", "description": "First task"},
    {"action": "add", "parent_id": null, "title": "Task Beta", "description": "Second task"}
  ],
  "ready": false
}
```"""

    _has_initial: bool

    def __init__(self) -> None:
        self._has_initial = False

    async def run_agent(self, *, profile, prompt, timeout_seconds, cwd=None, on_chunk=None):
        if "Planning Facilitator Agent" in prompt and not self._has_initial:
            self._has_initial = True
            return AgentResult(
                ok=True, text=self._PLANNER_INITIAL, model_reported="fake", cost_usd=0.0,
            )
        # All subsequent calls block forever, keeping the session active.
        await asyncio.Event().wait()
        return AgentResult(ok=True, text="unreachable", model_reported="fake", cost_usd=0.0)


class _QuickFakeProvider:
    """Minimal deterministic provider for daemon-level shutdown tests.

    Returns sane responses for planning agents, enough to let a daemon start and a
    planning session complete.
    """

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
    _APPROVED = (
        "The plan looks complete.\n"
        '```json\n{"verdict": "pass", "reason": "complete"}\n```'
    )

    _has_initial: bool

    def __init__(self) -> None:
        self._has_initial = False
        self.prompts: list[str] = []

    async def run_agent(self, *, profile, prompt, timeout_seconds, cwd=None, on_chunk=None):
        self.prompts.append(prompt)
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


async def test_daemon_shutdown_cancels_background_tasks(db: Database, tmp_path: Path) -> None:
    """Daemon.shutdown must cancel any in-flight ``_bg_tasks`` before db.close()."""
    home = tmp_path / "conclave-home"
    home.mkdir(parents=True, exist_ok=True)

    daemon = Daemon(db, home, _QuickFakeProvider(), workers_enabled=True)
    await daemon.start()

    # Inject a stuck background task to stand in for any tracked housekeeping task.
    async def stuck_bg() -> None:
        await asyncio.Event().wait()

    bg = asyncio.create_task(stuck_bg())
    daemon._bg_tasks.add(bg)
    bg.add_done_callback(daemon._bg_tasks.discard)
    await asyncio.sleep(0)  # let the task enter its await

    # Shutdown must cancel the stuck task and clean up without raising.
    await daemon.shutdown()

    # Background tasks must be cleared after shutdown.
    assert not daemon._bg_tasks
    assert bg.cancelled()
    # The orchestrator should have been shut down too.
    assert not daemon.planning_orchestrator._active_sessions
    assert not daemon.planning_orchestrator._bg_tasks


async def test_approve_session_awaits_cancelled_loop(db: Database) -> None:
    """approve_session must await the cancelled background loop so it fully unwinds.

    Before the fix, approve_session popped the task and called cancel() without
    awaiting it — a fire-and-forget cancel that raced the loop's own DB writes.
    Now it mirrors cancel_session: cancel + await with CancelledError suppression
    so the task is fully done before approve returns.
    """
    project = await repo.create_project(
        db, name="approve-test", path="/tmp/approve-test", default_branch="main",
    )
    bus = EventBus(db)
    orchestrator = PlanningOrchestrator(db, bus, _BlockingProvider())

    # Start a session — the discussion loop will block inside run_agent forever.
    session = await orchestrator.create_and_start(
        project_id=project.id,
        title="Approve Awaits Test",
        prompt="Test that approve_session awaits the cancelled loop.",
    )
    # Let the spawned task enter _run_discussion and block on run_agent.
    await asyncio.sleep(0)

    assert session.id in orchestrator._active_sessions

    # approve_session must cancel AND await the background task so it is
    # fully unwound before we return — no torn-write race with the DB.
    await orchestrator.approve_session(session.id)

    # After approve returns the task must be removed from tracking AND done.
    assert session.id not in orchestrator._active_sessions


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


# --- approve_session guards (PLAN-2) ----------------------------------------


async def test_approve_session_idempotent(db: Database) -> None:
    """Re-approving a completed session returns the same task_ids and creates
    zero additional Task rows."""
    project = await repo.create_project(
        db, name="idempotent-test", path="/tmp/idempotent-test", default_branch="main",
    )
    bus = EventBus(db)
    orchestrator = PlanningOrchestrator(db, bus, _QuickFakeProvider())

    # Start a session and wait for the discussion loop to finish.
    # _QuickFakeProvider produces APPROVED responses for all reviewers, so the
    # session reaches stable after round 1.
    session = await orchestrator.create_and_start(
        project_id=project.id,
        title="Idempotency Test",
        prompt="Test that re-approval is idempotent.",
        max_rounds=2,
    )

    # Wait for the background discussion loop to complete (session → stable).
    for _ in range(100):
        refreshed = await repo.get_planning_session(db, session.id)
        assert refreshed is not None
        if refreshed.status in (PlanningSessionStatus.stable, PlanningSessionStatus.completed):
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("session never reached stable/completed")

    # First approval — materialises tasks.
    first_ids = await orchestrator.approve_session(session.id)
    assert len(first_ids) > 0

    # Count tasks linked to the session after first approval.
    nodes_after_first = await repo.list_planning_task_nodes(db, session.id)
    first_task_count = sum(1 for n in nodes_after_first if n.task_id)
    assert first_task_count > 0

    # Second approval — must be idempotent: returns the same node-derived
    # task IDs (the session parent task is not returned on re-approval) and
    # creates ZERO additional Task rows.
    second_ids = await orchestrator.approve_session(session.id)
    # The idempotent return is [n.task_id for n in nodes if n.task_id] —
    # every planning node that already has a linked task.
    expected_ids = [n.task_id for n in nodes_after_first if n.task_id]
    assert second_ids == expected_ids

    nodes_after_second = await repo.list_planning_task_nodes(db, session.id)
    second_task_count = sum(1 for n in nodes_after_second if n.task_id)
    assert second_task_count == first_task_count


async def test_approve_session_on_active_cancels_loop_before_create(db: Database) -> None:
    """Approving an active session cancels the bg loop and creates exactly
    one set of tasks — no duplicates from a racing discussion."""
    project = await repo.create_project(
        db, name="active-approve-test", path="/tmp/active-approve-test",
        default_branch="main",
    )
    bus = EventBus(db)
    # _OneShotThenBlockProvider: initial planner turn creates 2 task nodes,
    # then blocks forever so the session stays active.
    orchestrator = PlanningOrchestrator(db, bus, _OneShotThenBlockProvider())

    session = await orchestrator.create_and_start(
        project_id=project.id,
        title="Active Approve Test",
        prompt="Test that active approval cancels loop first.",
    )
    # Let the spawned task enter _run_discussion and complete the initial
    # planner turn (which creates task nodes), then block.
    await asyncio.sleep(0.1)

    # Sanity: session is active and has task nodes.
    refreshed = await repo.get_planning_session(db, session.id)
    assert refreshed is not None
    assert refreshed.status == PlanningSessionStatus.active
    assert session.id in orchestrator._active_sessions

    nodes_before = await repo.list_planning_task_nodes(db, session.id)
    assert len(nodes_before) == 2  # Task Alpha + Task Beta

    # Approve the active session — must cancel and await the loop before
    # materialising tasks.
    task_ids = await orchestrator.approve_session(session.id)

    # The session is no longer tracked as active.
    assert session.id not in orchestrator._active_sessions

    # Session is now completed.
    session_after = await repo.get_planning_session(db, session.id)
    assert session_after is not None
    assert session_after.status == PlanningSessionStatus.completed

    # Tasks were created: 1 session parent + 2 node tasks.
    assert len(task_ids) == 3

    # Re-approval is idempotent — no new tasks created.
    second_ids = await orchestrator.approve_session(session.id)
    # Idempotent return gives node-derived task IDs (no session parent).
    nodes_after = await repo.list_planning_task_nodes(db, session.id)
    expected_repeat = [n.task_id for n in nodes_after if n.task_id]
    assert second_ids == expected_repeat


async def test_approve_session_rejects_cancelled(db: Database) -> None:
    """Approving a cancelled session raises ValueError."""
    project = await repo.create_project(
        db, name="cancelled-reject-test", path="/tmp/cancelled-reject-test",
        default_branch="main",
    )
    bus = EventBus(db)
    orchestrator = PlanningOrchestrator(db, bus, _QuickFakeProvider())

    session = await orchestrator.create_and_start(
        project_id=project.id,
        title="Cancelled Reject Test",
        prompt="Test that cancelled session approval is rejected.",
    )

    # Cancel the session.
    await orchestrator.cancel_session(session.id)

    refreshed = await repo.get_planning_session(db, session.id)
    assert refreshed is not None
    assert refreshed.status == PlanningSessionStatus.cancelled

    # Approving a cancelled session must raise.
    with pytest.raises(ValueError, match="session is cancelled"):
        await orchestrator.approve_session(session.id)


# --- per-session lock serialization (PLAN-3) ----------------------------------


class _GatedProvider:
    """Provider that blocks the first ``run_agent`` on an asyncio.Event.

    Used to test per-session lock serialisation: the first call (Round 0
    planner) holds the lock while blocked on the gate, so a concurrent
    ``add_human_message`` turn must wait until the gate is opened.
    """

    def __init__(self) -> None:
        self.gate: asyncio.Event = asyncio.Event()
        self.enter_count: int = 0
        self.planner_calls: int = 0
        # Fired as soon as the first run_agent call enters (before it blocks).
        self._round0_entered: asyncio.Event = asyncio.Event()

    async def run_agent(
        self,
        *,
        profile: object,
        prompt: str,
        timeout_seconds: int,
        cwd: object = None,
        on_chunk: object = None,
    ) -> AgentResult:
        self.enter_count += 1

        if self.enter_count == 1:
            self._round0_entered.set()
            await self.gate.wait()

        if "Planning Facilitator" in prompt:
            self.planner_calls += 1
            pc = self.planner_calls
            if pc <= 2:
                return AgentResult(
                    ok=True,
                    text=f'```json\n{{"message":"ok","task_changes":[{{"action":"add","parent_id":null,"title":"Task{pc}","description":"desc"}}],"ready":false}}\n```',
                    model_reported="fake",
                    cost_usd=0.0,
                )
            return AgentResult(
                ok=True,
                text='```json\n{"message":"done","task_changes":[],"ready":true}\n```',
                model_reported="fake",
                cost_usd=0.0,
            )
        return AgentResult(
            ok=True,
            text='```json\n{"verdict": "pass", "reason": "ok"}\n```',
            model_reported="fake",
            cost_usd=0.0,
        )


async def test_concurrent_turns_on_same_session_are_serialized(db: Database) -> None:
    """Two turns on the same session must never run concurrently.

    Round 0 is blocked on a gate while holding the per-session lock.
    A human interjection spawns a second turn that must wait for the
    lock.  Once the gate opens both turns complete serially, producing
    exactly two task nodes with no duplicates or sort_order collisions.
    """
    project = await repo.create_project(
        db, name="lock-test", path="/tmp/lock-test", default_branch="main",
    )
    bus = EventBus(db)
    provider = _GatedProvider()
    orchestrator = PlanningOrchestrator(db, bus, provider)

    # Start a session — Round 0 planner enters run_agent and blocks on gate.
    session = await orchestrator.create_and_start(
        project_id=project.id,
        title="Lock Serialisation Test",
        prompt="Test per-session lock serialises concurrent turns.",
    )

    # Wait until Round 0 has entered run_agent (holding the lock).
    await provider._round0_entered.wait()

    # At this point only Round 0 has entered run_agent.
    assert provider.enter_count == 1

    # Simulate a human interjection arriving while Round 0 is still blocked.
    # This spawns a background _agent_turn that must wait for the lock.
    await orchestrator.add_human_message(session.id, "Please add another task.")

    # Give the background task a chance to reach the lock — it should be
    # blocked waiting, so enter_count must still be 1.
    await asyncio.sleep(0)

    assert provider.enter_count == 1, (
        f"Expected enter_count==1 (bg turn waiting on lock), got {provider.enter_count}"
    )

    # Open the gate — Round 0 finishes, releases the lock, then the bg
    # turn acquires it and runs.
    provider.gate.set()

    # Wait for the background turn to complete (enter_count >= 2) and the
    # discussion loop to reach stable.
    for _ in range(200):
        refreshed = await repo.get_planning_session(db, session.id)
        if refreshed is not None and refreshed.status in (
            PlanningSessionStatus.stable,
            PlanningSessionStatus.completed,
        ):
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("session never reached stable/completed")

    # Both turns must have entered run_agent (serially, not concurrently).
    assert provider.enter_count >= 2, (
        f"Expected enter_count>=2, got {provider.enter_count}"
    )

    # Exactly two task nodes were created (Task1 from Round 0, Task2 from bg turn).
    # No duplicates — the title-dedupe guard held because turns were serialised.
    nodes = await repo.list_planning_task_nodes(db, session.id)
    assert len(nodes) == 2, f"Expected 2 nodes, got {len(nodes)}"

    titles = sorted(n.title for n in nodes)
    assert titles == ["Task1", "Task2"], f"Unexpected titles: {titles}"

    # No sort_order collisions: each root-level node has a distinct sort_order.
    root_nodes = [n for n in nodes if n.parent_id is None]
    sort_orders = [n.sort_order for n in root_nodes]
    assert len(sort_orders) == len(set(sort_orders)), (
        f"sort_order collision detected: {sort_orders}"
    )


# --- regression: structured verdicts, not substring matching -----------------


class _FailVerdictWithApprovedProseProvider:
    """Reviewers whose prose contains the word 'APPROVED' but whose structured
    verdict is 'fail'.  Locks in that stabilisation keys on ``parse_verdict``,
    not a naive ``"APPROVED" in text`` substring — which this text would wrongly
    satisfy, flipping a rejection into a false consensus.
    """

    def __init__(self) -> None:
        self._planner_calls = 0

    async def run_agent(self, *, profile, prompt, timeout_seconds, cwd=None, on_chunk=None):
        if "Planning Facilitator" in prompt:
            self._planner_calls += 1
            if self._planner_calls == 1:
                text = (
                    '```json\n{"message":"init","task_changes":'
                    '[{"action":"add","parent_id":null,"title":"Task A","description":"d"}],'
                    '"ready":false}\n```'
                )
            else:
                text = '```json\n{"message":"done","task_changes":[],"ready":true}\n```'
            return AgentResult(ok=True, text=text, model_reported="fake", cost_usd=0.0)
        # Reviewer: prose name-drops "APPROVED" but the structured verdict is fail.
        return AgentResult(
            ok=True,
            text=(
                "This plan is NOT APPROVED in its current form — gaps remain.\n"
                '```json\n{"verdict": "fail", "reason": "gaps remain"}\n```'
            ),
            model_reported="fake",
            cost_usd=0.0,
        )


async def test_reviewer_prose_with_approved_word_does_not_count_as_approval(
    db: Database,
) -> None:
    """A reviewer that name-drops 'APPROVED' in prose but returns a 'fail'
    verdict must NOT be counted as an approval.  Consensus can never fire, so the
    session reaches stable only via the max-rounds fallback.  Under the old
    substring gate the 'APPROVED' prose would have wrongly triggered consensus
    (leaving stabilization_reason unset)."""
    project = await repo.create_project(
        db, name="verdict-test", path="/tmp/verdict-test", default_branch="main",
    )
    orchestrator = PlanningOrchestrator(
        db, EventBus(db), _FailVerdictWithApprovedProseProvider()
    )

    session = await orchestrator.create_and_start(
        project_id=project.id,
        title="Verdict Gate",
        prompt="Decompose a feature.",
        max_rounds=2,
    )

    for _ in range(300):
        refreshed = await repo.get_planning_session(db, session.id)
        assert refreshed is not None
        if refreshed.status in (PlanningSessionStatus.stable, PlanningSessionStatus.completed):
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("session never reached stable/completed")

    after = await repo.get_planning_session(db, session.id)
    assert after is not None
    assert after.status == PlanningSessionStatus.stable
    assert after.stabilization_reason == "max_rounds_reached"


# --- regression: reviewers vote on the post-refinement tree ------------------


class _MutateThenReadyProvider:
    """Planner that, in a later turn, mutates the tree (adds 'BadTask') AND sets
    ready:true in the same turn.  Reviewers reject any tree containing 'BadTask'.

    Under the corrected round order (planner first, then reviewers) the reviewers
    vote on the *mutated* tree, reject it, and the session never reaches
    consensus.  Under the old order (reviewers vote, then the planner mutates and
    readies in the same round) the session would have stabilised a 'BadTask' tree
    the reviewers never approved.
    """

    def __init__(self) -> None:
        self._planner_calls = 0

    async def run_agent(self, *, profile, prompt, timeout_seconds, cwd=None, on_chunk=None):
        if "Planning Facilitator" in prompt:
            self._planner_calls += 1
            if self._planner_calls == 1:
                text = (
                    '```json\n{"message":"init","task_changes":'
                    '[{"action":"add","parent_id":null,"title":"GoodTask","description":"d"}],'
                    '"ready":false}\n```'
                )
            else:
                # Mutate (add BadTask) AND declare ready in the same turn.
                text = (
                    '```json\n{"message":"refine","task_changes":'
                    '[{"action":"add","parent_id":null,"title":"BadTask","description":"d"}],'
                    '"ready":true}\n```'
                )
            return AgentResult(ok=True, text=text, model_reported="fake", cost_usd=0.0)
        # Reviewer: reject any tree that contains BadTask.
        verdict = "fail" if "BadTask" in prompt else "pass"
        return AgentResult(
            ok=True,
            text=f'```json\n{{"verdict": "{verdict}", "reason": "r"}}\n```',
            model_reported="fake",
            cost_usd=0.0,
        )


async def test_reviewers_vote_on_post_refinement_tree(db: Database) -> None:
    """The planner's mutate-and-ready turn must be reviewed before stabilising.

    Because the planner adds 'BadTask' (which reviewers reject) in the same turn
    it signals ready, consensus can never fire on that tree — the session falls
    through to the max-rounds fallback.  This proves reviewers evaluate the
    post-refinement tree, closing the 'approve V, ship V+1' gap."""
    project = await repo.create_project(
        db, name="order-test", path="/tmp/order-test", default_branch="main",
    )
    orchestrator = PlanningOrchestrator(db, EventBus(db), _MutateThenReadyProvider())

    session = await orchestrator.create_and_start(
        project_id=project.id,
        title="Round Order",
        prompt="Decompose a feature.",
        max_rounds=3,
    )

    for _ in range(300):
        refreshed = await repo.get_planning_session(db, session.id)
        assert refreshed is not None
        if refreshed.status in (PlanningSessionStatus.stable, PlanningSessionStatus.completed):
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("session never reached stable/completed")

    after = await repo.get_planning_session(db, session.id)
    assert after is not None
    assert after.status == PlanningSessionStatus.stable
    # No false consensus on the unreviewed 'BadTask' tree — only the max-rounds
    # fallback could have stabilised it.
    assert after.stabilization_reason == "max_rounds_reached"

    # The mutating turn did run, so BadTask was added to the tree.
    nodes = await repo.list_planning_task_nodes(db, session.id)
    titles = sorted(n.title for n in nodes)
    assert titles == ["BadTask", "GoodTask"], f"Unexpected titles: {titles}"


# --- regression: hybrid reviewer topology (parallel specialists, senior last) ---


class _HybridTopologyProbeProvider:
    """Proves the round's specialists are dispatched concurrently and the senior
    "final reviewer" runs after them.

    Each specialist call registers itself in flight and blocks on a barrier that
    only releases once all specialists are in flight at once — so a sequential
    dispatch would deadlock (and the test would time out). The senior reviewer
    records how many specialists had completed before it started.
    """

    _PASS = '```json\n{"verdict": "pass", "reason": "ok"}\n```'

    def __init__(self, num_specialists: int) -> None:
        self._planner_calls = 0
        self._num_specialists = num_specialists
        self._in_flight = 0
        self.max_in_flight = 0
        self.specialists_done = 0
        self.senior_started_after = -1
        self._barrier = asyncio.Event()

    async def run_agent(self, *, profile, prompt, timeout_seconds, cwd=None, on_chunk=None):
        if "Planning Facilitator" in prompt:
            self._planner_calls += 1
            if self._planner_calls == 1:
                text = (
                    '```json\n{"message":"init","task_changes":'
                    '[{"action":"add","parent_id":null,"title":"Task A","description":"d"}],'
                    '"ready":true}\n```'
                )
            else:
                text = '```json\n{"message":"done","task_changes":[],"ready":true}\n```'
            return AgentResult(ok=True, text=text, model_reported="fake", cost_usd=0.0)
        if "Senior Reviewer Agent" in prompt:
            # Records the state at the moment the senior reviewer is dispatched.
            self.senior_started_after = self.specialists_done
            return AgentResult(ok=True, text=self._PASS, model_reported="fake", cost_usd=0.0)
        # Specialist: block until every specialist is concurrently in flight.
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        if self._in_flight >= self._num_specialists:
            self._barrier.set()
        await self._barrier.wait()
        self._in_flight -= 1
        self.specialists_done += 1
        return AgentResult(ok=True, text=self._PASS, model_reported="fake", cost_usd=0.0)


async def test_specialists_run_in_parallel_then_senior_reviewer(db: Database) -> None:
    """The specialist reviewers are dispatched concurrently (proven by a barrier
    that only releases when all are in flight at once), and the senior reviewer
    runs only after every specialist has finished."""
    n = len(_SPECIALIST_AGENTS)
    project = await repo.create_project(
        db, name="hybrid-test", path="/tmp/hybrid-test", default_branch="main",
    )
    provider = _HybridTopologyProbeProvider(num_specialists=n)
    orchestrator = PlanningOrchestrator(db, EventBus(db), provider)

    session = await orchestrator.create_and_start(
        project_id=project.id,
        title="Hybrid Topology",
        prompt="Decompose a feature.",
        max_rounds=1,
    )

    for _ in range(1000):
        refreshed = await repo.get_planning_session(db, session.id)
        assert refreshed is not None
        if refreshed.status in (PlanningSessionStatus.stable, PlanningSessionStatus.completed):
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("session never reached stable/completed (parallel deadlock?)")

    # All specialists were in flight simultaneously → dispatched in parallel.
    assert provider.max_in_flight == n, (
        f"specialists not concurrent: max_in_flight={provider.max_in_flight} (expected {n})"
    )
    # The senior reviewer started only after every specialist had completed.
    assert provider.senior_started_after == n, (
        f"senior reviewer did not run last: started_after={provider.senior_started_after}"
    )
