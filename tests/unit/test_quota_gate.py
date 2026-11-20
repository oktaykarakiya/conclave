"""The usage-limit gate and the shared dispatch path that waits on it (``providers.quota``).

Deterministic: every gate runs on a fake clock, and its sleep either advances that clock or
blocks until the test moves it, so nothing ever waits for real.
"""

from __future__ import annotations

import ast
import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import conclave
from conclave.config import ArgMode
from conclave.providers import (
    CONTINUATION_PROMPT,
    AgentResult,
    Engine,
    OnChunk,
    QuotaExhausted,
    QuotaGate,
    QuotaWait,
    ResolvedProfile,
    WaitTracker,
    dispatch_with_quota,
    quota_key,
)
from conclave.providers.quota import RESUME_MARGIN
from waits import wait_until, within

T0 = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def _limit() -> QuotaExhausted:
    """Built per use, never at import: a hot-reload test swaps the conclave classes in place."""
    return QuotaExhausted(kind="rate_limit", window="five_hour", message="You've hit your limit")


class FakeClock:
    """A UTC clock that only moves when a gate sleeps on it (by the time slept)."""

    def __init__(self, start: datetime = T0) -> None:
        self.current = start
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.current += timedelta(seconds=seconds)
        await asyncio.sleep(0)


class SteppedClock:
    """A clock whose sleepers block until the test moves it with :meth:`advance`."""

    def __init__(self, start: datetime = T0) -> None:
        self.current = start
        self._moved = asyncio.Event()
        self.sleepers = 0

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        moved = self._moved
        self.sleepers += 1
        try:
            await moved.wait()
        finally:
            self.sleepers -= 1

    def advance(self, delta: timedelta) -> None:
        self.current += delta
        moved, self._moved = self._moved, asyncio.Event()
        moved.set()


async def _settle() -> None:
    """Yield a few times: time for a waiter the gate released by mistake to finish (a check that
    something does NOT happen)."""
    for _ in range(5):
        await asyncio.sleep(0)


def _profile(model: str | None = "deepseek/deepseek-flash") -> ResolvedProfile:
    return ResolvedProfile(name="opencode", arg_mode=ArgMode.inherit, model=model)


# --- closing and reopening ---------------------------------------------------------------


def test_a_reported_reset_closes_the_key_until_the_reset_plus_a_minute() -> None:
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    resets_at = T0 + timedelta(hours=2)

    reopens_at = gate.close(
        "claude-code", "claude", QuotaExhausted(kind="rate_limit", resets_at=resets_at),
        retry_minutes=15,
    )

    assert reopens_at == resets_at + timedelta(seconds=60)
    wait = gate.pending("claude-code")
    assert wait is not None and wait.resumes_at == reopens_at and wait.engine == "claude"
    clock.current = reopens_at - timedelta(seconds=1)
    assert gate.pending("claude-code") is not None
    clock.current = reopens_at
    assert gate.pending("claude-code") is None


def test_without_a_reset_time_the_key_closes_for_the_retry_minutes() -> None:
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)

    reopens_at = gate.close(
        "opencode/deepseek", "opencode", QuotaExhausted(kind="billing"), retry_minutes=15
    )

    assert reopens_at == T0 + timedelta(minutes=15)


def test_a_reset_time_already_past_falls_back_to_the_retry_minutes() -> None:
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    stale = QuotaExhausted(kind="rate_limit", resets_at=T0 - timedelta(minutes=5))

    assert gate.close("claude-code", "claude", stale, retry_minutes=7) == T0 + timedelta(
        minutes=7
    )


def test_a_reset_beyond_any_usage_window_is_ignored_for_the_retry_minutes() -> None:
    # E.g. a retry-after in milliseconds read as seconds: kept closed until then, only a cancel
    # or a restart would recover.
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    absurd = T0 + timedelta(days=30)

    reopens_at = gate.close(
        "claude-code", "claude", QuotaExhausted(kind="rate_limit", resets_at=absurd),
        retry_minutes=15,
    )

    assert reopens_at == T0 + timedelta(minutes=15)
    wait = gate.pending("claude-code")
    assert wait is not None and wait.resumes_at == reopens_at
    payload = wait.payload()
    assert payload["reset_ignored"] is True
    assert payload["resets_at"] == absurd.isoformat()  # what the engine reported, ignored


