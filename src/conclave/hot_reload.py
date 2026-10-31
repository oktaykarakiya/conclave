"""100% in-process quiescent hot-reload for self-hosted Conclave."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
import os
import sys
import time
from collections.abc import MutableMapping
from enum import Enum
from pathlib import Path
from types import ModuleType
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

logger = logging.getLogger("conclave.hot_reload")

_PACKAGE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PACKAGE_DIR.parent.parent

# Explicit topological rank within subpackages so dependencies always reload before dependents.
_EXPLICIT_MODULE_RANKS: dict[str, tuple[int, int]] = {
    "conclave": (0, 0),
    "conclave.util": (1, 0),
    # events
    "conclave.events.types": (2, 0),
    "conclave.events.bus": (2, 1),
    "conclave.events.notifications": (2, 2),
    "conclave.events": (2, 99),
    # config
    "conclave.config.graph": (3, 0),
    "conclave.config.models": (3, 1),
    "conclave.config.resolver": (3, 2),
    "conclave.config.schema": (3, 3),
    "conclave.config": (3, 99),
    # agents
    "conclave.agents.personas": (4, 0),
    "conclave.agents": (4, 99),
    # db
    "conclave.db.migrations": (5, 0),
    "conclave.db.models": (5, 1),
    "conclave.db.planning_models": (5, 2),
    "conclave.db.database": (5, 3),
    "conclave.db.repositories": (5, 4),
    "conclave.db": (5, 99),
    # verification
    "conclave.verification.quarantine": (6, 0),
    "conclave.verification": (6, 99),
    # observability
    "conclave.observability.parquet_sync": (7, 0),
    "conclave.observability": (7, 99),
    # providers
    "conclave.providers.base": (8, 0),
    "conclave.providers.profiles": (8, 1),
    "conclave.providers.claude_cli": (8, 2),
    "conclave.providers.opencode_cli": (8, 3),
    "conclave.providers.router": (8, 4),
    "conclave.providers": (8, 99),
    # bootstrap
    "conclave.bootstrap": (9, 0),
    # engine
    "conclave.engine.baseline": (10, 0),
    "conclave.engine.gitio": (10, 1),
    "conclave.engine.gate": (10, 2),
    "conclave.engine.test_integrity": (10, 3),
    "conclave.engine.verdict": (10, 4),
    "conclave.engine.memory": (10, 5),
    "conclave.engine.worktree": (10, 6),
    "conclave.engine.pipeline": (10, 7),
    "conclave.engine.graph_executor": (10, 8),
    "conclave.engine.workflow_evolver": (10, 9),
    "conclave.engine.compactor": (10, 10),
    "conclave.engine.runner": (10, 11),
    "conclave.engine.hunter": (10, 12),
    "conclave.engine.repro": (10, 13),
    "conclave.engine.repro_gate": (10, 14),
    "conclave.engine.discovery": (10, 15),
    "conclave.engine.coverage_ingest": (10, 16),
    "conclave.engine.region_scheduler": (10, 17),
    "conclave.engine.orchestrator": (10, 80),
    "conclave.engine.task_diff": (10, 82),
    "conclave.engine.task_merge": (10, 83),
    "conclave.engine.bug_fixer": (10, 85),
    "conclave.engine": (10, 99),
    # planning
    "conclave.planning.prompts": (11, 0),
    "conclave.planning.session": (11, 1),
    "conclave.planning": (11, 99),
    # hot_reload + runtime
    "conclave.hot_reload": (12, 0),
    "conclave.runtime": (13, 0),
    # mcp
    "conclave.mcp.tools": (14, 0),
    "conclave.mcp.server": (14, 1),
    "conclave.mcp": (14, 98),
    "conclave.mcp.__main__": (14, 99),
    # web
    "conclave.web.deps": (15, 0),
    "conclave.web.schemas": (15, 1),
    "conclave.web.ws": (15, 2),
    "conclave.web.mcp_routes": (15, 3),
    "conclave.web.api": (15, 4),
    "conclave.web.app": (15, 5),
    "conclave.web": (15, 99),
    # main entrypoint
    "conclave.main": (16, 0),
}

_LAYER_PREFIX_RANKS: list[tuple[str, int]] = [
    ("conclave.events.", 2),
    ("conclave.config.", 3),
    ("conclave.agents.", 4),
    ("conclave.db.migrations.", 5),
    ("conclave.db.", 5),
    ("conclave.verification.", 6),
    ("conclave.observability.", 7),
    ("conclave.providers.", 8),
    ("conclave.engine.", 10),
    ("conclave.planning.", 11),
    ("conclave.mcp.", 14),
    ("conclave.web.", 15),
]


def py_file_to_module_name(py_file: Path, package_dir: Path = _PACKAGE_DIR) -> str:
    """Convert a ``.py`` path inside ``src/conclave/`` into its dotted module name."""
    rel = py_file.resolve().relative_to(package_dir.resolve())
    if rel.parts == ("__init__.py",):
        return "conclave"
    if rel.name == "__init__.py":
        return "conclave." + ".".join(rel.parts[:-1])
    return "conclave." + ".".join((*rel.parts[:-1], rel.stem))


def _module_sort_key(mod_name: str) -> tuple[int, int, str]:
    if mod_name in _EXPLICIT_MODULE_RANKS:
        major, minor = _EXPLICIT_MODULE_RANKS[mod_name]
        return (major, minor, mod_name)
    for prefix, major in _LAYER_PREFIX_RANKS:
        if mod_name.startswith(prefix):
            return (major, 50, mod_name)
    return (11, 50, mod_name)


def discover_conclave_modules(package_dir: Path | None = None) -> list[str]:
    """Dynamically walk ``src/conclave/**/*.py`` and return all ``conclave.*`` modules
    sorted in bottom-up topological dependency order."""
    pkg = (package_dir or _PACKAGE_DIR).resolve()
    discovered: set[str] = set()
    for py_path in pkg.rglob("*.py"):
        if "__pycache__" in py_path.parts:
            continue
        discovered.add(py_file_to_module_name(py_path, pkg))
    return sorted(discovered, key=_module_sort_key)


def is_quiescent(daemon: Any) -> bool:
    """Return ``True`` when no task or planning turn is actively running."""
    orch = getattr(daemon, "orchestrator", None)
    if orch is not None:
        cancel_events = getattr(orch, "_cancel_events", {})
        if len(cancel_events) > 0:
            return False
    planning = getattr(daemon, "planning_orchestrator", None)
    if planning is not None:
        active_sessions = getattr(planning, "_active_sessions", {})
        for task in list(active_sessions.values()):
            if task is not None and not task.done():
                return False
        bg_tasks: set[Any] = getattr(planning, "_bg_tasks", set())
        for task in list(bg_tasks):
            if task is not None and not task.done():
                return False
    return True


class _NoCacheSpaStaticFiles(StaticFiles):
    """StaticFiles wrapper ensuring SPA HTML responses carry ``Cache-Control: no-cache``."""

    async def get_response(self, path: str, scope: MutableMapping[str, Any]) -> Any:
        response = await super().get_response(path, scope)
        if path in ("", ".", "index.html") or path.endswith(".html"):
            response.headers["Cache-Control"] = "no-cache"
        return response


def mount_spa_routes(app: FastAPI, static_dir: Path, placeholder_html: str) -> None:
    """Mount the built SPA (or placeholder) with ``Cache-Control: no-cache`` on HTML."""
    if (static_dir / "index.html").is_file():
        app.mount(
            "/",
            _NoCacheSpaStaticFiles(directory=str(static_dir), html=True),
            name="spa",
        )
    else:

        @app.get("/", response_class=HTMLResponse)
        async def root() -> HTMLResponse:
            return HTMLResponse(
                content=placeholder_html,
                headers={"Cache-Control": "no-cache"},
            )


def refresh_fastapi_routes(app: FastAPI) -> int:
    """Refresh API, MCP, WebSocket, and SPA routes in-place on a live :class:`FastAPI` app."""
    from .web import api as web_api
    from .web import app as web_app
    from .web import mcp_routes as web_mcp_routes
    from .web import ws as web_ws

    doc_paths = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    kept_routes = [
        r for r in app.router.routes if getattr(r, "path", None) in doc_paths
    ]
    app.router.routes[:] = kept_routes
    app.include_router(web_api.router)
    app.include_router(web_mcp_routes.router)
    app.include_router(web_ws.router)

    static_dir = Path(web_app.__file__).resolve().parent / "static"
    mount_spa_routes(app, static_dir, web_app._PLACEHOLDER)
    app.openapi_schema = None
    return len(app.router.routes)


def _remap_value(val: Any, rebind_map: dict[int, tuple[Any, Any]]) -> tuple[Any, bool]:
    """Recursively remap any old class/function/enum-member reference inside *val*."""
    entry = rebind_map.get(id(val))
    if entry is not None and val is entry[0]:
        return entry[1], True
    if isinstance(val, tuple):
        changed = False
        items = []
        for item in val:
            new_item, item_changed = _remap_value(item, rebind_map)
            items.append(new_item)
            if item_changed:
                changed = True
        return (tuple(items) if changed else val), changed
    if isinstance(val, list):
        changed = False
        items = []
        for item in val:
            new_item, item_changed = _remap_value(item, rebind_map)
            items.append(new_item)
            if item_changed:
                changed = True
        if changed:
            val[:] = items
        return val, changed
    if isinstance(val, frozenset):
        changed = False
        items = []
        for item in val:
            new_item, item_changed = _remap_value(item, rebind_map)
            items.append(new_item)
            if item_changed:
                changed = True
        return (frozenset(items) if changed else val), changed
    return val, False


def _preserve_enum_and_exception_identities(
    mod: ModuleType,
    old_dict: dict[str, Any],
    rebind_map: dict[int, tuple[Any, Any]],
) -> None:
    """Keep Exception class identities stable across reloads, and record changed symbol
    and Enum member identities in *rebind_map* for cross-module rebinding."""
    for attr_name, old_val in old_dict.items():
        if attr_name.startswith("__"):
            continue
        new_val = mod.__dict__.get(attr_name)
        if new_val is None or new_val is old_val:
            continue
        if (
            isinstance(old_val, type)
            and isinstance(new_val, type)
            and issubclass(old_val, BaseException)
            and issubclass(new_val, BaseException)
        ):
            setattr(mod, attr_name, old_val)
            continue
        if (
            isinstance(old_val, type)
            and isinstance(new_val, type)
            and issubclass(old_val, Enum)
            and issubclass(new_val, Enum)
        ):
            rebind_map[id(old_val)] = (old_val, new_val)
            for m_name, old_member in old_val.__members__.items():
                if m_name in new_val.__members__:
                    rebind_map[id(old_member)] = (old_member, new_val[m_name])
            continue
        if isinstance(old_val, Enum) and isinstance(new_val, Enum):
            rebind_map[id(old_val)] = (old_val, new_val)
            continue
        if isinstance(old_val, (dict, tuple, list, frozenset)):
            rebind_map[id(old_val)] = (old_val, new_val)
            continue
        if isinstance(old_val, (type, object)) and callable(old_val):
            rebind_map[id(old_val)] = (old_val, new_val)


class HotReloader:
    """Manages file snapshotting, quiescent deferral, and in-process system hot-reload."""

    def __init__(self, repo_root: Path | None = None) -> None:
        self.repo_root = (repo_root or _REPO_ROOT).resolve()
        self.package_dir = (self.repo_root / "src" / "conclave").resolve()
        if not self.package_dir.is_dir():
            self.package_dir = _PACKAGE_DIR
        self.reload_count: int = 0
        self.reload_pending: bool = False
        self.pending_reason: str | None = None
        self.last_reload_at: float | None = None
        self.last_result: dict[str, Any] | None = None
        self._lock = asyncio.Lock()
        self._snapshot: dict[str, tuple[int, int]] = self.snapshot_files()

    def _watched_paths(self) -> list[Path]:
        paths: list[Path] = []
        if self.package_dir.is_dir():
            for p in self.package_dir.rglob("*.py"):
                if "__pycache__" not in p.parts:
                    paths.append(p)
        pyproject = self.repo_root / "pyproject.toml"
        if pyproject.is_file():
            paths.append(pyproject)
        frontend_dir = self.repo_root / "frontend"
        if (frontend_dir / "package.json").is_file():
            paths.append(frontend_dir / "package.json")
        frontend_src = frontend_dir / "src"
        if frontend_src.is_dir():
            for p in frontend_src.rglob("*"):
                if p.is_file():
                    paths.append(p)
        static_dir = self.package_dir / "web" / "static"
        if static_dir.is_dir():
            for p in static_dir.rglob("*"):
                if p.is_file():
                    paths.append(p)
        return paths

    def snapshot_files(self) -> dict[str, tuple[int, int]]:
        """Capture ``(mtime_ns, size)`` across backend, config, and frontend assets."""
        snap: dict[str, tuple[int, int]] = {}
        for p in self._watched_paths():
            with contextlib.suppress(OSError):
                st = p.stat()
                try:
                    key = str(p.relative_to(self.repo_root)).replace("\\", "/")
                except ValueError:
                    key = str(p)
                snap[key] = (st.st_mtime_ns, st.st_size)
        return snap

    def changed_files(self) -> list[str]:
        """Return relative paths of files modified, added, or removed since last snapshot."""
        current = self.snapshot_files()
        changed: list[str] = []
        all_keys = sorted(set(self._snapshot) | set(current))
        for k in all_keys:
            if self._snapshot.get(k) != current.get(k):
                changed.append(k)
        return changed

    def frontend_needs_rebuild(self) -> bool:
        """Return ``True`` when ``frontend/src/**/*`` is newer than ``web/static/index.html``."""
        frontend_src = self.repo_root / "frontend" / "src"
        if not frontend_src.is_dir():
            return False
        index_html = self.package_dir / "web" / "static" / "index.html"
        if not index_html.is_file():
            return True
        with contextlib.suppress(OSError):
            index_mtime = index_html.stat().st_mtime_ns
            pkg_json = self.repo_root / "frontend" / "package.json"
            if pkg_json.is_file() and pkg_json.stat().st_mtime_ns > index_mtime:
                return True
            for p in frontend_src.rglob("*"):
                if p.is_file() and p.stat().st_mtime_ns > index_mtime:
                    return True
        return False

    def is_quiescent(self, daemon: Any) -> bool:
        """Return ``True`` when *daemon* has zero in-flight tasks or planning turns."""
        return is_quiescent(daemon)

    def request_reload(
        self, daemon: Any = None, *, reason: str = "requested"
    ) -> dict[str, Any]:
        """Mark a reload as pending so it executes as soon as the daemon is quiescent."""
        self.reload_pending = True
        self.pending_reason = reason
        quiescent = is_quiescent(daemon) if daemon is not None else True
        return {
            "reload_pending": self.reload_pending,
            "reason": self.pending_reason,
            "quiescent": quiescent,
            "reload_count": self.reload_count,
        }

    def status(self, daemon: Any = None) -> dict[str, Any]:
        """Return current hot-reload state and module coverage metadata."""
        from . import __version__

        mods = discover_conclave_modules(self.package_dir)
        quiescent = is_quiescent(daemon) if daemon is not None else True
        return {
            "reload_count": self.reload_count,
            "reload_pending": self.reload_pending,
            "pending_reason": self.pending_reason,
            "quiescent": quiescent,
            "changed_files": self.changed_files(),
            "frontend_stale": self.frontend_needs_rebuild(),
            "discoverable_modules": len(mods),
            "modules": mods,
            "last_reload_at": self.last_reload_at,
            "version": __version__,
        }

    async def on_task_complete(
        self, daemon: Any, app: FastAPI | None = None
    ) -> dict[str, Any] | None:
        """Execute a pending reload automatically once the last in-flight task finishes."""
        if not self.reload_pending:
            return None
        if not is_quiescent(daemon):
            return None
        return await self.reload_now(
            daemon,
            app=app or getattr(daemon, "app", None),
            force=False,
        )

    async def _maybe_rebuild_frontend(self) -> bool:
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return False
        if not self.frontend_needs_rebuild():
            return False
        frontend_dir = self.repo_root / "frontend"
        if not (frontend_dir / "package.json").is_file():
            return False
        from .engine.gitio import run_shell

        rc, out = await run_shell(frontend_dir, "npm run build", timeout_seconds=180)
        if rc != 0:
            logger.warning("frontend rebuild during hot-reload exited %d: %s", rc, out[-500:])
            return False
        return True

    async def reload_now(
        self,
        daemon: Any,
        app: FastAPI | None = None,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Reload all Conclave modules, migrations, defaults, runtime workers, and routes.

        If ``force`` is ``False`` and *daemon* is not quiescent, sets ``reload_pending = True``
        and defers execution until the in-flight task completes.
        """
        from . import __version__

        if not force and not is_quiescent(daemon):
            self.reload_pending = True
            if not self.pending_reason:
                self.pending_reason = "deferred_in_flight_task"
            return {
                "reloaded": False,
                "deferred": True,
                "reload_pending": True,
                "quiescent": False,
                "reload_count": self.reload_count,
                "modules_reloaded": 0,
                "frontend_rebuilt": False,
                "version": __version__,
            }

        async with self._lock:
            if not force and not is_quiescent(daemon):
                self.reload_pending = True
                return {
                    "reloaded": False,
                    "deferred": True,
                    "reload_pending": True,
                    "quiescent": False,
                    "reload_count": self.reload_count,
                    "modules_reloaded": 0,
                    "frontend_rebuilt": False,
                    "version": __version__,
                }

            frontend_rebuilt = await self._maybe_rebuild_frontend()
            ordered_modules = discover_conclave_modules(self.package_dir)
            reloaded_names: list[str] = []
            rebind_map: dict[int, tuple[Any, Any]] = {}

            for mod_name in ordered_modules:
                mod = sys.modules.get(mod_name)
                if mod is None:
                    continue
                old_dict = dict(mod.__dict__)
                try:
                    importlib.reload(mod)
                    _preserve_enum_and_exception_identities(mod, old_dict, rebind_map)
                    reloaded_names.append(mod_name)
                except Exception:
                    logger.warning("failed to reload module %s", mod_name, exc_info=True)

            # Rebind updated class/function/enum references in loaded modules so existing callers
            # and test modules see the freshly reloaded classes.
            if rebind_map:
                for loaded_name, loaded_mod in list(sys.modules.items()):
                    if loaded_mod is None or not (
                        loaded_name.startswith("conclave") or loaded_name.startswith("test")
                    ):
                        continue
                    for attr, val in list(loaded_mod.__dict__.items()):
                        if attr.startswith("__"):
                            continue
                        new_val, changed = _remap_value(val, rebind_map)
                        if changed:
                            with contextlib.suppress(Exception):
                                setattr(loaded_mod, attr, new_val)
                        marks = getattr(val, "pytestmark", None)
                        if isinstance(marks, list):
                            for mark in marks:
                                if (
                                    getattr(mark, "name", None) == "parametrize"
                                    and len(mark.args) >= 2
                                ):
                                    remapped_args, m_changed = _remap_value(
                                        mark.args[1], rebind_map
                                    )
                                    if m_changed:
                                        with contextlib.suppress(Exception):
                                            object.__setattr__(
                                                mark,
                                                "args",
                                                (mark.args[0], remapped_args, *mark.args[2:]),
                                            )

            # c) Re-run DB migrations and seed global defaults on live DB connection.
            db = getattr(daemon, "db", None)
            if db is not None and getattr(db, "_conn", None) is not None:
                if hasattr(db, "_apply_migrations"):
                    await db._apply_migrations()
                from .bootstrap import seed_global_defaults

                await seed_global_defaults(db)

            # d) Hot-swap orchestrator, planning_orchestrator, and ProjectWorker instances
            #    while preserving daemon.db, daemon.bus (and active subscribers!), and reloader.
            from .engine.bug_fixer import BugFixerController
            from .engine.orchestrator import Orchestrator
            from .planning.session import PlanningOrchestrator
            from .runtime import ProjectWorker

            old_orch = getattr(daemon, "orchestrator", None)
            if old_orch is not None and db is not None:
                new_orch = Orchestrator(
                    daemon.db,
                    daemon.bus,
                    daemon.provider,
                    daemon.home,
                )
                new_orch._merge_locks = dict(getattr(old_orch, "_merge_locks", {}))
                new_orch._merge_lock_refs = dict(getattr(old_orch, "_merge_lock_refs", {}))
                new_orch._cancel_events = dict(getattr(old_orch, "_cancel_events", {}))
                new_orch._pending_steers = dict(getattr(old_orch, "_pending_steers", {}))
                new_orch._node_steers = dict(getattr(old_orch, "_node_steers", {}))
                new_orch._approval_waiters = dict(getattr(old_orch, "_approval_waiters", {}))
                new_orch._approval_resolutions = dict(
                    getattr(old_orch, "_approval_resolutions", {})
                )
                new_orch._dynamic_hitl = dict(getattr(old_orch, "_dynamic_hitl", {}))
                new_orch._reloader = self
                new_orch._daemon = daemon
                daemon.orchestrator = new_orch

            old_planning = getattr(daemon, "planning_orchestrator", None)
            if old_planning is not None and db is not None:
                new_planning = PlanningOrchestrator(
                    daemon.db,
                    daemon.bus,
                    daemon.provider,
                )
                new_planning._active_sessions = dict(
                    getattr(old_planning, "_active_sessions", {})
                )
                new_planning._bg_tasks = set(getattr(old_planning, "_bg_tasks", set()))
                new_planning._session_locks = dict(
                    getattr(old_planning, "_session_locks", {})
                )
                daemon.planning_orchestrator = new_planning

            workers: dict[str, Any] = getattr(daemon, "_workers", {})
            current_task = asyncio.current_task()
            for pid, old_worker in list(workers.items()):
                new_worker = ProjectWorker(
                    daemon.db,
                    daemon.orchestrator,
                    pid,
                    idle_sleep=getattr(old_worker, "_idle_sleep", 2.0),
                    max_idle_sleep=getattr(old_worker, "_max_idle_sleep", 30.0),
                )
                new_worker.paused = bool(getattr(old_worker, "paused", False))
                new_worker._current_idle_sleep = float(
                    getattr(old_worker, "_current_idle_sleep", new_worker._idle_sleep)
                )
                new_worker._session_budget = getattr(old_worker, "_session_budget", None)
                new_worker._session_started = getattr(old_worker, "_session_started", None)
                new_worker._reloader = self
                new_worker._daemon = daemon

                old_task: asyncio.Task[None] | None = getattr(old_worker, "_task", None)
                if old_task is not None and not old_task.done():
                    if old_task is current_task:
                        # Reload triggered from inside this worker's task finalization;
                        # update old_worker in-place as well and transfer _task handle.
                        old_worker._orchestrator = daemon.orchestrator
                        old_worker._bug_fixer = BugFixerController(daemon.orchestrator)
                        new_worker._task = old_task
                    else:
                        await old_worker.stop()
                        new_worker._task = asyncio.create_task(new_worker._loop())
                workers[pid] = new_worker

            # e) Refresh FastAPI route table in-place if app is attached.
            target_app = app or getattr(daemon, "app", None)
            routes_count = 0
            if target_app is not None:
                routes_count = refresh_fastapi_routes(target_app)

            self.reload_count += 1
            self.reload_pending = False
            self.pending_reason = None
            self.last_reload_at = time.time()
            self._snapshot = self.snapshot_files()

            from . import __version__ as reloaded_version
            from .events import EventType

            payload: dict[str, Any] = {
                "reload_count": self.reload_count,
                "modules_reloaded": len(reloaded_names),
                "frontend_rebuilt": frontend_rebuilt,
                "version": reloaded_version,
                "routes_refreshed": routes_count,
            }
            if getattr(daemon, "bus", None) is not None and getattr(db, "_conn", None) is not None:
                await daemon.bus.emit(
                    type=EventType.system_reloaded,
                    payload=payload,
                )

            result = {
                "reloaded": True,
                "deferred": False,
                "reload_pending": False,
                "quiescent": True,
                **payload,
            }
            self.last_result = result
            return result
