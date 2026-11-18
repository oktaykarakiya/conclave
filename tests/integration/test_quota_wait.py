"""A usage limit waits and resumes the interrupted dispatch; it never fails or degrades the work.

The fake provider refuses one role's first dispatch for quota (``quota_once``); the gate runs on
a fake clock, so a wait of hours takes no real time. Each test checks the dispatch's real
outcome counts (no attempt spent, the plan used, the real verdict recorded) and that the resumed
run continued the reported session.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fake_provider import FakeProvider
from httpx import ASGITransport

from conclave.config import ConclaveConfig
from conclave.db import Database, EventRow, Project, ProjectMode, Task, TaskState
from conclave.db import repositories as repo
from conclave.db.planning_models import PlanningSessionStatus
from conclave.engine import Orchestrator, discover_bug, run_git
from conclave.engine import runner as runner_module
from conclave.events import EventBus
from conclave.mcp.server import handle_mcp_message
from conclave.planning.session import PlanningOrchestrator
from conclave.providers import (
    CONTINUATION_PROMPT,
    AgentResult,
    OnChunk,
    QuotaExhausted,
    QuotaGate,
    ResolvedProfile,
)
from conclave.runtime import Daemon, ProjectWorker
from conclave.web import create_app

T0 = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
# The key every FakeProvider dispatch draws on: the built-in model is deepseek/deepseek-flash.
_KEY = "opencode/deepseek"
_GATED = {"target_branch": "main", "baseline_test_command": "echo ok"}


class FakeClock:
    """The gate's UTC clock: it moves only when the gate sleeps, by the time slept."""

    def __init__(self) -> None:
        self.current = T0

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)
        await asyncio.sleep(0)


class BlockingClock:
    """A gate clock that never moves: a wait on it lasts until it is cancelled."""

    def __init__(self) -> None:
        self.sleeping = asyncio.Event()

    def now(self) -> datetime:
        return T0

    async def sleep(self, seconds: float) -> None:
        self.sleeping.set()
        await asyncio.Event().wait()


async def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    await run_git(path, "config", "user.email", "test@example.com")
    await run_git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# test repo\n")
    await run_git(path, "add", "-A")
    await run_git(path, "commit", "-m", "initial commit")


async def _project_and_task(
    db: Database, tmp_path: Path, execution: dict[str, Any] | None = None
) -> tuple[Project, Task]:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {**_GATED, **(execution or {})}},
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )
    return project, task


async def _process(orchestrator: Orchestrator, project: Project) -> bool:
    claimed = await repo.claim_next_approved(orchestrator._db, project.id)
    assert claimed is not None
    return await orchestrator.process_task(claimed)


def _of(events: list[EventRow], type_: str) -> list[EventRow]:
    return [e for e in events if e.type == type_]


def _assert_waited_and_resumed(
    provider: FakeProvider,
    events: list[EventRow],
    *,
    agent: str,
    node_id: str,
    session: str = "ses-q-1",
) -> None:
    """One wait for *agent*'s refused run, then a resume of its *session*."""
    (waiting,) = [e for e in _of(events, "quota.waiting") if e.agent == agent]
    assert waiting.payload == {
        "engine": "opencode",
        "key": _KEY,
        "kind": "rate_limit",
        "window": "five_hour",
        "resets_at": None,
        "resumes_at": waiting.payload["resumes_at"],
        "message": "You've hit your limit",
        "agent": agent,
        "node_id": node_id,
    }
    # No reset time was reported: the key reopens after execution.quota_retry_minutes (15).
    assert datetime.fromisoformat(waiting.payload["resumes_at"]) >= T0 + timedelta(minutes=15)
    (resumed,) = [e for e in _of(events, "quota.resumed") if e.agent == agent]
    assert resumed.payload["resumed_session"] is True
    assert resumed.payload["waited_seconds"] >= 15 * 60
    assert {resumed.payload[k] for k in ("engine", "key")} == {"opencode", _KEY}
    resumes = [
        (profile.resume_session_id, prompt)
        for profile, prompt in zip(provider.profiles, provider.prompts, strict=True)
        if profile.resume_session_id is not None
    ]
    assert resumes == [(session, CONTINUATION_PROMPT)]


def _orchestrator(
    db: Database, tmp_path: Path, provider: FakeProvider, clock: FakeClock | BlockingClock
) -> Orchestrator:
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    return Orchestrator(db, EventBus(db), provider, tmp_path / "home", quota_gate=gate)


