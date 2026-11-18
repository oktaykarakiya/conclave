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
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from .config import (
    ConclaveConfig,
    resolve_bug_fixer_session,
    resolve_project_config,
    save_project_config,
)
from .db import Database, Project, ProjectMode, Task, TaskState
from .db import repositories as repo
from .engine import AgentRunner, BugFixerController, Orchestrator, SessionBudget, WorktreeManager
from .events import EventBus, EventType
from .hot_reload import HotReloader
from .planning.session import PlanningOrchestrator
from .providers import (
    CLAUDE_CODE_PREFIX,
    Engine,
    Provider,
    QuotaGate,
    WaitTracker,
    engine_status,
    resolve_engine,
)
from .util import new_id

logger = logging.getLogger("conclave.runtime")


async def hot_reload_watch_enabled(db: Database) -> bool:
    """Whether the daemon's file watcher may reload on source changes.

    Daemon-wide, so only the global settings count; a project's ``system`` value is ignored.
    """
    return (await resolve_project_config(db, None)).system.hot_reload_watch


async def reload_status_report(daemon: Daemon) -> dict[str, Any]:
    """The hot-reload status plus ``watcher_enabled`` (the global ``system.hot_reload_watch``).

    A module function rather than a :class:`Daemon` method: a hot reload keeps the running
    daemon instance, so the reloaded web and MCP layers must not rely on new methods on it.
    """
    return {**daemon.reload_status(), "watcher_enabled": await hot_reload_watch_enabled(daemon.db)}


def engine_quota_report(daemon: Daemon) -> dict[str, Any]:
    """Every engine account's usage-limit gate (closed or not, until when and why) and the latest
    rate-limit info it reported: the REST and MCP view of the daemon's :class:`QuotaGate`.

    A module function for the same hot-reload reason as :func:`reload_status_report`.
    """
    gate: QuotaGate | None = getattr(daemon, "quota_gate", None)
    return {"gates": gate.snapshot() if gate is not None else []}


# Offered when ui.model_choices is empty, limited to the engines available on this host.
_BUILTIN_MODEL_CHOICES: list[str] = [
    "deepseek/deepseek-flash",  # supersedes deepseek-v4-pro, which is intentionally not offered
    "google/gemini-2.5-flash-lite",
    "google/gemini-3.1-flash-lite-preview",
    "google/gemini-2.5-flash",
    "google/gemini-2.5-pro",
    "google/gemini-3-flash-preview",
    "google/gemini-3.1-pro-preview",
    "deepseek/deepseek-chat",
    "deepseek/deepseek-reasoner",
    "anthropic/claude-sonnet-4-5",
    # Claude Code CLI engine (its own login/subscription), routed by the prefix.
    f"{CLAUDE_CODE_PREFIX}default",
    f"{CLAUDE_CODE_PREFIX}opus",
    f"{CLAUDE_CODE_PREFIX}sonnet",
    f"{CLAUDE_CODE_PREFIX}haiku",
]
_EFFORT_VARIANTS: list[str] = ["low", "medium", "high", "xhigh", "max"]
_CLI_AGENTS: list[str] = ["build", "general", "explore", "plan"]
_SKILL_TEMPLATES: list[str] = [
    "tdd-strict",
    "minimal-diff",
    "security-hardening",
    "async-safety",
    "type-strict",
    "zero-regression",
]


def _configured_models(config: ConclaveConfig) -> list[str]:
    """Every model string *config* dispatches with: agent defaults, overrides, workflow nodes."""
    found: list[str | None] = [config.agent_defaults.model]
    for override in config.agent_overrides.values():
        model = override.get("model")
        found.append(model if isinstance(model, str) else None)
    if config.workflow is not None:
        found.extend(node.model for node in config.workflow.nodes)
    return [m.strip() for m in found if m and m.strip()]