def test_a_weekly_reset_days_ahead_is_kept() -> None:
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    weekly = T0 + timedelta(days=6)

    reopens_at = gate.close(
        "claude-code", "claude",
        QuotaExhausted(kind="rate_limit", resets_at=weekly, window="seven_day"),
        retry_minutes=15,
    )

    assert reopens_at == weekly + RESUME_MARGIN
    wait = gate.pending("claude-code")
    assert wait is not None and wait.payload()["reset_ignored"] is False


async def test_a_dispatch_refused_with_an_absurd_reset_waits_the_retry_minutes() -> None:
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    absurd = QuotaExhausted(kind="rate_limit", resets_at=T0 + timedelta(days=30), message="x")
    provider = ScriptedProvider(clock, [_refused(quota=absurd), AgentResult(ok=True, text="ok")])
    listener = RecordingListener()

    result = await _dispatch(provider, gate, listener=listener)

    assert result.ok is True
    _first, resumed = provider.calls
    assert resumed.at == T0 + timedelta(minutes=15)  # _dispatch's retry_minutes
    (kind, waiting), _ = listener.events
    assert kind == "waiting" and waiting["reset_ignored"] is True


def test_a_closure_is_extended_but_never_shortened() -> None:
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    late = QuotaExhausted(kind="rate_limit", resets_at=T0 + timedelta(hours=3), window="seven_day")
    early = QuotaExhausted(kind="rate_limit", resets_at=T0 + timedelta(minutes=10))

    first = gate.close("claude-code", "claude", late, retry_minutes=15)
    assert gate.close("claude-code", "claude", early, retry_minutes=15) == first
    wait = gate.pending("claude-code")
    assert wait is not None and wait.quota.window == "seven_day"

    later = QuotaExhausted(kind="rate_limit", resets_at=T0 + timedelta(hours=5))
    assert gate.close("claude-code", "claude", later, retry_minutes=15) == (
        T0 + timedelta(hours=5, seconds=60)
    )


def test_keys_are_isolated() -> None:
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)

    gate.close("claude-code", "claude", _limit(), retry_minutes=15)
    assert gate.pending("opencode/deepseek") is None

    gate.close("opencode/deepseek", "opencode", _limit(), retry_minutes=15)
    assert gate.pending("opencode/openai") is None
    assert gate.pending("opencode/deepseek") is not None


@dataclass
class _Router:
    default: Engine


@pytest.mark.parametrize(
    ("model", "default", "expected"),
    [
        ("claude-code/opus", Engine.opencode, ("claude", "claude-code")),
        ("claude-code/default", Engine.opencode, ("claude", "claude-code")),
        (None, Engine.claude, ("claude", "claude-code")),
        ("deepseek/deepseek-flash", Engine.opencode, ("opencode", "opencode/deepseek")),
        ("deepseek/deepseek-reasoner", Engine.opencode, ("opencode", "opencode/deepseek")),
        ("openai/gpt-5", Engine.opencode, ("opencode", "opencode/openai")),
        (None, Engine.opencode, ("opencode", "opencode/default")),
    ],
)
def test_quota_key_is_one_per_engine_account(
    model: str | None, default: Engine, expected: tuple[str, str]
) -> None:
    assert quota_key(_Router(default), _profile(model)) == expected  # type: ignore[arg-type]


# --- waiting -----------------------------------------------------------------------------


async def test_two_waiters_are_released_together_when_the_key_reopens() -> None:
    clock = SteppedClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    gate.close("claude-code", "claude", _limit(), retry_minutes=5)
    first = asyncio.create_task(gate.wait("claude-code"))
    second = asyncio.create_task(gate.wait("claude-code"))
    await wait_until(lambda: clock.sleepers == 2, "the two waiters never both slept")

    clock.advance(timedelta(minutes=4))
    await _settle()
    assert not first.done() and not second.done()

    clock.advance(timedelta(minutes=1))
    released = await within(asyncio.gather(first, second), "the reopened key released nobody")
    assert released == [True, True]


async def test_a_wait_sleeps_in_chunks_of_at_most_a_minute() -> None:
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    gate.close(
        "claude-code", "claude",
        QuotaExhausted(kind="rate_limit", resets_at=T0 + timedelta(minutes=10)),
        retry_minutes=15,
    )

    assert await gate.wait("claude-code") is True

    assert clock.current == T0 + timedelta(minutes=11)
    assert max(clock.slept) <= 60 and len(clock.slept) == 11