# --- every kind of task dispatch waits and resumes ---------------------------------------


async def test_a_planner_limit_hit_waits_and_the_plan_is_used(
    db: Database, tmp_path: Path
) -> None:
    project, task = await _project_and_task(db, tmp_path)
    provider = FakeProvider(quota_once={"planner"})

    assert await _process(_orchestrator(db, tmp_path, provider, FakeClock()), project) is True

    done = await repo.get_task(db, task.id)
    assert done is not None and done.state is TaskState.done
    assert done.result_summary == "completed in 1 attempt(s); merged=False"
    assert done.plan == {"approach": "create the file", "files_to_touch": ["FEATURE.txt"]}
    (developer_prompt,) = [
        p for p in provider.prompts
        if provider.role_of(p) == "developer" and p != CONTINUATION_PROMPT
    ]
    assert "=== PLAN (from the Planner" in developer_prompt
    events = await repo.list_events(db, task_id=task.id)
    _assert_waited_and_resumed(provider, events, agent="planner", node_id="planner")
    assert [e.payload["ok"] for e in _of(events, "agent.result") if e.agent == "planner"] == [
        True
    ]


async def test_a_developer_limit_hit_consumes_no_attempt(db: Database, tmp_path: Path) -> None:
    project, task = await _project_and_task(db, tmp_path)
    provider = FakeProvider(quota_once={"developer"})

    assert await _process(_orchestrator(db, tmp_path, provider, FakeClock()), project) is True

    done = await repo.get_task(db, task.id)
    assert done is not None and done.state is TaskState.done
    assert done.result_summary == "completed in 1 attempt(s); merged=False"
    events = await repo.list_events(db, task_id=task.id)
    assert _of(events, "attempt.failed") == []
    assert len(_of(events, "attempt.started")) == 1
    _assert_waited_and_resumed(provider, events, agent="developer", node_id="developer")
    code, out = await run_git(Path(project.path), "show", f"conclave/{task.id}:FEATURE.txt")
    assert code == 0 and out == "done\n"
    # One agent.result per dispatch, and a usage row for the refused run besides each dispatch's.
    results = _of(events, "agent.result")
    assert [e.payload["ok"] for e in results if e.agent == "developer"] == [True]
    assert (await repo.get_task_usage(db, task.id))["agent_count"] == len(results) + 1


async def test_one_of_several_parallel_reviewers_hitting_the_limit_keeps_its_real_verdict(
    db: Database, tmp_path: Path
) -> None:
    project, task = await _project_and_task(db, tmp_path, {"parallel_reviewers": True})
    provider = FakeProvider(quota_once={"reviewer:security"})

    assert await _process(_orchestrator(db, tmp_path, provider, FakeClock()), project) is True

    verdicts = await repo.list_verdicts(db, task.id)
    assert {"tester", "security", "reviewer"} <= {v.agent for v in verdicts}
    assert {v.verdict for v in verdicts} == {"pass"}  # no 'unknown' for the refused reviewer
    events = await repo.list_events(db, task_id=task.id)
    assert not [e for e in _of(events, "grounding.warning") if "dispatch failed" in str(e.payload)]
    _assert_waited_and_resumed(provider, events, agent="security", node_id="security")
    assert _of(events, "attempt.failed") == []


async def test_a_gate_fix_up_hitting_the_limit_waits_and_fixes(
    db: Database, tmp_path: Path
) -> None:
    gate_check = (
        "import pathlib, sys; "
        "f = pathlib.Path('FEATURE.txt'); "
        "sys.exit(0 if f.exists() and f.read_text() == 'fixed\\n' else 1)"
    )
    project, task = await _project_and_task(
        db, tmp_path, {"baseline_test_command": f'"{sys.executable}" -c "{gate_check}"'}
    )
    provider = FakeProvider(quota_once={"fixup"}, fixup_content="fixed\n")

    assert await _process(_orchestrator(db, tmp_path, provider, FakeClock()), project) is True

    done = await repo.get_task(db, task.id)
    assert done is not None and done.state is TaskState.done
    assert done.result_summary == "completed in 2 attempt(s); merged=False"
    events = await repo.list_events(db, task_id=task.id)
    assert [e.payload.get("stage") for e in _of(events, "attempt.failed")] == ["gate"]
    _assert_waited_and_resumed(provider, events, agent="developer", node_id="developer")
    code, out = await run_git(Path(project.path), "show", f"conclave/{task.id}:FEATURE.txt")
    assert code == 0 and out == "fixed\n"


