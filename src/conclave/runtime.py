"""Daemon runtime: per-project workers that auto-process approved tasks.

One :class:`ProjectWorker` per active project claims approved tasks and runs them
through the orchestrator. The :class:`Daemon` owns the shared db/bus/provider and the
worker registry, and is reachable from the web layer via ``app.state.daemon``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from .db import Database
from .db import repositories as repo
from .engine import Orchestrator
from .events import EventBus
from .providers import Provider

logger = logging.getLogger("conclave.runtime")


class ProjectWorker:
    def __init__(
        self, db: Database, orchestrator: Orchestrator, project_id: str, *, idle_sleep: float = 2.0
    ) -> None:
        self._db = db
        self._orchestrator = orchestrator
        self.project_id = project_id
        self._idle_sleep = idle_sleep
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.paused = False

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
                    await asyncio.sleep(self._idle_sleep)
                    continue
                await self._orchestrator.process_task(task)
            except asyncio.CancelledError:
                raise
            except Exception:  # keep the worker alive on unexpected errors
                logger.exception("worker error for project %s", self.project_id)
                await asyncio.sleep(self._idle_sleep)

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
        self._workers: dict[str, ProjectWorker] = {}
        self._workers_enabled = workers_enabled

    async def start(self) -> None:
        if not self._workers_enabled:
            return
        for project in await repo.list_projects(self.db):
            await self.start_worker(project.id)

    async def shutdown(self) -> None:
        for worker in list(self._workers.values()):
            await worker.stop()
        self._workers.clear()

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
