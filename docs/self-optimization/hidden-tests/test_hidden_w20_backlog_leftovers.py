"""Hidden acceptance tests for W20 items 1 and 2, written by the pilot from the spec alone.

- 1: every reviewer prompt carries the baseline preamble when the baseline has failures;
- 2: unknown ids answer 404 on REST, with nothing emitted.

Run against a W20 snapshot with ``PYTHONPATH=<snap>/src:<snap>/tests/integration``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fake_provider import FakeProvider
from httpx import ASGITransport

from conclave.bootstrap import seed_global_defaults
from conclave.db import Database, Task, TaskState
from conclave.db import repositories as repo
from conclave.engine import Orchestrator, run_git
from conclave.events import EventBus
from conclave.runtime import Daemon
from conclave.web import create_app

_PRE_EXISTING = "tests/test_legacy.py::test_old_bug"
_PREAMBLE = "PRE-EXISTING TEST FAILURES"


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "conclave.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("conclave.engine.orchestrator._REVIEWER_RETRY_BACKOFF_S", 0.0)


async def _init_repo(path: Path, red: bool) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    await run_git(path, "config", "user.email", "test@example.com")
    await run_git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# test repo\n", encoding="utf-8")
    script = (
        f'#!/bin/sh\necho "FAILED {_PRE_EXISTING} - assert 0"\nexit 1\n'
        if red
        else "#!/bin/sh\necho '1 passed'\nexit 0\n"
    )
    (path / "check.sh").write_text(script, encoding="utf-8")
    await run_git(path, "add", "-A")
    await run_git(path, "commit", "-m", "initial commit")


async def _run_task(db: Database, tmp_path: Path, provider: FakeProvider, red: bool) -> Task:
    await _init_repo(tmp_path / "repo", red)
    project = await repo.create_project(
        db, name="t", path=str(tmp_path / "repo"), default_branch="main",
        config={
            "execution": {
                "target_branch": "main",
                "baseline_test_command": "sh check.sh",
                "require_full_green": False,
            },
            "agent_overrides": {"developer": {"max_retries": 1}},
        },
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    await Orchestrator(db, EventBus(db), provider, tmp_path / "home").process_task(claimed)
    final = await repo.get_task(db, task.id)
    assert final is not None
    return final


def _review_prompts(provider: FakeProvider) -> list[str]:
    return [p for p in provider.prompts if FakeProvider.role_of(p).startswith("reviewer:")]


# --- 1. reviewers get the baseline preamble ----------------------------------------------------


async def test_every_reviewer_prompt_carries_a_red_baseline(db: Database, tmp_path: Path) -> None:
    provider = FakeProvider()
    await _run_task(db, tmp_path, provider, red=True)

    developer = [p for p in provider.prompts if "You are the developer agent of" in p]
    assert developer and _PRE_EXISTING in developer[0]  # the baseline was red, as intended
    reviews = _review_prompts(provider)
    assert reviews
    for prompt in reviews:
        assert _PREAMBLE in prompt and _PRE_EXISTING in prompt


async def test_no_reviewer_prompt_carries_a_green_baseline(db: Database, tmp_path: Path) -> None:
    provider = FakeProvider()
    await _run_task(db, tmp_path, provider, red=False)

    reviews = _review_prompts(provider)
    assert reviews
    assert all(_PREAMBLE not in prompt for prompt in reviews)


# --- 2. unknown ids answer 404 on REST -----------------------------------------------------------


@pytest_asyncio.fixture
async def client(db: Database, tmp_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    await seed_global_defaults(db)
    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        try:
            yield c
        finally:
            await daemon.shutdown()


_UNKNOWN = "0" * 32


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", f"/api/projects/{_UNKNOWN}/tasks"),
        ("GET", f"/api/projects/{_UNKNOWN}/quarantine"),
        ("GET", f"/api/tasks/{_UNKNOWN}/events"),
        ("DELETE", f"/api/projects/{_UNKNOWN}"),
        ("POST", f"/api/projects/{_UNKNOWN}/pause"),
        ("POST", f"/api/projects/{_UNKNOWN}/resume"),
    ],
)
async def test_unknown_ids_answer_404_and_emit_nothing(
    client: httpx.AsyncClient, db: Database, method: str, path: str
) -> None:
    before = await repo.list_events(db, limit=10_000)
    response = await client.request(method, path)
    assert response.status_code == 404, response.text
    after = await repo.list_events(db, limit=10_000)
    assert [e.id for e in after] == [e.id for e in before]


async def test_known_ids_still_answer_200(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    await _init_repo(tmp_path / "known", red=False)
    created = await client.post(
        "/api/projects", json={"name": "k", "path": str(tmp_path / "known")}
    )
    assert created.status_code == 200, created.text
    body: dict[str, Any] = created.json()
    pid = body.get("id") or body["project"]["id"]
    for path in (f"/api/projects/{pid}/tasks", f"/api/projects/{pid}/quarantine"):
        assert (await client.get(path)).status_code == 200