class _SessionTotalCosts(FakeProvider):
    """Reports the developer's cost as Claude Code does, as the session's running total: 4.52
    when the limit refuses its run, 8.49 for the whole session once the resumed run ends."""

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
        result = await super().run_agent(
            profile=profile, prompt=prompt, timeout_seconds=timeout_seconds, cwd=cwd,
            on_chunk=on_chunk, cancel_event=cancel_event,
        )
        if result.quota is not None:
            return result.model_copy(update={"cost_usd": 4.52, "cost_is_session_total": True})
        if profile.resume_session_id is not None:
            return result.model_copy(update={"cost_usd": 8.49, "cost_is_session_total": True})
        return result


async def test_a_resumed_sessions_usage_rows_add_up_to_its_real_cost(
    db: Database, tmp_path: Path
) -> None:
    project, task = await _project_and_task(db, tmp_path)
    provider = _SessionTotalCosts(quota_once={"developer"})

    assert await _process(_orchestrator(db, tmp_path, provider, FakeClock()), project) is True

    rows = await db.fetchall(
        "SELECT cost_usd FROM usage WHERE task_id = ? AND agent = 'developer' ORDER BY rowid",
        (task.id,),
    )
    assert [row["cost_usd"] for row in rows] == [4.52, pytest.approx(3.97)]
    # Every other dispatch of the task cost nothing: the rows sum to the session's real total.
    assert (await repo.usage_summary(db, project.id))["total_cost_usd"] == pytest.approx(8.49)


# --- budgets, cancellation, restart ------------------------------------------------------


class _ShiftedTime:
    """The orchestrator's ``time``: its monotonic clock moves with the gate's fake clock too."""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock

    def monotonic(self) -> float:
        return time.monotonic() + (self._clock.current - T0).total_seconds()


async def test_a_two_hour_wait_does_not_consume_a_one_hour_budget(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, task = await _project_and_task(db, tmp_path, {"wall_clock_budget_minutes": 60})
    provider = FakeProvider(quota_once={"developer"}, quota_resets_at=T0 + timedelta(hours=2))
    clock = FakeClock()
    monkeypatch.setattr("conclave.engine.orchestrator.time", _ShiftedTime(clock))

    assert await _process(_orchestrator(db, tmp_path, provider, clock), project) is True

    assert clock.current >= T0 + timedelta(hours=2, minutes=1)  # the wait did take two hours
    done = await repo.get_task(db, task.id)
    assert done is not None and done.state is TaskState.done


async def test_a_cancel_during_the_wait_cancels_the_task_at_once(
    db: Database, tmp_path: Path
) -> None:
    project, task = await _project_and_task(db, tmp_path)
    provider = FakeProvider(quota_once={"developer"})
    clock = BlockingClock()
    orchestrator = _orchestrator(db, tmp_path, provider, clock)
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    cancel_event = asyncio.Event()
    orchestrator._cancel_events[task.id] = cancel_event
    running = asyncio.create_task(orchestrator.process_task(claimed, cancel_event=cancel_event))
    await asyncio.wait_for(clock.sleeping.wait(), 10)
    dispatches = len(provider.prompts)

    assert orchestrator.request_cancel(task.id) is True
    assert await asyncio.wait_for(running, 5) is False

    cancelled = await repo.get_task(db, task.id)
    assert cancelled is not None and cancelled.state is TaskState.cancelled
    assert len(provider.prompts) == dispatches  # nothing ran after the cancel
    events = await repo.list_events(db, task_id=task.id)
    assert len(_of(events, "quota.waiting")) == 1 and _of(events, "quota.resumed") == []


async def test_a_restart_during_the_wait_recovers_the_task_and_it_completes(
    db: Database, tmp_path: Path
) -> None:
    project, task = await _project_and_task(db, tmp_path)
    clock = BlockingClock()
    first = _orchestrator(db, tmp_path, FakeProvider(quota_once={"developer"}), clock)
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    running = asyncio.create_task(first.process_task(claimed))
    await asyncio.wait_for(clock.sleeping.wait(), 10)

    running.cancel()  # the daemon stops mid-wait
    with contextlib.suppress(asyncio.CancelledError):
        await running
    stranded = await repo.get_task(db, task.id)
    assert stranded is not None and stranded.state is TaskState.in_progress

    # The next start recovers it, and its re-run (still limited once) waits and completes.
    assert await first.recover(project.id) == (1, 0)
    recovered = await repo.get_task(db, task.id)
    assert recovered is not None and recovered.state is TaskState.approved
    provider = FakeProvider(quota_once={"developer"})
    assert await _process(_orchestrator(db, tmp_path, provider, FakeClock()), project) is True
    done = await repo.get_task(db, task.id)
    assert done is not None and done.state is TaskState.done
    assert provider.quota_hits == ["developer"]


# --- planning sessions, the bug fixer, notifications, surfaces ---------------------------


async def _planning(
    db: Database, tmp_path: Path, provider: FakeProvider, clock: FakeClock | BlockingClock
) -> tuple[PlanningOrchestrator, str, asyncio.Task[None]]:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="p", path=str(repo_path), default_branch="main"
    )
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    orchestrator = PlanningOrchestrator(db, EventBus(db), provider, quota_gate=gate)
    session = await orchestrator.create_and_start(project.id, "t", "Build feature X", 2)
    return orchestrator, session.id, orchestrator._active_sessions[session.id]


