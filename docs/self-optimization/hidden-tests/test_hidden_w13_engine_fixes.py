"""Hidden acceptance tests for W13 (engine backlog fixes).

Written by the pilot from the spec alone, run against the task branch before merging; not part
of the repository's own suite. Needs `tests/integration` on PYTHONPATH (FakeProvider).
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest_asyncio
from fake_provider import FakeProvider
from httpx import ASGITransport

from conclave.bootstrap import seed_global_defaults
from conclave.config.resolver import save_project_config
from conclave.db import Database, TaskState
from conclave.db import repositories as repo
from conclave.engine import Orchestrator, run_git
from conclave.engine.test_integrity import FileChange, integrity_findings
from conclave.engine.worktree import WorktreeManager
from conclave.events import EventBus
from conclave.providers import AgentResult, OnChunk, ResolvedProfile
from conclave.runtime import Daemon
from conclave.web import create_app


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "conclave.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


async def _git(cwd: Path, *args: str) -> str:
    code, out = await run_git(cwd, *args)
    assert code == 0, (args, out)
    return out.strip()


async def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    await _git(path, "init", "-b", "main")
    await _git(path, "config", "user.email", "t@example.com")
    await _git(path, "config", "user.name", "T")
    (path / "README.md").write_text("# t\n")
    await _git(path, "add", "-A")
    await _git(path, "commit", "-m", "init")
    return path


# --- 4. focus-marker scan is linear --------------------------------------------------------


def _focus_change(n: int) -> FileChange:
    before = "".join(f'it("t{i}", () => {{ expect(1).toBe(1); }});\n' for i in range(n))
    return FileChange("M", "web/a.test.js", "web/a.test.js", before,
                      before.replace('it("', 'it.only("'))


def test_focus_marker_findings_unchanged() -> None:
    found = integrity_findings([_focus_change(200)])
    assert sorted((f.file, f.test, f.kind) for f in found) == sorted(
        ("web/a.test.js", f"t{i}", "skip_marker") for i in range(200)
    )


def test_focus_marker_scan_is_linear() -> None:
    """5x the markers must cost well under 25x the time (a quadratic scan's ratio), measured on
    this host against itself, so a loaded machine does not fail it (best of three runs each)."""

    def best(n: int) -> float:
        change = _focus_change(n)
        times = []
        for _ in range(3):
            t = time.perf_counter()
            assert len(integrity_findings([change])) == n
            times.append(time.perf_counter() - t)
        return min(times)

    small, big = best(4_000), best(20_000)
    assert big < 12 * small, f"4k: {small:.3f}s, 20k: {big:.3f}s (linear ~5x, quadratic ~25x)"


# --- 1. concurrent merges --------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(db: Database, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    await seed_global_defaults(db)
    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        try:
            yield c
        finally:
            await daemon.shutdown()


async def test_concurrent_merges_of_one_task(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    rp = await _repo(tmp_path / "repo")
    created = await client.post(
        "/api/projects", json={"name": "m", "path": str(rp), "default_branch": "main"}
    )
    assert created.status_code == 200, created.text
    pid = created.json()["id"]
    t = await client.post(f"/api/projects/{pid}/tasks", json={"request": "do it"})
    tid = t.json()["id"]
    wm = WorktreeManager(rp, tmp_path / "home" / "projects" / pid / "worktrees")
    wt = await wm.create(tid, "main", f"conclave/{tid}")
    (wt / "task.txt").write_text("change\n")
    await _git(wt, "add", "-A")
    await _git(wt, "commit", "-m", "task work")
    await wm.cleanup(tid)
    await repo.finalize_task(db, tid, state=TaskState.done, result_summary="merged=False")
    # main moves on, so the merge creates a merge commit (a real write under the lock)
    (rp / "main.txt").write_text("later\n")
    await _git(rp, "add", "-A")
    await _git(rp, "commit", "-m", "main moves on")

    a, b = await asyncio.gather(
        client.post(f"/api/tasks/{tid}/merge"), client.post(f"/api/tasks/{tid}/merge")
    )
    codes = sorted([a.status_code, b.status_code])
    assert codes == [200, 409], (a.text, b.text)
    ok = a if a.status_code == 200 else b
    head = await _git(rp, "rev-parse", "main")
    assert ok.json()["target_sha"] == head
    # exactly one merge of the task branch landed on main
    merges = await _git(rp, "rev-list", "--merges", "main")
    assert len([m for m in merges.splitlines() if m]) <= 1
    log = await _git(rp, "log", "--format=%s", "main")
    assert log.count("task work") == 1


# --- 2. baseline rows are bounded ------------------------------------------------------------


async def test_baseline_output_is_bounded(db: Database, tmp_path: Path) -> None:
    rp = await _repo(tmp_path / "repo")
    cmd = (f"{sys.executable} -c \"import sys; sys.stdout.write('FAILED tests/x.py::t\\n' * 40000);"
           " sys.exit(1)\"")
    project = await repo.create_project(
        db, name="t", path=str(rp), default_branch="main",
        config={"execution": {"target_branch": "main", "baseline_test_command": cmd},
                "context": {"bulk_max_chars": 20_000}},
    )
    await repo.create_task(db, project_id=project.id, request="add a feature file",
                           state=TaskState.approved)
    orch = Orchestrator(db, EventBus(db), FakeProvider(), tmp_path / "home")
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    await orch.process_task(claimed)
    rows = await db.fetchall("SELECT output FROM baselines WHERE project_id = ?", (project.id,))
    assert rows, "no baseline stored"
    for row in rows:
        out = str(row["output"] if not isinstance(row, tuple) else row[0])
        assert len(out) <= 20_000 + 600, len(out)
        assert "chars omitted" in out


# --- 3b. test command removed mid-task fails the attempt ------------------------------------


class _RemovesTestCommand(FakeProvider):
    def __init__(self, db: Database, project_id: str) -> None:
        super().__init__()
        self._db = db
        self._pid = project_id
        self.removed = False

    async def run_agent(self, *, profile: ResolvedProfile, prompt: str, timeout_seconds: int,
                        cwd: Path | None = None, on_chunk: OnChunk | None = None,
                        cancel_event: asyncio.Event | None = None) -> AgentResult:
        if not self.removed and "You are the developer agent" in prompt:
            project = await repo.get_project(self._db, self._pid)
            assert project is not None
            await save_project_config(
                self._db, project, {}, unset=["execution.baseline_test_command"]
            )
            self.removed = True
        return await super().run_agent(
            profile=profile, prompt=prompt, timeout_seconds=timeout_seconds, cwd=cwd,
            on_chunk=on_chunk, cancel_event=cancel_event,
        )


async def test_test_command_removed_mid_task_fails(db: Database, tmp_path: Path) -> None:
    rp = await _repo(tmp_path / "repo")
    project = await repo.create_project(
        db, name="t", path=str(rp), default_branch="main",
        config={"execution": {"target_branch": "main", "baseline_test_command": "echo ok"}},
    )
    task = await repo.create_task(db, project_id=project.id, request="add a feature file",
                                  state=TaskState.approved)
    provider = _RemovesTestCommand(db, project.id)
    orch = Orchestrator(db, EventBus(db), provider, tmp_path / "home")
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    await orch.process_task(claimed)
    assert provider.removed
    final = await repo.get_task(db, task.id)
    assert final is not None
    assert final.state is not TaskState.done, "gate passed without a test command"
    events = await repo.list_events(db, task_id=task.id)
    blob = " ".join(str(e.payload) for e in events if e.type in ("attempt.failed", "task.failed"))
    blob += " " + (final.result_summary or "")
    assert "no_test_command" in blob