async def test_a_cancel_ends_the_wait_at_once() -> None:
    clock = FakeClock()  # never advances on its own: only a real sleep would end this wait
    gate = QuotaGate(clock=clock.now)  # the real asyncio.sleep, in one-minute chunks
    gate.close("claude-code", "claude", _limit(), retry_minutes=600)
    cancel = asyncio.Event()
    asyncio.get_running_loop().call_later(0.05, cancel.set)
    started = time.monotonic()

    assert await gate.wait("claude-code", cancel) is False

    assert time.monotonic() - started < 1


async def test_claim_notification_is_true_once_per_closure_and_project() -> None:
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    assert gate.claim_notification("claude-code", "p1") is False  # nothing is closed

    gate.close("claude-code", "claude", _limit(), retry_minutes=5)
    assert gate.claim_notification("claude-code", "p1") is True
    assert gate.claim_notification("claude-code", "p1") is False
    assert gate.claim_notification("claude-code", "p2") is True
    gate.close("claude-code", "claude", _limit(), retry_minutes=10)  # extended: same closure
    assert gate.claim_notification("claude-code", "p1") is False

    clock.current += timedelta(minutes=11)
    gate.close("claude-code", "claude", _limit(), retry_minutes=5)  # a new closure
    assert gate.claim_notification("claude-code", "p1") is True


def test_snapshot_reports_closed_gates_and_rate_limit_info() -> None:
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    info = {"status": "allowed_warning", "resetsAt": 1794924600, "rateLimitType": "five_hour"}
    gate.record_rate_limit("claude-code", "claude", info)
    resets_at = T0 + timedelta(hours=1)
    gate.close(
        "opencode/deepseek", "opencode",
        QuotaExhausted(kind="rate_limit", resets_at=resets_at, message="429"),
        retry_minutes=15,
    )

    assert gate.snapshot() == [
        {
            "key": "claude-code",
            "engine": "claude",
            "closed": False,
            "resumes_at": None,
            "kind": None,
            "window": None,
            "resets_at": None,
            "reset_ignored": None,
            "message": None,
            "rate_limit": info,
        },
        {
            "key": "opencode/deepseek",
            "engine": "opencode",
            "closed": True,
            "resumes_at": (resets_at + timedelta(seconds=60)).isoformat(),
            "kind": "rate_limit",
            "window": None,
            "resets_at": resets_at.isoformat(),
            "reset_ignored": False,
            "message": "429",
            "rate_limit": None,
        },
    ]
    clock.current = resets_at + timedelta(minutes=2)
    assert [row["closed"] for row in gate.snapshot()] == [False, False]


def test_wait_tracker_counts_overlapping_waits_once() -> None:
    clock = FakeClock()
    waits = WaitTracker(clock.now)
    with waits.waiting():
        clock.current += timedelta(minutes=10)
        with waits.waiting():  # a second reviewer waits for the same reset
            clock.current += timedelta(minutes=20)
        assert waits.seconds() == 30 * 60
        clock.current += timedelta(minutes=5)
    clock.current += timedelta(hours=1)  # not waiting
    with waits.waiting():
        clock.current += timedelta(minutes=1)

    assert waits.seconds() == 36 * 60


def test_a_parent_tracker_counts_its_childrens_overlapping_waits_once() -> None:
    """A bug-fixer session's tracker spans the trackers of its tasks and project dispatches."""
    clock = FakeClock()
    session = WaitTracker(clock.now)
    task = WaitTracker(clock.now, parent=session)
    hunter = WaitTracker(clock.now, parent=session)
    with task.waiting():
        clock.current += timedelta(minutes=10)
        with hunter.waiting():  # waits for the same reset meanwhile
            clock.current += timedelta(minutes=20)
        assert session.seconds() == 30 * 60
        clock.current += timedelta(minutes=5)
    clock.current += timedelta(hours=1)  # nobody waits
    with hunter.waiting():
        clock.current += timedelta(minutes=1)

    assert task.seconds() == 35 * 60
    assert hunter.seconds() == 21 * 60
    assert session.seconds() == 36 * 60


# --- the shared dispatch path ------------------------------------------------------------


@dataclass
class _Call:
    profile: ResolvedProfile
    prompt: str
    at: datetime


@dataclass
class ScriptedProvider:
    """Answers each dispatch with the next scripted result, recording the call and its time."""

    clock: FakeClock | SteppedClock
    script: list[AgentResult]
    calls: list[_Call] = field(default_factory=list)

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
        self.calls.append(_Call(profile, prompt, self.clock.now()))
        return self.script.pop(0)