async def test_a_planning_turn_hitting_the_limit_waits_and_the_session_completes(
    db: Database, tmp_path: Path
) -> None:
    provider = FakeProvider(quota_once={"planning:planner"})
    _, session_id, loop = await _planning(db, tmp_path, provider, FakeClock())

    await asyncio.wait_for(loop, 10)

    session = await repo.get_planning_session(db, session_id)
    assert session is not None and session.status is PlanningSessionStatus.stable
    messages = await repo.list_planning_messages(db, session_id)
    assert not [m for m in messages if m.content.startswith("[Error")]
    assert len(await repo.list_planning_task_nodes(db, session_id)) == 3
    session_events = [
        e for e in await repo.list_events(db, project_id=session.project_id)
        if e.planning_session_id == session_id
    ]
    (waiting,) = _of(session_events, "quota.waiting")
    (resumed,) = _of(session_events, "quota.resumed")
    assert waiting.agent == resumed.agent == "planner"
    assert waiting.payload["node_id"] == "planning_planner"
    assert resumed.payload["resumed_session"] is True
    assert provider.profiles[1].resume_session_id == "ses-q-1"


async def test_cancelling_a_planning_session_ends_its_wait(
    db: Database, tmp_path: Path
) -> None:
    provider = FakeProvider(quota_once={"planning:planner"})
    clock = BlockingClock()
    orchestrator, session_id, loop = await _planning(db, tmp_path, provider, clock)
    await asyncio.wait_for(clock.sleeping.wait(), 10)

    await asyncio.wait_for(orchestrator.cancel_session(session_id), 5)

    assert loop.done()
    session = await repo.get_planning_session(db, session_id)
    assert session is not None and session.status is PlanningSessionStatus.cancelled
    assert len(provider.prompts) == 1


async def test_a_hunter_dispatch_hitting_the_limit_waits_and_records_its_find(
    db: Database, tmp_path: Path
) -> None:
    project = await repo.create_project(db, name="b", path=str(tmp_path), default_branch="main")
    await repo.upsert_coverage_region(db, project_id=project.id, region="src")
    claim = {"file": "src/app.py", "symbol": "f", "claim": "f breaks", "severity": "high"}
    provider = FakeProvider(
        quota_once={"hunter"}, hunter_text=f"```json\n{json.dumps(claim)}\n```"
    )
    clock = FakeClock()
    bus = EventBus(db)

    found = await discover_bug(
        db, bus, provider, project, ConclaveConfig(),
        quota_gate=QuotaGate(clock=clock.now, sleep=clock.sleep),
    )

    assert found is not None and found.claim == "f breaks"
    events = await repo.list_events(db, project_id=project.id)
    (waiting,) = _of(events, "quota.waiting")
    assert waiting.agent == "hunter" and waiting.task_id is None
    (resumed,) = _of(events, "quota.resumed")
    assert resumed.payload["resumed_session"] is True


