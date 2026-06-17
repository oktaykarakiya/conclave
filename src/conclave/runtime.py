"""Daemon runtime: per-project workers that auto-process approved tasks.

One :class:`ProjectWorker` per active project claims approved tasks and runs them
through the orchestrator. The :class:`Daemon` owns the shared db/bus/provider and the
worker registry, and is reachable from the web layer via ``app.state.daemon``.

Repo context comes from each project's AGENTS.md (read natively by opencode), so there
is no onboarding/analysis step on project creation or startup.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path

from .config import ConclaveConfig, load_project_config, resolve_bug_fixer_session
from .db import Database, Project, ProjectMode
from .db import repositories as repo
from .engine import BugFixerController, Orchestrator, SessionBudget, WorktreeManager
from .events import EventBus
from .planning.session import PlanningOrchestrator
from .providers import Provider

logger = logging.getLogger("conclave.runtime")


class ProjectWorker:
    def __init__(
        self,
        db: Database,
        orchestrator: Orchestrator,
        project_id: str,
        *,
        idle_sleep: float = 2.0,
        max_idle_sleep: float = 30.0,
    ) -> None:
        self._db = db
        self._orchestrator = orchestrator
        self.project_id = project_id
        self._idle_sleep = idle_sleep
        self._max_idle_sleep = max_idle_sleep
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.paused = False
        # Current backoff sleep — grows on consecutive idle iterations and resets
        # to ``_idle_sleep`` whenever work is claimed.
        self._current_idle_sleep = idle_sleep
        # Autonomous bug-fixer mode: the controller that drives one discover→reproduce→fix
        # cycle per tick, plus the session budget (caps + wall-clock) it runs under. Both are
        # lazily built on the first autonomous tick so a task_queue project never pays for them.
        self._bug_fixer = BugFixerController(orchestrator)
        self._session_budget: SessionBudget | None = None
        self._session_started: float | None = None

    async def start(self) -> None:
        await self._orchestrator.recover(self.project_id)
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.paused:
                    await asyncio.sleep(self._idle_sleep)
                    continue
                # Re-read the project each tick so a mode flip (or a config change) is picked up
                # without bouncing the worker. A vanished project just idles until detached.
                project = await repo.get_project(self._db, self.project_id)
                if project is None:
                    await self._idle_backoff()
                    continue
                if project.mode is ProjectMode.autonomous_bug_fixer:
                    await self._bug_fixer_tick(project)
                else:
                    await self._task_queue_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # keep the worker alive on unexpected errors
                logger.exception("worker error for project %s", self.project_id)
                await asyncio.sleep(self._idle_sleep)

    # --- Per-mode ticks ------------------------------------------------------

    async def _task_queue_tick(self) -> None:
        """One task-queue iteration: claim the next approved task and process it, or back off."""
        task = await repo.claim_next_approved(self._db, self.project_id)
        if task is None:
            await self._idle_backoff()
            return
        # Work claimed — reset backoff so the next idle cycle starts from the minimum.
        self._current_idle_sleep = self._idle_sleep
        # Register a cancellation event so the cancel endpoint can signal cooperative
        # cancellation for this specific task.
        cancel_event = asyncio.Event()
        self._orchestrator._cancel_events[task.id] = cancel_event
        try:
            await self._orchestrator.process_task(task, cancel_event=cancel_event)
        finally:
            # Clean the event entry regardless of outcome — the orchestrator's
            # _finish_cancelled also cleans it, but this finally is the belt-and-suspenders
            # so the dict can never leak entries.
            self._orchestrator._cancel_events.pop(task.id, None)

    async def _bug_fixer_tick(self, project: Project) -> None:
        """One autonomous-bug-fixer iteration: run a controller cycle metered by the session budget.

        Resolves the per-session caps + wall-clock budget once (lazily, on the first autonomous
        tick) from the project's :class:`BugFixerPolicy`, then meters every cycle against them.
        Once the budget is exhausted the worker stops starting new cycles and idles — any candidate
        a cycle was mid-flight on is already parked by the controller (a failed/over-budget fix
        defers it), so there is never a candidate stranded in ``fixing`` between ticks. A cycle that
        found nothing (idle) backs off; a cycle that did work resets the backoff.
        """
        config = load_project_config(project.config)
        budget = self._ensure_session(config)

        if budget.exhausted(elapsed_seconds=self._session_elapsed()):
            # Session caps reached — go quiet. (A fresh session begins after a pause/resume or
            # worker restart, which clears the budget.)
            await self._idle_backoff()
            return

        cancel_event = asyncio.Event()
        # Surface a stop request for whatever fix task this cycle spawns: the controller registers
        # the task's own id in the same _cancel_events table, but we also honour a worker stop by
        # signalling between stages through the event threaded into the cycle.
        result = await self._bug_fixer.run_cycle(project, config, cancel_event=cancel_event)
        budget.record(result)

        if result.outcome.did_work:
            # Acted on a candidate — reset backoff so the next sweep starts promptly.
            self._current_idle_sleep = self._idle_sleep
        else:
            # No candidate this turn — nothing to hunt, so back off like an empty task queue.
            await self._idle_backoff()

    def _ensure_session(self, config: ConclaveConfig) -> SessionBudget:
        """Lazily build (and remember) this autonomous session's budget + start time."""
        if self._session_budget is None:
            self._session_budget = SessionBudget.from_config(resolve_bug_fixer_session(config))
            self._session_started = time.monotonic()
        return self._session_budget

    def _session_elapsed(self) -> float:
        """Seconds since the current autonomous session began (0 before one starts)."""
        if self._session_started is None:
            return 0.0
        return time.monotonic() - self._session_started

    # --- Internal helpers ----------------------------------------------------

    async def _idle_backoff(self) -> None:
        """Sleep for the current backoff duration then double it (capped)."""
        await asyncio.sleep(self._current_idle_sleep)
        self._current_idle_sleep = min(
            self._current_idle_sleep * 2, self._max_idle_sleep
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None


class Daemon:
    def __init__(
        self,
        db: Database,
        home: Path,
        provider: Provider,
        *,
        workers_enabled: bool = True,
    ) -> None:
        self.db = db
        self.home = home
        self.provider = provider
        self.bus = EventBus(db)
        self.orchestrator = Orchestrator(db, self.bus, provider, home)
        self.planning_orchestrator = PlanningOrchestrator(db, self.bus, provider)
        self._workers: dict[str, ProjectWorker] = {}
        self._bg_tasks: set[asyncio.Task[None]] = set()
        self._workers_enabled = workers_enabled

    async def start(self) -> None:
        if not self._workers_enabled:
            return
        for project in await repo.list_projects(self.db):
            await self.start_worker(project.id)

    async def shutdown(self) -> None:
        # 1. Stop per-project workers so no new tasks are claimed.
        for worker in list(self._workers.values()):
            await worker.stop()
        self._workers.clear()

        # 2. Cancel and await any backfill / housekeeping background tasks
        #    spawned by start() so they cannot touch the DB after close.
        for task in list(self._bg_tasks):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._bg_tasks.clear()

        # 3. Shut down planning-session discussions and agent-turn continuations.
        await self.planning_orchestrator.shutdown()

    async def start_worker(self, project_id: str) -> None:
        if not self._workers_enabled or project_id in self._workers:
            return
        worker = ProjectWorker(self.db, self.orchestrator, project_id)
        self._workers[project_id] = worker
        await worker.start()

    async def stop_worker(self, project_id: str) -> None:
        worker = self._workers.pop(project_id, None)
        if worker is not None:
            await worker.stop()

    def worker(self, project_id: str) -> ProjectWorker | None:
        return self._workers.get(project_id)

    def set_paused(self, project_id: str, paused: bool) -> bool:
        worker = self._workers.get(project_id)
        if worker is None:
            return False
        worker.paused = paused
        return True

    async def request_cancel(self, task_id: str) -> bool:
        """Request cooperative cancellation for an in-progress *task_id*.

        Returns ``True`` when a cancellation event was set (the task was in-flight).
        Returns ``False`` when the task is not currently being processed, meaning the
        caller should handle non-running states directly (e.g. transition inbox/approved
        to cancelled, or return a no-op for terminal states).
        """
        return self.orchestrator.request_cancel(task_id)

    async def request_steer(self, task_id: str, message: str) -> bool:
        """Queue operator *message* for an in-progress *task_id*'s next dispatch.

        Returns ``True`` when the task is in-flight (the guidance was queued and will be
        injected before its next developer dispatch). Returns ``False`` when the task is
        not currently being processed, so the caller can report that there is no in-flight
        dispatch to steer.
        """
        return self.orchestrator.request_steer(task_id, message)

    async def cleanup_in_progress_work(self, project_id: str) -> None:
        """Cancel in-progress tasks and clean their worktrees for a stopped worker.

        Must be called AFTER :meth:`stop_worker` so no new tasks can be claimed
        by the project's worker between the query and the cleanup. Each in-progress
        task's on-disk worktree is removed (``--force``), then the task is marked
        ``cancelled``. Exceptions during cleanup are caught and logged so one bad
        worktree cannot block project detachment.

        FK ``ON DELETE CASCADE`` handles removing the cancelled rows when the
        project itself is deleted — this method only ensures the disk worktrees
        are gone first.
        """
        from .db.models import TaskState

        project = await repo.get_project(self.db, project_id)
        if project is None:
            return

        in_progress = await repo.get_in_progress_tasks(self.db, project_id)
        if not in_progress:
            return

        wm = WorktreeManager(
            Path(project.path),
            self.home / "projects" / project_id / "worktrees",
        )

        for task in in_progress:
            # Clean the worktree before cancelling.  git worktree remove --force
            # handles locked/incomplete worktrees; if the path doesn't exist at
            # all the call still exits non-zero, so we catch and continue.
            try:
                await wm.cleanup(task.id, task_branch=None)
            except Exception:
                logger.exception(
                    "failed to clean worktree for task %s during project detach; "
                    "continuing", task.id,
                )
            await repo.set_task_state(self.db, task.id, TaskState.cancelled)
