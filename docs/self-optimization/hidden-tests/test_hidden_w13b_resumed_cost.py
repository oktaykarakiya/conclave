"""Hidden acceptance tests for W13 item 7: a resumed session's cost is counted once.

Written by the pilot from the spec alone, run against the task branch before merging; not part
of the repository's own suite. Measured on the live daemon (2026-11-17, W17's planner): the run a
five-hour limit interrupted reported ``total_cost_usd`` 4.52, and the resumed run 8.49. Claude
Code's ``total_cost_usd`` is the session's running total, the resumed run included, so Conclave
recorded 13.01 for a planner that cost 8.49. opencode's cost is the sum of the run's own steps.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from conclave.config import ArgMode
from conclave.providers import AgentResult, QuotaExhausted, QuotaGate, ResolvedProfile
from conclave.providers.quota import dispatch_with_quota

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX fake executables")

T0 = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
RESET = int((T0 + timedelta(hours=1)).timestamp())


class FakeClock:
    def __init__(self) -> None:
        self.current = T0

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)
        await asyncio.sleep(0)


@dataclass
class Recorder:
    """A QuotaListener that keeps the cost of each run a limit interrupted."""

    interrupted: list[float | None] = field(default_factory=list)

    async def waiting(self, wait: Any, interrupted: AgentResult | None) -> None:
        if interrupted is not None:
            self.interrupted.append(interrupted.cost_usd)

    async def resumed(self, wait: Any, *, waited_seconds: float, resumed_session: bool) -> None:
        return None


def _fake_claude(tmp_path: Path, refusals: int, run_costs: list[float]) -> Path:
    """A ``claude`` that refuses its first *refusals* runs, then completes.

    Run *n* adds ``run_costs[n]`` to its session's running total and reports that total as
    ``total_cost_usd``, as Claude Code does (a ``--resume`` continues the same session's total).
    An unknown ``--resume`` session prints Claude Code's error and adds nothing.
    """
    state = tmp_path / "state.json"
    script = tmp_path / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "sys.stdin.read()\n"
        f"state_path = {str(state)!r}\n"
        "try:\n"
        "    st = json.load(open(state_path))\n"
        "except FileNotFoundError:\n"
        "    st = {'runs': 0, 'totals': {}, 'forget': False}\n"
        "resume = args[args.index('--resume') + 1] if '--resume' in args else None\n"
        "if resume is not None and (st['forget'] or resume not in st['totals']):\n"
        "    print(json.dumps({'type': 'result', 'subtype': 'error_during_execution',\n"
        "        'is_error': True,\n"
        "        'result': 'No conversation found with session ID: ' + resume}))\n"
        "    sys.exit(1)\n"
        "n = st['runs']\n"
        "sid = resume or ('s-%d' % n)\n"
        f"total = round(st['totals'].get(sid, 0.0) + {run_costs!r}[n], 4)\n"
        "st['runs'] = n + 1\n"
        "st['totals'][sid] = total\n"
        "json.dump(st, open(state_path, 'w'))\n"
        "out = lambda e: print(json.dumps({**e, 'session_id': sid}), flush=True)\n"
        "out({'type': 'system', 'subtype': 'init', 'model': 'claude-test'})\n"
        f"if n < {refusals}:\n"
        f"    info = {{'status': 'rejected', 'resetsAt': {RESET}, 'rateLimitType': 'five_hour'}}\n"
        "    out({'type': 'rate_limit_event', 'rate_limit_info': info})\n"
        "    out({'type': 'assistant', 'error': 'rate_limit', 'message': {'content': [\n"
        "        {'type': 'text', 'text': \"You've hit your limit\"}]}})\n"
        "    out({'type': 'result', 'subtype': 'success', 'is_error': True, 'num_turns': 3,\n"
        "         'result': \"You've hit your limit\", 'total_cost_usd': total})\n"
        "    sys.exit(1)\n"
        "out({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'done'}]}})\n"
        "out({'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 2,\n"
        "     'result': 'done', 'total_cost_usd': total})\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


async def _dispatch(tmp_path: Path, refusals: int, run_costs: list[float]) -> tuple[
    AgentResult, Recorder
]:
    from conclave.providers.claude_cli import ClaudeCliProvider

    cli = _fake_claude(tmp_path, refusals, run_costs)
    clock = FakeClock()
    recorder = Recorder()
    profile = ResolvedProfile(
        name="claude-code", arg_mode=ArgMode.flag, cli=str(cli), model="claude-code/claude-test"
    )
    result = await dispatch_with_quota(
        ClaudeCliProvider(),
        QuotaGate(clock=clock.now, sleep=clock.sleep),
        profile=profile,
        prompt="do the task",
        timeout_seconds=30,
        retry_minutes=15,
        cwd=tmp_path,
        listener=recorder,
    )
    return result, recorder


async def test_a_resumed_claude_session_is_billed_once(tmp_path: Path) -> None:
    # The live case: refused at 4.52, resumed run adds 3.97, final running total 8.49.
    result, recorder = await _dispatch(tmp_path, refusals=1, run_costs=[4.52, 3.97])
    assert result.ok and result.session_id == "s-0"
    assert recorder.interrupted == [pytest.approx(4.52)]
    # Each run is recorded once: the usage rows (interrupted runs + final) sum to the session's
    # real total, not 4.52 + 8.49.
    assert sum(c or 0.0 for c in recorder.interrupted) + (result.cost_usd or 0.0) == (
        pytest.approx(8.49)
    )
    assert result.cost_usd == pytest.approx(3.97)


async def test_two_waits_on_one_session_still_sum_to_its_total(tmp_path: Path) -> None:
    result, recorder = await _dispatch(tmp_path, refusals=2, run_costs=[4.52, 1.48, 2.49])
    assert result.ok
    assert recorder.interrupted == [pytest.approx(4.52), pytest.approx(1.48)]
    assert result.cost_usd == pytest.approx(2.49)


async def test_a_fresh_session_after_an_unknown_resume_is_billed_in_full(tmp_path: Path) -> None:
    # The engine forgot the session: the fresh run starts its own running total at 0.
    from conclave.providers.claude_cli import ClaudeCliProvider

    cli = _fake_claude(tmp_path, refusals=1, run_costs=[4.52, 2.00])
    state = tmp_path / "state.json"
    clock = FakeClock()
    recorder = Recorder()

    class Forgetful(Recorder):
        async def waiting(self, wait: Any, interrupted: AgentResult | None) -> None:
            await super().waiting(wait, interrupted)
            st = json.loads(state.read_text())
            st["forget"] = True
            state.write_text(json.dumps(st))

    recorder = Forgetful()
    profile = ResolvedProfile(
        name="claude-code", arg_mode=ArgMode.flag, cli=str(cli), model="claude-code/claude-test"
    )
    result = await dispatch_with_quota(
        ClaudeCliProvider(),
        QuotaGate(clock=clock.now, sleep=clock.sleep),
        profile=profile,
        prompt="do the task",
        timeout_seconds=30,
        retry_minutes=15,
        cwd=tmp_path,
        listener=recorder,
    )
    assert result.ok and result.session_id == "s-1"
    assert recorder.interrupted == [pytest.approx(4.52)]
    assert result.cost_usd == pytest.approx(2.00)


class PerRunProvider:
    """An opencode-style provider: each run reports only its own cost."""

    default = "opencode"

    def __init__(self) -> None:
        self.calls = 0

    async def run_agent(self, *, profile: ResolvedProfile, prompt: str, timeout_seconds: int,
                        cwd: Path | None = None, **_: Any) -> AgentResult:
        self.calls += 1
        if self.calls == 1:
            return AgentResult(ok=False, error="limit", session_id="ses-1", cost_usd=1.25,
                               quota=QuotaExhausted(kind="rate_limit", resets_at=T0))
        return AgentResult(ok=True, text="done", session_id="ses-1", cost_usd=0.75)


async def test_per_run_costs_are_kept_as_reported(tmp_path: Path) -> None:
    clock = FakeClock()
    recorder = Recorder()
    result = await dispatch_with_quota(
        PerRunProvider(),  # type: ignore[arg-type]
        QuotaGate(clock=clock.now, sleep=clock.sleep),
        profile=ResolvedProfile(name="opencode", arg_mode=ArgMode.inherit,
                                model="deepseek/deepseek-flash"),
        prompt="do the task",
        timeout_seconds=30,
        retry_minutes=15,
        cwd=tmp_path,
        listener=recorder,
    )
    assert result.ok
    assert recorder.interrupted == [pytest.approx(1.25)]
    assert result.cost_usd == pytest.approx(0.75)