@dataclass
class RecordingListener:
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    fail: bool = False

    async def waiting(self, wait: QuotaWait, interrupted: AgentResult | None) -> None:
        self.events.append(("waiting", {**wait.payload(), "interrupted": interrupted}))
        if self.fail:
            raise RuntimeError("listener broke")

    async def resumed(
        self, wait: QuotaWait, *, waited_seconds: float, resumed_session: bool
    ) -> None:
        self.events.append(
            (
                "resumed",
                {"key": wait.key, "waited": waited_seconds, "resumed_session": resumed_session},
            )
        )
        if self.fail:
            raise RuntimeError("listener broke")


def _refused(
    session_id: str | None = "ses-1", quota: QuotaExhausted | None = None
) -> AgentResult:
    quota = quota if quota is not None else _limit()
    return AgentResult(ok=False, error=quota.message, quota=quota, session_id=session_id)


async def _dispatch(
    provider: ScriptedProvider,
    gate: QuotaGate,
    *,
    listener: RecordingListener | None = None,
    waits: WaitTracker | None = None,
    cancel_event: asyncio.Event | None = None,
    model: str = "deepseek/deepseek-flash",
) -> AgentResult:
    return await dispatch_with_quota(
        provider,  # type: ignore[arg-type]
        gate,
        profile=_profile(model),
        prompt="the original task",
        timeout_seconds=60,
        retry_minutes=15,
        cancel_event=cancel_event,
        listener=listener,
        waits=waits,
    )


async def test_a_limit_hit_waits_then_continues_the_reported_session() -> None:
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    resets_at = T0 + timedelta(hours=1)
    provider = ScriptedProvider(
        clock,
        [
            _refused(quota=QuotaExhausted(kind="rate_limit", resets_at=resets_at, message="x")),
            AgentResult(ok=True, text="done"),
        ],
    )
    listener = RecordingListener()

    result = await _dispatch(provider, gate, listener=listener)

    assert result.ok is True and result.text == "done"
    first, resumed = provider.calls
    assert (first.profile.resume_session_id, first.prompt) == (None, "the original task")
    assert resumed.profile.resume_session_id == "ses-1"
    assert resumed.prompt == CONTINUATION_PROMPT
    assert resumed.at == resets_at + timedelta(seconds=60)
    (kind, waiting), (_, done) = listener.events
    assert kind == "waiting"
    assert waiting["key"] == "opencode/deepseek" and waiting["engine"] == "opencode"
    assert waiting["resumes_at"] == (resets_at + timedelta(seconds=60)).isoformat()
    assert waiting["interrupted"] is not None and waiting["interrupted"].session_id == "ses-1"
    assert done == {"key": "opencode/deepseek", "waited": 3660.0, "resumed_session": True}


async def test_a_key_closed_by_another_dispatch_delays_this_one_until_it_reopens() -> None:
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    reopens_at = gate.close("opencode/deepseek", "opencode", _limit(), retry_minutes=15)
    provider = ScriptedProvider(clock, [AgentResult(ok=True, text="done")])
    listener = RecordingListener()

    result = await _dispatch(provider, gate, listener=listener)

    assert result.ok is True
    (call,) = provider.calls
    assert call.at >= reopens_at
    assert call.profile.resume_session_id is None and call.prompt == "the original task"
    assert [kind for kind, _ in listener.events] == ["waiting", "resumed"]
    assert listener.events[0][1]["interrupted"] is None
    assert listener.events[1][1]["resumed_session"] is False


async def test_an_unknown_session_falls_back_to_the_original_prompt() -> None:
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    provider = ScriptedProvider(
        clock,
        [
            _refused(),
            AgentResult(ok=False, error="No conversation found with session ID: ses-1"),
            AgentResult(ok=True, text="fresh"),
        ],
    )

    result = await _dispatch(provider, gate)

    assert result.text == "fresh"
    _, resume, fresh = provider.calls
    assert resume.profile.resume_session_id == "ses-1"
    assert fresh.profile.resume_session_id is None and fresh.prompt == "the original task"