async def test_a_wait_does_not_consume_the_bug_fixer_sessions_budget(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A two-hour wait for the session's hunter leaves its one-hour session budget unspent."""
    project = await repo.create_project(
        db, name="b", path=str(tmp_path), default_branch="main",
        config={"bug_fixer": {"wall_clock_budget_minutes": 60}},
    )
    await repo.set_project_mode(db, project.id, ProjectMode.autonomous_bug_fixer)
    await repo.upsert_coverage_region(db, project_id=project.id, region="src")
    claim = {"file": "src/app.py", "symbol": "f", "claim": "f breaks", "severity": "high"}
    provider = FakeProvider(
        quota_once={"hunter"},
        quota_resets_at=T0 + timedelta(hours=2),
        hunter_text=f"```json\n{json.dumps(claim)}\n```",
        developer_writes=False,  # the repro dispatch answers without a test: the cycle defers
    )
    clock = FakeClock()
    monkeypatch.setattr("conclave.runtime.time", _ShiftedTime(clock))
    worker = ProjectWorker(db, _orchestrator(db, tmp_path, provider, clock), project.id)
    started = time.monotonic()

    await worker._bug_fixer_tick(project)

    assert time.monotonic() - started < 10  # the wait took no real time
    assert clock.current >= T0 + timedelta(hours=2)  # but two hours on the gate's clock
    assert provider.quota_hits == ["hunter"]
    assert worker._session_elapsed() < 3600
    assert worker._session_budget is not None
    assert worker._session_budget.candidates_pursued == 1
    assert not worker._session_budget.exhausted(elapsed_seconds=worker._session_elapsed())
    assert worker._session_waits is not None
    assert worker._session_waits.seconds() >= 2 * 3600


class _RecordingSink:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def notify(self, payload: dict[str, Any]) -> None:
        self.payloads.append(payload)


async def test_a_wait_sends_one_webhook_notification(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, task = await _project_and_task(db, tmp_path)
    sink = _RecordingSink()

    def build_sink(config: ConclaveConfig) -> _RecordingSink:
        return sink

    monkeypatch.setattr(runner_module, "build_notification_sink", build_sink)
    provider = FakeProvider(quota_once={"developer"})

    assert await _process(_orchestrator(db, tmp_path, provider, FakeClock()), project) is True
    for _ in range(5):  # the delivery runs beside the wait
        await asyncio.sleep(0)

    (payload,) = sink.payloads  # only quota.waiting: the terminal one has its own sink
    assert payload["event"] == "quota.waiting"
    assert (payload["project_id"], payload["task_id"]) == (project.id, task.id)
    assert (payload["key"], payload["kind"]) == (_KEY, "rate_limit")
    assert payload["resumes_at"] is not None


async def test_the_gate_state_is_reported_over_rest_and_mcp(
    db: Database, tmp_path: Path
) -> None:
    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    resets_at = datetime.now(UTC) + timedelta(hours=1)
    reopens_at = daemon.quota_gate.close(
        "claude-code", "claude",
        QuotaExhausted(kind="rate_limit", resets_at=resets_at, window="five_hour", message="m"),
        retry_minutes=15,
    )
    info = {"status": "rejected", "resetsAt": int(resets_at.timestamp())}
    daemon.quota_gate.record_rate_limit("claude-code", "claude", info)
    expected = {
        "gates": [
            {
                "key": "claude-code",
                "engine": "claude",
                "closed": True,
                "resumes_at": reopens_at.isoformat(),
                "kind": "rate_limit",
                "window": "five_hour",
                "resets_at": resets_at.isoformat(),
                "message": "m",
                "rate_limit": info,
            }
        ]
    }

    app = create_app(daemon, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        res = await c.get("/api/engines/quota")
    assert res.status_code == 200 and res.json() == expected

    response = await handle_mcp_message(
        daemon,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "conclave_get_engine_quota", "arguments": {}},
        },
    )
    assert response is not None and response["result"]["isError"] is False
    assert json.loads(response["result"]["content"][0]["text"]) == expected


@pytest.mark.parametrize("minutes", [0, 1441, "soon"])
async def test_an_out_of_range_retry_setting_is_refused_with_4xx(
    db: Database, tmp_path: Path, minutes: object
) -> None:
    await _init_repo(tmp_path / "repo")
    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        created = await c.post(
            "/api/projects",
            json={"name": "q", "path": str(tmp_path / "repo"), "default_branch": "main"},
        )
        pid = created.json()["id"]
        res = await c.patch(
            f"/api/projects/{pid}/config",
            json={"config": {"execution": {"quota_retry_minutes": minutes}}},
        )
        assert 400 <= res.status_code < 500
        config = (await c.get(f"/api/projects/{pid}/config")).json()
    assert config["execution"]["quota_retry_minutes"] == 15
