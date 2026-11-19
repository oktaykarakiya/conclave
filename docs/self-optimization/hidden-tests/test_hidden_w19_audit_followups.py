"""Hidden acceptance tests for W19 (audit follow-ups), written from the spec alone.

Run against a W19 snapshot with ``PYTHONPATH=<snap>/src:<snap>/tests/integration``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fake_provider import FakeProvider

from conclave.config import ArgMode
from conclave.db import Database, Task, TaskState
from conclave.db import repositories as repo
from conclave.engine import Orchestrator, run_git
from conclave.events import EventBus
from conclave.providers import AgentResult, OnChunk, ResolvedProfile
from conclave.providers.base import QuotaExhausted
from conclave.providers.profiles import _ISOLATION_ENV, build_invocation
from conclave.providers.quota import RESUME_MARGIN, QuotaGate

_FAIL = AgentResult(
    ok=True,
    text='```json\n{"verdict": "fail", "reason": "needs work", "evidence": []}\n```',
    model_reported="fake",
)


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


# --- 1. isolation a settings file cannot undo ------------------------------------------------


def _settings_env(args: list[str]) -> dict[str, str]:
    assert "--settings" in args, args
    i = len(args) - 1 - args[::-1].index("--settings")
    settings = json.loads(args[i + 1])
    env = settings["env"]
    assert isinstance(env, dict)
    return env


def test_isolated_worker_passes_isolation_env_as_command_line_settings() -> None:
    inv = build_invocation(ResolvedProfile(name="p", arg_mode=ArgMode.flag, isolated=True))
    env = _settings_env(inv.args)
    for key, value in _ISOLATION_ENV.items():
        assert env[key] == value
    # The process environment still carries them too.
    for key, value in _ISOLATION_ENV.items():
        assert inv.env[key] == value


def test_extra_env_still_wins_inside_the_settings_env() -> None:
    key = next(iter(_ISOLATION_ENV))
    inv = build_invocation(
        ResolvedProfile(name="p", arg_mode=ArgMode.flag, isolated=True, extra_env={key: "0"})
    )
    assert _settings_env(inv.args)[key] == "0"
    assert inv.env[key] == "0"


def test_non_isolated_worker_has_no_settings_flag() -> None:
    inv = build_invocation(ResolvedProfile(name="p", arg_mode=ArgMode.flag, isolated=False))
    assert "--settings" not in inv.args


# --- 3. an absurd usage-limit reset is ignored ------------------------------------------------


def _gate(now: datetime) -> QuotaGate:
    return QuotaGate(clock=lambda: now)


def test_reset_30_days_out_waits_retry_minutes() -> None:
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    quota = QuotaExhausted(kind="rate_limit", resets_at=now + timedelta(days=30))
    reopens = _gate(now).close("k", "claude", quota, retry_minutes=15)
    assert reopens == now + timedelta(minutes=15)


def test_reset_6_days_out_is_kept() -> None:
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    resets = now + timedelta(days=6)
    quota = QuotaExhausted(kind="rate_limit", resets_at=resets, window="seven_day")
    reopens = _gate(now).close("k", "claude", quota, retry_minutes=15)
    assert reopens == resets + RESUME_MARGIN


# --- engine helpers -----------------------------------------------------------------------------


async def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    await run_git(path, "config", "user.email", "test@example.com")
    await run_git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# test repo\n", encoding="utf-8")
    await run_git(path, "add", "-A")
    await run_git(path, "commit", "-m", "initial commit")


async def _run_task(
    db: Database, tmp_path: Path, provider: FakeProvider, config: dict[str, Any] | None = None
) -> tuple[Task, bool]:
    await _init_repo(tmp_path / "repo")
    settings = dict(config or {})
    execution = {"target_branch": "main", "allow_no_test_command": True}
    execution.update(settings.pop("execution", {}))
    project = await repo.create_project(
        db, name="t", path=str(tmp_path / "repo"), default_branch="main",
        config={**settings, "execution": execution},
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")
    ok = await orchestrator.process_task(claimed)
    final = await repo.get_task(db, task.id)
    assert final is not None
    return final, ok


_DEVELOPER = "You are the developer agent of"


def _dev_prompts(provider: FakeProvider) -> list[str]:
    """Developer dispatches only (the post-mortem's prompt also reads as role ``developer``)."""
    return [p for p in provider.prompts if _DEVELOPER in p]


# --- 2. reviewers that change the shared worktree -----------------------------------------------


async def test_reviewer_changes_to_the_worktree_are_reported_and_not_committed(
    db: Database, tmp_path: Path
) -> None:
    provider = FakeProvider(reviewer_tampers=True)

    task, ok = await _run_task(db, tmp_path, provider)

    assert ok is True and task.state is TaskState.done
    events = [
        e for e in await repo.list_events(db, task_id=task.id, limit=10_000)
        if e.type == "review.worktree_modified"
    ]
    assert events, "no review.worktree_modified event"
    payload = json.dumps(events[0].payload)
    assert "STRAY_REVIEWER.txt" in payload and "FEATURE.txt" in payload
    assert task.branch
    code, content = await run_git(tmp_path / "repo", "show", f"{task.branch}:FEATURE.txt")
    assert code == 0 and content == "done\n"
    stray = f"{task.branch}:STRAY_REVIEWER.txt"
    code, _ = await run_git(tmp_path / "repo", "cat-file", "-e", stray)
    assert code != 0


async def test_reviewers_that_change_nothing_emit_no_worktree_event(
    db: Database, tmp_path: Path
) -> None:
    task, ok = await _run_task(db, tmp_path, FakeProvider())
    assert ok is True
    types = {e.type for e in await repo.list_events(db, task_id=task.id, limit=10_000)}
    assert "review.worktree_modified" not in types


async def test_every_reviewer_prompt_says_the_worktree_is_shared(
    db: Database, tmp_path: Path
) -> None:
    provider = FakeProvider()
    await _run_task(db, tmp_path, provider)
    reviews = [p for p in provider.prompts if FakeProvider.role_of(p).startswith("reviewer:")]
    assert reviews
    for prompt in reviews:
        lower = prompt.lower()
        assert "shared" in lower and "worktree" in lower and "temporar" in lower


# --- 4. review_rounds_max bounds the attempts ---------------------------------------------------


async def test_review_rounds_max_stops_the_retry_loop(db: Database, tmp_path: Path) -> None:
    provider = FakeProvider(replies={"reviewer:security": [_FAIL]})

    task, ok = await _run_task(
        db, tmp_path, provider,
        {
            "execution": {"review_rounds_max": 1},
            "agent_overrides": {"developer": {"max_retries": 3}},
        },
    )

    assert ok is False and task.state is TaskState.failed
    assert len(_dev_prompts(provider)) == 1
    assert "review_rounds_max" in (task.result_summary or "")


async def test_default_limits_still_make_three_attempts(db: Database, tmp_path: Path) -> None:
    provider = FakeProvider(replies={"reviewer:security": [_FAIL]})

    task, ok = await _run_task(db, tmp_path, provider)

    assert ok is False and task.state is TaskState.failed
    assert len(_dev_prompts(provider)) == 3
    assert (task.result_summary or "").startswith("failed (max_retries) after 3 attempt(s)")


# --- 5. the newest rejected diff is sent once ---------------------------------------------------


class _MarkedDeveloper(FakeProvider):
    """Writes a distinct marker on each developer attempt, so each attempt's diff is unique."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.dev_runs = 0

    async def run_agent(
        self,
        *,
        profile: ResolvedProfile,
        prompt: str,
        timeout_seconds: int,
        cwd: Path | None = None,
        on_chunk: OnChunk | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AgentResult:
        if _DEVELOPER in prompt:
            self.dev_runs += 1
            self.developer_content = f"attempt-marker-{self.dev_runs}\n"
        return await super().run_agent(
            profile=profile, prompt=prompt, timeout_seconds=timeout_seconds, cwd=cwd,
            on_chunk=on_chunk, cancel_event=cancel_event,
        )


async def test_third_attempt_prompt_carries_each_rejected_diff_once(
    db: Database, tmp_path: Path
) -> None:
    provider = _MarkedDeveloper(replies={"reviewer:security": [_FAIL]})

    await _run_task(db, tmp_path, provider)

    prompts = _dev_prompts(provider)
    assert len(prompts) == 3
    third = prompts[2]
    assert third.count("+attempt-marker-1") == 1
    assert third.count("+attempt-marker-2") == 1