async def test_a_second_limit_hit_waits_again() -> None:
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    provider = ScriptedProvider(
        clock, [_refused(), _refused(), _refused(), AgentResult(ok=True, text="done")]
    )
    listener = RecordingListener()
    waits = WaitTracker(clock.now)

    result = await _dispatch(provider, gate, listener=listener, waits=waits)

    assert result.ok is True
    assert [kind for kind, _ in listener.events] == ["waiting", "resumed"] * 3
    resumed = [c.profile.resume_session_id for c in provider.calls]
    assert resumed == [None, "ses-1", "ses-1", "ses-1"]
    assert waits.seconds() == 3 * 15 * 60


async def test_a_cancel_during_the_wait_returns_cancelled_without_another_run() -> None:
    clock = SteppedClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    provider = ScriptedProvider(clock, [_refused(), AgentResult(ok=True, text="never")])
    cancel = asyncio.Event()
    dispatch = asyncio.create_task(_dispatch(provider, gate, cancel_event=cancel))
    await wait_until(lambda: clock.sleepers == 1, "the dispatch never waited for the reset")

    cancel.set()
    result = await within(dispatch, "the cancelled dispatch never returned")

    assert result.cancelled is True and result.ok is False and result.error == "cancelled"
    assert len(provider.calls) == 1


async def test_a_failing_listener_never_breaks_the_dispatch() -> None:
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    provider = ScriptedProvider(clock, [_refused(), AgentResult(ok=True, text="done")])

    result = await _dispatch(provider, gate, listener=RecordingListener(fail=True))

    assert result.ok is True


async def test_allowed_warning_only_updates_the_rate_limit_info() -> None:
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    info = {"status": "allowed_warning", "resetsAt": 1794924600, "rateLimitType": "seven_day"}
    provider = ScriptedProvider(clock, [AgentResult(ok=True, text="done", rate_limit_info=info)])
    listener = RecordingListener()

    result = await _dispatch(provider, gate, listener=listener, model="claude-code/opus")

    assert result.ok is True and listener.events == [] and clock.slept == []
    (row,) = gate.snapshot()
    assert (row["key"], row["engine"]) == ("claude-code", "claude")
    assert row["closed"] is False and row["rate_limit"] == info


# --- a resumed session's cost --------------------------------------------------------------
# Claude Code reports total_cost_usd as the session's running total, and a --resume run
# continues it (measured: 4.52 refused, then 8.49 after the resume, for a planner that cost 8.49).


def _run_cost(
    cost: float | None, *, refused: bool, session_total: bool = True, session: str = "ses-1"
) -> AgentResult:
    quota = _limit() if refused else None
    return AgentResult(
        ok=not refused,
        text="" if refused else "done",
        error=quota.message if quota is not None else None,
        quota=quota,
        session_id=session,
        cost_usd=cost,
        cost_is_session_total=session_total,
    )


def _interrupted_costs(listener: RecordingListener) -> list[float | None]:
    return [
        event["interrupted"].cost_usd
        for kind, event in listener.events
        if kind == "waiting" and event["interrupted"] is not None
    ]


async def test_a_resumed_session_total_is_charged_as_the_resumed_runs_own_share() -> None:
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    script = [_run_cost(4.52, refused=True), _run_cost(8.49, refused=False)]
    provider = ScriptedProvider(clock, list(script))
    listener = RecordingListener()

    result = await _dispatch(provider, gate, listener=listener)

    assert result.ok is True and result.text == "done"
    assert _interrupted_costs(listener) == [4.52]
    assert result.cost_usd == pytest.approx(3.97)
    assert result.cost_is_session_total is True
    # The provider's own results are left as they were: the shares are copies.
    assert [r.cost_usd for r in script] == [4.52, 8.49]


async def test_two_waits_in_one_session_charge_each_run_its_share() -> None:
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    provider = ScriptedProvider(
        clock,
        [
            _run_cost(4.52, refused=True),
            _run_cost(6.00, refused=True),
            _run_cost(8.49, refused=False),
        ],
    )
    listener = RecordingListener()

    result = await _dispatch(provider, gate, listener=listener)

    assert _interrupted_costs(listener) == [4.52, pytest.approx(1.48)]
    assert result.cost_usd == pytest.approx(2.49)
    assert sum(c or 0.0 for c in _interrupted_costs(listener)) + (
        result.cost_usd or 0.0
    ) == pytest.approx(8.49)


