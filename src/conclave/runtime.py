"""Daemon runtime: per-project workers that auto-process approved tasks.

One :class:`ProjectWorker` per active project claims approved tasks and runs them
through the orchestrator. The :class:`Daemon` owns the shared db/bus/provider and the
worker registry, and is reachable from the web layer via ``app.state.daemon``.

On startup, projects that were imported before the AI analyser was wired in are
automatically backfilled with an AI enrichment pass.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from .config import load_project_config
from .db import Database
from .db import repositories as repo
from .engine import Orchestrator, WorktreeManager
from .events import EventBus
from .planning.session import PlanningOrchestrator
from .providers import Provider
from .repo_intel import RepoKnowledge, ai_enrich, onboard

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

    async def start(self) -> None:
        await self._orchestrator.recover(self.project_id)
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.paused:
                    await asyncio.sleep(self._idle_sleep)
                    continue
                task = await repo.claim_next_approved(self._db, self.project_id)
                if task is None:
                    await self._idle_backoff()
                    continue
                # Work claimed — reset backoff so the next idle cycle starts
                # from the minimum.
                self._current_idle_sleep = self._idle_sleep
                await self._orchestrator.process_task(task)
            except asyncio.CancelledError:
                raise
            except Exception:  # keep the worker alive on unexpected errors
                logger.exception("worker error for project %s", self.project_id)
                await asyncio.sleep(self._idle_sleep)

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
            # Kick off AI backfill in the background if this project was never
            # AI-enriched (e.g. imported before the feature was wired in).
            ai_row = await repo.latest_ai_knowledge(self.db, project.id)
            if ai_row is None:
                task = asyncio.create_task(self._backfill_ai(project))
                self._bg_tasks.add(task)
                task.add_done_callback(self._bg_tasks.discard)

    async def _backfill_ai(self, project: object) -> None:
        """Run AI enrichment for a project that missed it on import."""
        from .db.models import Project as ProjectModel
        p: ProjectModel = project  # type: ignore[assignment]
        try:
            logger.info("backfill: starting AI enrichment for project %s", p.id)
            current = await repo.current_repo_knowledge(self.db, p.id)
            if current is None:
                logger.warning("backfill: no knowledge at all for %s, skipping", p.id)
                return
            if current.ai_enriched:
                logger.info("backfill: project %s already AI-enriched, skipping", p.id)
                return
            config = load_project_config(p.config)
            heuristic = RepoKnowledge(**current.knowledge)
            await ai_enrich(
                self.db, self.bus, self.provider, p, config,
                heuristic=heuristic,
                sha=current.sha,
                fingerprint=current.manifest_fingerprint or "",
            )
            logger.info("backfill: AI enrichment complete for project %s", p.id)
        except Exception:
            logger.exception("backfill: AI enrichment failed for project %s", p.id)

    async def _onboard_project(self, project: object) -> None:
        """Run onboarding for a newly-created project as a tracked background task.

        This runs after :meth:`create_project` returns so the endpoint is fast.
        An onboarding failure is logged but never fatal — the project remains
        usable (it just won't have repo knowledge yet, which the operator can
        obtain later via the re-onboard endpoint).
        """
        from .db.models import Project as ProjectModel
        p: ProjectModel = project  # type: ignore[assignment]
        try:
            config = load_project_config(p.config)
            await onboard(
                self.db, self.bus, p,
                provider=self.provider, config=config,
            )
            logger.info("onboarding complete for project %s", p.id)
        except Exception:
            logger.exception(
                "onboarding failed for project %s (project is still usable)", p.id
            )

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
