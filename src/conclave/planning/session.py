"""Multi-agent planning session orchestrator.

Manages turn-based discussion between AI agents to decompose a feature
request into an approved task tree, then materializes it into real tasks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

from ..config import ArgMode
from ..db import Database, TaskOrigin, TaskState
from ..db import repositories as repo
from ..db.planning_models import (
    PlanningMessage,
    PlanningNodeStatus,
    PlanningSession,
    PlanningSessionStatus,
    PlanningTaskNode,
)
from ..events import EventBus, EventType
from ..providers import Provider, ResolvedProfile
from .prompts import (
    APPROVAL_AGENTS,
    DISCUSSION_AGENTS,
    PLANNER_DISCUSSION,
)

logger = logging.getLogger("conclave.planning")

# Planner is the coordinator; it talks first and after each review round
_PLANNER = "planner"
# Max context messages to include (keeps prompt size bounded)
_MAX_CONTEXT_MSGS = 20


class PlanningOrchestrator:
    """Orchestrates multi-agent planning discussion sessions.

    Each session runs as a background asyncio task. Agents take turns
    discussing and refining a task tree. The human operator can interject
    at any point. When all mandatory reviewers approve and the planner
    signals readiness, the session becomes "stable" and the operator can
    approve it, creating real tasks in the task system.
    """

    def __init__(self, db: Database, bus: EventBus, provider: Provider) -> None:
        self._db = db
        self._bus = bus
        self._provider = provider
        self._active_sessions: dict[str, asyncio.Task[None]] = {}
        self._bg_tasks: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    async def create_and_start(
        self,
        project_id: str,
        title: str,
        prompt: str,
        max_rounds: int = 5,
    ) -> PlanningSession:
        """Create a new planning session and start the discussion loop."""
        session = await repo.create_planning_session(
            self._db,
            project_id=project_id,
            title=title,
            prompt=prompt,
            max_rounds=max_rounds,
        )
        await self._bus.emit(
            type=EventType.planning_session_created,
            project_id=project_id,
            planning_session_id=session.id,
            payload={
                "planning_session_id": session.id,
                "title": title,
                "preview": prompt[:200],
            },
        )
        task = asyncio.create_task(self._run_discussion(session))
        self._active_sessions[session.id] = task
        return session

    async def add_human_message(
        self, session_id: str, content: str
    ) -> PlanningMessage:
        """Store a human interjection and trigger a planner response."""
        session = await repo.get_planning_session(self._db, session_id)
        if session is None:
            raise ValueError("session not found")

        # Bump the turn and store the human message atomically (no provider call between
        # them, unlike _agent_turn) so a turn is never consumed without its message.
        msg = await repo.add_message_with_turn(
            self._db,
            session_id=session_id,
            agent="human",
            role="human",
            content=content,
        )
        turn = msg.turn_number
        await self._bus.emit(
            type=EventType.planning_human_interject,
            project_id=session.project_id,
            planning_session_id=session_id,
            agent="human",
            payload={
                "planning_session_id": session_id,
                "turn": turn,
                "preview": content[:200],
            },
        )
        # If session is still active, trigger an immediate planner turn
        # that incorporates the human's input.
        if session.status == PlanningSessionStatus.active:
            context = await self._build_context(session_id)
            bg = asyncio.create_task(
                self._agent_turn(session_id, _PLANNER, PLANNER_DISCUSSION, context)
            )
            self._bg_tasks.add(bg)
            bg.add_done_callback(self._bg_tasks.discard)
        return msg

    async def approve_session(self, session_id: str) -> list[str]:
        """Create real Task rows from all planning task nodes and mark completed.

        Guarded by session status so that:
        * ``completed`` — idempotent: returns the existing task IDs without creating new ones.
        * ``cancelled`` — rejects with ValueError.
        * ``active`` — cancels & awaits the background discussion loop before creating tasks,
          avoiding a race between the loop's own status writes and task creation.
        * ``stable`` — proceeds with task creation as normal.
        """
        session = await repo.get_planning_session(self._db, session_id)
        if session is None:
            raise ValueError("session not found")

        # --- status guard ---------------------------------------------------
        if session.status == PlanningSessionStatus.completed:
            # Idempotent: tasks already created — return their ids without creating more.
            nodes = await repo.list_planning_task_nodes(self._db, session_id)
            return [n.task_id for n in nodes if n.task_id]

        if session.status == PlanningSessionStatus.cancelled:
            raise ValueError("session is cancelled")

        if session.status == PlanningSessionStatus.active:
            # Cancel the background discussion loop first so it cannot race
            # task creation with its own status writes (e.g. marking itself
            # stable in _run_discussion after we've started materialising).
            bg = self._active_sessions.pop(session_id, None)
            if bg is not None:
                bg.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await bg
            # Re-read status — the loop may have already set it to stable
            # before we cancelled it.  If it somehow ended up completed or
            # cancelled during the await we bail out.
            session = await repo.get_planning_session(self._db, session_id)
            if session is None:
                raise ValueError("session not found")
            if session.status == PlanningSessionStatus.completed:
                # Already materialised by a concurrent call — idempotent return.
                nodes = await repo.list_planning_task_nodes(self._db, session_id)
                return [n.task_id for n in nodes if n.task_id]
            if session.status == PlanningSessionStatus.cancelled:
                raise ValueError("session is cancelled")
            # If the loop already set us to stable, proceed; otherwise we
            # transition manually (the loop was cancelled before it could).
            if session.status != PlanningSessionStatus.stable:
                await repo.update_planning_session_status(
                    self._db, session_id, PlanningSessionStatus.stable
                )
                # Re-read so the local variable reflects the new status.
                session = await repo.get_planning_session(self._db, session_id)
                if session is None:
                    raise ValueError("session not found")

        # At this point session.status must be stable (either originally, or
        # we just settled an active session).  Any other unexpected status
        # (e.g. a future status added later) is rejected.
        if session.status != PlanningSessionStatus.stable:
            raise ValueError(
                f"session cannot be approved in status {session.status}"
            )

        # --- task materialisation -------------------------------------------
        nodes = await repo.list_planning_task_nodes(self._db, session_id)

        # Create a parent task representing the session itself
        session_task = await repo.create_task(
            self._db,
            project_id=session.project_id,
            request=f"[Agent-Ception Session]\n{session.prompt[:500]}",
            title=session.title or session.prompt[:80],
            level=0,
            state=TaskState.done,
            origin=TaskOrigin.operator,
        )
        session_parent_id = session_task.id

        # Create tasks for ALL nodes (root and sub-tasks), linking
        # sub-tasks to their parents via the request text.
        created_ids: list[str] = [session_parent_id]
        node_id_to_task_id: dict[str, str] = {}

        # Process in order: roots first, then children (already sorted by level, sort_order)
        for node in nodes:
            await repo.update_planning_task_node(
                self._db, node_id=node.id, status=PlanningNodeStatus.approved
            )

            # Resolve parent task ID for hierarchical linking.
            # Root nodes (no planning parent) become children of the session task.
            # Child nodes link to their planning parent's real task.
            parent_task_id: str | None = session_parent_id
            if node.parent_id and node.parent_id in node_id_to_task_id:
                parent_task_id = node_id_to_task_id[node.parent_id]

            # Build a request that references the parent task if applicable
            request = f"[Planning Session: {session.title}]\n{node.description or node.title}"
            if parent_task_id:
                request += f"\n\nParent task: {parent_task_id}"

            task = await repo.create_task(
                self._db,
                project_id=session.project_id,
                request=request,
                title=node.title,
                level=node.level,
                state=TaskState.inbox,
                origin=TaskOrigin.operator,
                parent_task_id=parent_task_id,
            )
            await repo.update_planning_task_node(
                self._db, node_id=node.id, task_id=task.id
            )
            node_id_to_task_id[node.id] = task.id
            created_ids.append(task.id)

        await repo.update_planning_session_status(
            self._db, session_id, PlanningSessionStatus.completed
        )
        await self._bus.emit(
            type=EventType.planning_tasks_approved,
            project_id=session.project_id,
            planning_session_id=session_id,
            payload={
                "planning_session_id": session_id,
                "task_ids": created_ids,
                "count": len(created_ids),
            },
        )
        await self._bus.emit(
            type=EventType.planning_session_completed,
            project_id=session.project_id,
            planning_session_id=session_id,
            payload={"planning_session_id": session_id},
        )
        return created_ids

    async def shutdown(self) -> None:
        """Cancel and await all active sessions and background tasks.

        Called during daemon shutdown before the database is closed.  Every
        tracked task (discussion loops, pending agent-turn continuations) is
        cancelled and then awaited so no coroutine can touch the DB after
        ``db.close()``.
        """
        for _session_id, task in list(self._active_sessions.items()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._active_sessions.clear()

        for task in list(self._bg_tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._bg_tasks.clear()

    async def cancel_session(self, session_id: str) -> None:
        """Cancel an active or stable session."""
        session = await repo.get_planning_session(self._db, session_id)
        if session is None:
            raise ValueError("session not found")

        await repo.update_planning_session_status(
            self._db, session_id, PlanningSessionStatus.cancelled
        )
        bg = self._active_sessions.pop(session_id, None)
        if bg is not None:
            bg.cancel()
            # Wait for the task to actually unwind so it can't touch the DB after
            # we return (otherwise it races teardown / shutdown).
            with contextlib.suppress(asyncio.CancelledError):
                await bg
        await self._bus.emit(
            type=EventType.planning_session_cancelled,
            project_id=session.project_id,
            planning_session_id=session_id,
            payload={"planning_session_id": session_id},
        )

    # ------------------------------------------------------------------
    # discussion loop
    # ------------------------------------------------------------------

    async def _run_discussion(self, session: PlanningSession) -> None:
        """Background task: orchestrate agent discussion rounds."""
        try:
            await self._bus.emit(
                type=EventType.planning_session_started,
                project_id=session.project_id,
                planning_session_id=session.id,
                payload={"planning_session_id": session.id},
            )

            # Round 0: initial planner breakdown
            await self._agent_turn(
                session.id, _PLANNER, PLANNER_DISCUSSION, session.prompt
            )

            for round_num in range(1, session.max_rounds + 1):
                # Check if session was cancelled during execution
                current = await repo.get_planning_session(self._db, session.id)
                if current is None or current.status == PlanningSessionStatus.cancelled:
                    return

                # Reviewer round: each reviewer critiques the current plan
                approvals: set[str] = set()
                for agent_name, system_prompt in DISCUSSION_AGENTS:
                    if agent_name == _PLANNER:
                        continue  # planner responds after reviewers
                    context = await self._build_context(session.id)
                    result = await self._agent_turn(
                        session.id, agent_name, system_prompt, context
                    )
                    if result and "APPROVED" in result:
                        approvals.add(agent_name)

                # Planner refinement round: incorporate feedback
                context = await self._build_context(session.id)
                planner_result = await self._agent_turn(
                    session.id, _PLANNER, PLANNER_DISCUSSION, context
                )

                # Check if planner signalled readiness
                planner_ready = False
                if planner_result:
                    data = _extract_json_block(planner_result)
                    if data is not None:
                        planner_ready = data.get("ready", False)

                # Session is stable when all mandatory agents approve + planner ready
                if planner_ready and approvals.issuperset(APPROVAL_AGENTS):
                    await repo.update_planning_session_status(
                        self._db, session.id, PlanningSessionStatus.stable
                    )
                    await self._bus.emit(
                        type=EventType.planning_session_stable,
                        project_id=session.project_id,
                        planning_session_id=session.id,
                        payload={
                            "planning_session_id": session.id,
                            "round": round_num,
                        },
                    )
                    return

            # Max rounds reached — mark stable for human review anyway
            await repo.update_planning_session_status(
                self._db, session.id, PlanningSessionStatus.stable
            )
            await self._bus.emit(
                type=EventType.planning_session_stable,
                project_id=session.project_id,
                planning_session_id=session.id,
                payload={
                    "planning_session_id": session.id,
                    "max_rounds_reached": True,
                },
            )

        except asyncio.CancelledError:
            logger.info("discussion cancelled for session %s", session.id)
        except Exception:
            logger.exception("discussion failed for session %s", session.id)
            try:
                await self._bus.emit(
                    type=EventType.planning_error,
                    project_id=session.project_id,
                    planning_session_id=session.id,
                    payload={
                        "planning_session_id": session.id,
                        "error": "Discussion loop crashed — check server logs.",
                    },
                )
            except Exception:
                logger.debug("could not emit planning error event (db closed?)")

    # ------------------------------------------------------------------
    # agent turn
    # ------------------------------------------------------------------

    async def _agent_turn(
        self,
        session_id: str,
        agent_name: str,
        system_prompt: str,
        user_message: str,
    ) -> str | None:
        """Dispatch one agent, store its message, and parse task changes.

        Returns the agent's response text, or None on failure.
        """
        session = await repo.get_planning_session(self._db, session_id)
        if session is None:
            return None

        turn = await repo.increment_planning_turn(self._db, session_id)

        full_prompt = f"{system_prompt}\n\n{user_message}"

        # Use a simple inherited profile (uses the host's logged-in Claude default).
        profile = ResolvedProfile(name="system-default", arg_mode=ArgMode.inherit)
        try:
            result = await self._provider.run_agent(
                profile=profile,
                prompt=full_prompt,
                timeout_seconds=900,  # 15 min/turn — opus-max does deep repo analysis here
            )
        except Exception as exc:
            logger.exception("agent %s failed for session %s", agent_name, session_id)
            result_text = f"[Error dispatching {agent_name}: {exc}]"
        else:
            result_text = result.text if result.ok else f"[Error: {result.error}]"

        # Parse task tree changes from planner output
        tasks_changed = False
        if agent_name == _PLANNER:
            data = _extract_json_block(result_text)
            if data is not None and "task_changes" in data:
                await self._apply_task_changes(session_id, data["task_changes"])
                tasks_changed = True

        # Persist the message
        await repo.add_planning_message(
            self._db,
            session_id=session_id,
            agent=agent_name,
            role="agent",
            content=result_text,
            turn_number=turn,
        )

        # Emit event
        await self._bus.emit(
            type=EventType.planning_agent_turn,
            project_id=session.project_id,
            planning_session_id=session_id,
            agent=agent_name,
            payload={
                "planning_session_id": session_id,
                "agent": agent_name,
                "turn": turn,
                "preview": result_text[:200],
            },
        )
        if tasks_changed:
            await self._bus.emit(
                type=EventType.planning_task_refined,
                project_id=session.project_id,
                planning_session_id=session_id,
                agent=agent_name,
                payload={"planning_session_id": session_id, "turn": turn},
            )

        return result_text

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    async def _build_context(self, session_id: str) -> str:
        """Build the full discussion context for an agent turn."""
        session = await repo.get_planning_session(self._db, session_id)
        if session is None:
            return ""

        messages = await repo.list_planning_messages(self._db, session_id)
        task_nodes = await repo.list_planning_task_nodes(self._db, session_id)

        parts: list[str] = []

        # Feature request
        parts.append(f"## Feature Request\n{session.prompt}\n")

        # Prior discussion (last N messages to stay within context limits)
        if messages:
            parts.append("## Discussion So Far\n")
            for msg in messages[-_MAX_CONTEXT_MSGS:]:
                label = "HUMAN" if msg.role == "human" else msg.agent.upper()
                parts.append(
                    f"**{label}** (turn {msg.turn_number}):\n{msg.content}\n"
                )

        # Current task tree
        if task_nodes:
            parts.append("## Current Task Breakdown\n")
            parts.append(self._render_task_tree(task_nodes))

        parts.append(
            "\nRespond with your analysis. "
            "If you are the planner, include any task tree changes as a JSON block. "
            "Each existing task above is shown as (id=...). To revise one, use "
            '{"action":"update","id":"<that id>",...} or {"action":"remove","id":"<that id>"}; '
            "only use \"add\" for genuinely NEW tasks. Do NOT re-add a task that already exists. "
            "Reviewer agents: end with APPROVED or CHANGES_REQUESTED."
        )
        return "\n".join(parts)

    @staticmethod
    def _render_task_tree(nodes: list[PlanningTaskNode]) -> str:
        """Render the task tree as an indented text representation."""
        by_parent: dict[str | None, list[PlanningTaskNode]] = {}
        for n in nodes:
            by_parent.setdefault(n.parent_id, []).append(n)

        lines: list[str] = []

        def render(pid: str | None, indent: int) -> None:
            children = sorted(
                by_parent.get(pid, []), key=lambda x: (x.sort_order, x.title)
            )
            for n in children:
                status = f"[{n.status}]"
                desc = n.description[:80] + ("..." if len(n.description) > 80 else "")
                lines.append(
                    f"{'  ' * indent}- (id={n.id}) {status} {n.title}: {desc}"
                )
                render(n.id, indent + 1)

        render(None, 0)
        return "\n".join(lines) if lines else "(no tasks yet)"

    async def _apply_task_changes(
        self, session_id: str, changes: list[dict[str, Any]]
    ) -> None:
        """Apply task tree modifications from planner JSON output."""
        # Existing titles (case-insensitive) guard against the planner re-adding a
        # task instead of updating it — keeps the tree from duplicating across rounds.
        existing = await repo.list_planning_task_nodes(self._db, session_id)
        seen_titles = {n.title.strip().lower() for n in existing}
        for change in changes:
            action = change.get("action")
            try:
                if action == "add":
                    title = change.get("title", "Untitled")
                    if title.strip().lower() in seen_titles:
                        logger.info(
                            "skipping duplicate add %r in session %s", title, session_id
                        )
                        continue
                    seen_titles.add(title.strip().lower())
                    parent_id = change.get("parent_id")
                    level = 0
                    if parent_id:
                        parent = await repo.get_planning_task_node(
                            self._db, parent_id
                        )
                        if parent:
                            level = parent.level + 1
                    siblings = await repo.list_planning_task_nodes_by_parent(
                        self._db, session_id, parent_id
                    )
                    node = await repo.add_planning_task_node(
                        self._db,
                        session_id=session_id,
                        parent_id=parent_id,
                        title=change.get("title", "Untitled"),
                        description=change.get("description", ""),
                        level=level,
                        sort_order=len(siblings),
                    )
                    await self._bus.emit(
                        type=EventType.planning_task_proposed,
                        project_id=None,  # filled by caller context
                        planning_session_id=session_id,
                        payload={
                            "planning_session_id": session_id,
                            "node_id": node.id,
                            "title": node.title,
                        },
                    )
                elif action == "update":
                    await repo.update_planning_task_node(
                        self._db,
                        node_id=change["id"],
                        title=change.get("title"),
                        description=change.get("description"),
                    )
                elif action == "remove":
                    await repo.delete_planning_task_node(self._db, change["id"])
            except Exception:
                logger.exception(
                    "failed to apply task change: %s", json.dumps(change)[:200]
                )


# ------------------------------------------------------------------
# module helpers
# ------------------------------------------------------------------


def _extract_json_block(text: str) -> dict[str, Any] | None:
    """Extract the first complete JSON object from *text*.

    Uses :func:`json.JSONDecoder.raw_decode` so nested braces are handled
    correctly, unlike a non-greedy regex which truncates at the first ``}``.
    """
    idx = text.find("{")
    if idx == -1:
        return None
    try:
        decoder = json.JSONDecoder()
        obj, _end = decoder.raw_decode(text, idx)
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict):
        return obj
    return None