async def test_a_run_reporting_no_cost_keeps_the_last_known_total() -> None:
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    provider = ScriptedProvider(
        clock,
        [
            _run_cost(4.52, refused=True),
            _run_cost(None, refused=True),  # stopped before its result event
            _run_cost(8.49, refused=False),
        ],
    )
    listener = RecordingListener()

    result = await _dispatch(provider, gate, listener=listener)

    assert _interrupted_costs(listener) == [4.52, None]
    assert result.cost_usd == pytest.approx(3.97)


async def test_a_fresh_session_after_an_unknown_one_is_charged_in_full() -> None:
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    provider = ScriptedProvider(
        clock,
        [
            _run_cost(4.52, refused=True),
            AgentResult(
                ok=False,
                error="No conversation found with session ID: ses-1",
                cost_is_session_total=True,
            ),
            _run_cost(3.00, refused=False, session="ses-2"),
        ],
    )

    result = await _dispatch(provider, gate)

    assert [c.profile.resume_session_id for c in provider.calls] == [None, "ses-1", None]
    assert result.cost_usd == 3.00


async def test_a_resumed_total_below_the_reported_one_is_charged_in_full() -> None:
    """The engine did not restore the session's totals (the interrupted run was stopped before
    it saved them), so the resumed run's total is its own cost."""
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    provider = ScriptedProvider(
        clock, [_run_cost(4.52, refused=True), _run_cost(3.00, refused=False)]
    )
    listener = RecordingListener()

    result = await _dispatch(provider, gate, listener=listener)

    assert _interrupted_costs(listener) == [4.52]
    assert result.cost_usd == 3.00
    assert [c.profile.resume_session_id for c in provider.calls] == [None, "ses-1"]


async def test_a_restarted_total_is_where_the_running_total_continues_from() -> None:
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    provider = ScriptedProvider(
        clock,
        [
            _run_cost(4.52, refused=True),
            _run_cost(2.00, refused=True),  # restarted: its own 2.00
            _run_cost(3.50, refused=False),  # restored from 2.00: 1.50 of its own
        ],
    )
    listener = RecordingListener()

    result = await _dispatch(provider, gate, listener=listener)

    assert _interrupted_costs(listener) == [4.52, 2.00]
    assert result.cost_usd == pytest.approx(1.50)


async def test_per_run_costs_pass_through_unchanged() -> None:
    """An engine whose cost is each run's own (opencode sums its own run's steps)."""
    clock = FakeClock()
    gate = QuotaGate(clock=clock.now, sleep=clock.sleep)
    provider = ScriptedProvider(
        clock,
        [
            _run_cost(0.40, refused=True, session_total=False),
            _run_cost(0.30, refused=False, session_total=False),
        ],
    )
    listener = RecordingListener()

    result = await _dispatch(provider, gate, listener=listener)

    assert _interrupted_costs(listener) == [0.40]
    assert result.cost_usd == 0.30


# --- only the shared dispatch path continues a session ------------------------------------

_SRC = Path(conclave.__file__).resolve().parent


def _sets_resume_session_id(tree: ast.AST) -> list[int]:
    """Lines of *tree* that set ``resume_session_id``: a keyword argument (``replace``, a
    profile's constructor), an assigned attribute, or a string naming it (``setattr``, a
    ``model_copy`` update). Reading it is fine."""
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if (isinstance(node, ast.keyword) and node.arg == "resume_session_id")
        or (
            isinstance(node, ast.Attribute)
            and node.attr == "resume_session_id"
            and isinstance(node.ctx, ast.Store)
        )
        or (isinstance(node, ast.Constant) and node.value == "resume_session_id")
    )


def test_the_guard_finds_every_way_to_set_resume_session_id() -> None:
    sample = (
        "replace(profile, resume_session_id='s')\n"
        "profile.resume_session_id = 's'\n"
        "setattr(profile, 'resume_session_id', 's')\n"
        "profile.model_copy(update={'resume_session_id': 's'})\n"
        "print(profile.resume_session_id)\n"
    )
    assert _sets_resume_session_id(ast.parse(sample)) == [1, 2, 3, 4]


def test_no_caller_outside_the_providers_continues_a_session() -> None:
    """Callers start fresh sessions; continuing one is dispatch_with_quota's job, since it knows
    the running totals of only the runs it made (see its docstring)."""
    offenders = [
        f"{path.relative_to(_SRC)}:{line}"
        for path in sorted(_SRC.rglob("*.py"))
        if "__pycache__" not in path.parts and path.relative_to(_SRC).parts[0] != "providers"
        for line in _sets_resume_session_id(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert offenders == []