async def models_catalog(daemon: Daemon, project: Project | None) -> dict[str, Any]:
    """The model pickers' catalog, from the resolved config (*project*'s, else the global one):
    the REST (``GET /api/models``) and MCP (``conclave_list_models``) view.

    ``ui.model_choices`` is offered when set, else the built-in list for the engines this host
    has; every model the config already uses is added so a picker never hides one. A module
    function for the same hot-reload reason as :func:`reload_status_report`.
    """
    config = await resolve_project_config(daemon.db, project)
    engines = engine_status(daemon.provider)
    offered = list(config.ui.model_choices) or [
        model
        for model in _BUILTIN_MODEL_CHOICES
        if engines["available"].get(str(resolve_engine(model, Engine.opencode)[0]), False)
    ]
    models = list(dict.fromkeys([*offered, *_configured_models(config)]))
    return {
        "default_model": config.agent_defaults.model,
        "models": models,
        "recommended_models": list(models),
        "efforts": list(_EFFORT_VARIANTS),
        "effort_variants": list(_EFFORT_VARIANTS),
        "cli_agents": list(_CLI_AGENTS),
        "skills": list(_SKILL_TEMPLATES),
        "skill_templates": list(_SKILL_TEMPLATES),
        "engines": engines,
    }


def hitl_report(daemon: Daemon, task_id: str) -> dict[str, Any]:
    """*task_id*'s human-in-the-loop state: ``armed`` (the node and edge ids with a pause armed)
    and ``waiting`` (the ids it is paused on now). The REST (``GET /api/tasks/{id}/hitl``) and
    MCP (``conclave_get_hitl_state``) view; a module function like :func:`reload_status_report`.
    """
    return {"task_id": task_id, **daemon.get_hitl_state(task_id)}


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
        # How long the session's dispatches waited for a usage limit: not part of its budget.
        self._session_waits: WaitTracker | None = None
        self._active_tasks: set[asyncio.Task[None]] = set()
        self._reloader: HotReloader | None = getattr(orchestrator, "_reloader", None)
        self._daemon: Daemon | None = getattr(orchestrator, "_daemon", None)

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
                if project.mode == ProjectMode.autonomous_bug_fixer:
                    await self._bug_fixer_tick(project)
                else:
                    config = await resolve_project_config(self._db, project)
                    max_parallel = config.execution.max_parallel_tasks
                    if max_parallel > 1:
                        await self._swarm_queue_tick(max_parallel)
                    else:
                        await self._task_queue_tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # keep the worker alive on unexpected errors
                logger.exception("worker error for project %s", self.project_id)
                await asyncio.sleep(self._idle_sleep)

    # --- Per-mode ticks ------------------------------------------------------

    async def _run_claimed_task(self, task: Task) -> None:
        """Run a single claimed task with full cancellation and backstop finalization."""
        cancel_event = asyncio.Event()
        self._orchestrator._cancel_events[task.id] = cancel_event
        try:
            await self._orchestrator.process_task(task, cancel_event=cancel_event)
        except Exception:
            logger.exception("process_task escaped for task %s; marking failed", task.id)
            with contextlib.suppress(Exception):
                current = await repo.get_task(self._db, task.id)
                if current is not None and current.state == TaskState.in_progress:
                    await repo.finalize_task(
                        self._db,
                        task.id,
                        state=TaskState.failed,
                        result_summary="worker backstop: unhandled exception during "
                        "processing (see daemon logs)",
                        block_children=True,
                    )
        finally:
            self._orchestrator._cancel_events.pop(task.id, None)
            reloader = self._reloader or getattr(self._orchestrator, "_reloader", None)
            daemon_ref = self._daemon or getattr(self._orchestrator, "_daemon", None)
            if reloader is not None and daemon_ref is not None and reloader.reload_pending:
                with contextlib.suppress(Exception):
                    await reloader.on_task_complete(daemon_ref)

    async def _task_queue_tick(self) -> None:
        """One task-queue iteration: claim the next approved task and process it, or back off."""
        task = await repo.claim_next_approved(self._db, self.project_id)
        if task is None:
            await self._idle_backoff()
            return
        self._current_idle_sleep = self._idle_sleep
        await self._run_claimed_task(task)

    async def _swarm_queue_tick(self, max_parallel: int) -> None:
        """Parallel Swarm iteration: claim up to *max_parallel* tasks into isolated worktrees."""
        claimed_any = False
        while len(self._active_tasks) < max_parallel and not self._stop.is_set():
            task = await repo.claim_next_approved(
                self._db, self.project_id, max_parallel=max_parallel
            )
            if task is None:
                break
            claimed_any = True
            self._current_idle_sleep = self._idle_sleep
            bg = asyncio.create_task(self._run_claimed_task(task))
            self._active_tasks.add(bg)
            bg.add_done_callback(self._active_tasks.discard)

        if self._active_tasks:
            await asyncio.wait(
                self._active_tasks,
                timeout=0.25,
                return_when=asyncio.FIRST_COMPLETED,
            )
        elif not claimed_any:
            await self._idle_backoff()

    async def _bug_fixer_tick(self, project: Project) -> None:
        """One autonomous-bug-fixer iteration: run a controller cycle metered by the session budget.

        Resolves the per-session caps + wall-clock budget once (lazily, on the first autonomous
        tick) from the project's :class:`BugFixerPolicy`, then meters every cycle against them.
        Once the budget is exhausted the worker stops starting new cycles and idles — any candidate
        a cycle was mid-flight on is already parked by the controller (a failed/over-budget fix
        defers it), so there is never a candidate stranded in ``fixing`` between ticks. A cycle that
        found nothing (idle) backs off; a cycle that did work resets the backoff.
        """
        config = await resolve_project_config(self._db, project)
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
        result = await self._bug_fixer.run_cycle(
            project, config, cancel_event=cancel_event, waits=self._session_waits
        )
        budget.record(result)

        if result.outcome.did_work:
            # Acted on a candidate — reset backoff so the next sweep starts promptly.
            self._current_idle_sleep = self._idle_sleep
        else:
            # No candidate this turn — nothing to hunt, so back off like an empty task queue.
            await self._idle_backoff()

    def _ensure_session(self, config: ConclaveConfig) -> SessionBudget:
        """Lazily build (and remember) this autonomous session's budget, start time and waits."""
        if self._session_budget is None:
            self._session_budget = SessionBudget.from_config(resolve_bug_fixer_session(config))
            self._session_started = time.monotonic()
            self._session_waits = WaitTracker(self._orchestrator._quota_gate.now)
        return self._session_budget

    def _session_elapsed(self) -> float:
        """Seconds since the current autonomous session began (0 before one starts), leaving out
        the time its dispatches waited for a usage limit to reset: a wait never uses the budget."""
        if self._session_started is None:
            return 0.0
        waited = self._session_waits.seconds() if self._session_waits is not None else 0.0
        return time.monotonic() - self._session_started - waited

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
        for active in list(self._active_tasks):
            active.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active
        self._active_tasks.clear()


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
        self.reloader = HotReloader()
        self.app: Any = None
        # One usage-limit gate for every dispatch of every project: a limit hit on an engine
        # account holds back all of its dispatches until the limit resets.
        self.quota_gate = QuotaGate()
        self.orchestrator = Orchestrator(db, self.bus, provider, home, quota_gate=self.quota_gate)
        self.orchestrator._reloader = self.reloader
        self.orchestrator._daemon = self
        self.planning_orchestrator = PlanningOrchestrator(
            db, self.bus, provider, quota_gate=self.quota_gate
        )
        self._workers: dict[str, ProjectWorker] = {}
        self._bg_tasks: set[asyncio.Task[None]] = set()
        self._workers_enabled = workers_enabled

    def is_quiescent(self) -> bool:
        """Return ``True`` when no task or planning turn is in-flight."""
        return self.reloader.is_quiescent(self)

    def request_reload(self, *, reason: str = "requested") -> dict[str, Any]:
        """Mark a system hot-reload as pending until the daemon is quiescent."""
        return self.reloader.request_reload(self, reason=reason)

    def reload_status(self) -> dict[str, Any]:
        """Return current hot-reload status and module coverage."""
        return self.reloader.status(self)

    async def reload_now(self, *, force: bool = False) -> dict[str, Any]:
        """Execute or defer a quiescent hot-reload across all Conclave modules."""
        return await self.reloader.reload_now(self, app=self.app, force=force)

    async def start(self) -> None:
        if not self._workers_enabled:
            return
        for project in await repo.list_projects(self.db):
            await self.start_worker(project.id)
        if "PYTEST_CURRENT_TEST" not in os.environ:
            watch_task = asyncio.create_task(self._watch_reload_loop())
            self._bg_tasks.add(watch_task)
            watch_task.add_done_callback(self._bg_tasks.discard)

    async def _watch_reload_loop(self) -> None:
        """Background loop: run a pending reload, and watch the source files when enabled."""
        while True:
            try:
                await asyncio.sleep(3.0)
                await self._watch_reload_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("hot-reload watcher error", exc_info=True)

    async def _watch_reload_tick(self) -> None:
        """Run a pending reload once quiescent, and reload on changed source files only while
        the global ``system.hot_reload_watch`` setting is on (read on every tick)."""
        if not self.reloader.reload_pending and not (
            await hot_reload_watch_enabled(self.db) and self.reloader.changed_files()
        ):
            return
        if self.is_quiescent():
            await self.reload_now()
        else:
            self.reloader.reload_pending = True

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

    def is_paused(self, project_id: str) -> bool:
        worker = self._workers.get(project_id)
        return worker.paused if worker is not None else False

    async def request_cancel(self, task_id: str) -> bool:
        """Request cooperative cancellation for an in-progress *task_id*.

        Returns ``True`` when a cancellation event was set (the task was in-flight).
        Returns ``False`` when the task is not currently being processed, meaning the
        caller should handle non-running states directly (e.g. transition inbox/approved
        to cancelled, or return a no-op for terminal states).
        """
        return self.orchestrator.request_cancel(task_id)

    async def request_steer(
        self,
        task_id: str,
        message: str,
        *,
        node_id: str | None = None,
        edge_id: str | None = None,
    ) -> bool:
        """Queue operator *message* for an in-progress *task_id*'s next dispatch."""
        return self.orchestrator.request_steer(
            task_id, message, node_id=node_id, edge_id=edge_id
        )

    def resolve_node(
        self,
        task_id: str,
        node_id: str,
        *,
        status: str = "completed",
        message: str | None = None,
        verdict: str | None = None,
        output: str | None = None,
        edge_id: str | None = None,
    ) -> bool:
        """Approve/complete or override a node or HITL connector on *task_id*."""
        return self.orchestrator.resolve_node(
            task_id,
            node_id,
            status=status,
            message=message,
            verdict=verdict,
            output=output,
            edge_id=edge_id,
        )

    def set_hitl_checkpoint(self, task_id: str, target_id: str, enabled: bool) -> list[str]:
        """Dynamically arm or disarm a HITL pause on *target_id* (node_id or edge_id)."""
        return self.orchestrator.set_hitl_checkpoint(task_id, target_id, enabled)

    def get_hitl_state(self, task_id: str) -> dict[str, list[str]]:
        """Return currently armed HITL ids and actively waiting node/edge ids for *task_id*."""
        return self.orchestrator.get_hitl_state(task_id)

    async def launch_swarm(
        self,
        project_id: str,
        *,
        units: list[dict[str, Any]] | None = None,
        prompt: str | None = None,
        concurrency: int = 4,
        auto_approve: bool = True,
    ) -> dict[str, Any]:
        """Launch a dedicated parallel Swarm of worktree sessions for mass changes.

        Supports either an explicit list of work *units* (each with ``title`` and ``request``)
        or a high-level mass-change *prompt* that the planner decomposes into independent work
        units. Configures ``execution.max_parallel_tasks`` to at least *concurrency* and stamps
        all created tasks with a shared ``swarm_id``.
        """
        project = await repo.get_project(self.db, project_id)
        if project is None:
            raise ValueError(f"project not found: {project_id}")

        effective_concurrency = max(1, min(int(concurrency), 16))
        config = await resolve_project_config(self.db, project)
        if effective_concurrency > config.execution.max_parallel_tasks:
            config = await save_project_config(
                self.db, project, {"execution": {"max_parallel_tasks": effective_concurrency}}
            )

        resolved_units: list[dict[str, Any]] = list(units or [])
        if not resolved_units and prompt and prompt.strip():
            runner = AgentRunner(
                self.db, self.bus, self.provider, project_id, config, quota_gate=self.quota_gate
            )
            decompose_prompt = (
                f"Break this mass-change objective into {effective_concurrency} independent, "
                "non-overlapping work units so a swarm of dedicated agents can execute them in "
                "parallel worktrees without file conflicts.\n\n"
                f"OBJECTIVE:\n{prompt.strip()}\n\n"
                'Respond with a fenced ```json block containing {"units": [{"title": "...", '
                '"request": "..."}]}'
            )
            res = await runner.run(
                agent="planner",
                prompt=decompose_prompt,
                worktree=Path(project.path),
            )
            if res.ok and res.text:
                m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", res.text, re.DOTALL)
                if m:
                    with contextlib.suppress(Exception):
                        parsed = json.loads(m.group(1))
                        if isinstance(parsed, dict) and isinstance(parsed.get("units"), list):
                            resolved_units = [
                                u for u in parsed["units"] if isinstance(u, dict)
                            ]
                        elif (
                            isinstance(parsed, dict)
                            and isinstance(parsed.get("files_to_touch"), list)
                        ):
                            resolved_units = [
                                {
                                    "title": f"Swarm unit: {f}",
                                    "request": f"{prompt.strip()}\nFocus exclusively on `{f}`.",
                                }
                                for f in parsed["files_to_touch"]
                                if isinstance(f, str)
                            ]
            if not resolved_units:
                # Fallback: split multiline bullet/numbered items or create dedicated units
                lines = [
                    ln.lstrip("-*0123456789.) ").strip()
                    for ln in prompt.splitlines()
                    if ln.strip()
                ]
                if len(lines) > 1:
                    resolved_units = [
                        {"title": ln[:80], "request": ln} for ln in lines
                    ]
                else:
                    resolved_units = [
                        {"title": prompt.strip()[:80], "request": prompt.strip()}
                    ]

        if not resolved_units:
            raise ValueError("either non-empty units or prompt is required to launch a swarm")

        swarm_id = f"swarm-{new_id()[:12]}"
        task_state = TaskState.approved if auto_approve else TaskState.inbox
        created: list[Task] = []
        for idx, unit in enumerate(resolved_units, start=1):
            req_text = str(unit.get("request") or unit.get("title") or "").strip()
            if not req_text:
                continue
            title = str(unit.get("title") or f"Swarm #{idx}: {req_text[:50]}").strip()
            use_planner_raw = unit.get("use_planner")
            use_planner = bool(use_planner_raw) if isinstance(use_planner_raw, bool) else False
            task = await repo.create_task(
                self.db,
                project_id=project_id,
                title=title,
                request=req_text,
                use_planner=use_planner,
                state=task_state,
                swarm_id=swarm_id,
            )
            await self.bus.emit(
                type=EventType.task_created,
                project_id=project_id,
                task_id=task.id,
                payload={
                    "title": task.title,
                    "auto_approve": auto_approve,
                    "swarm_id": swarm_id,
                },
            )
            created.append(task)

        return {
            "swarm_id": swarm_id,
            "project_id": project_id,
            "concurrency": effective_concurrency,
            "count": len(created),
            "tasks": [t.model_dump(mode="json") for t in created],
        }

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
