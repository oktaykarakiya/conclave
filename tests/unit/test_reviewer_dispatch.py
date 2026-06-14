"""Unit tests for ENG-4: reviewer dispatch retries on a missing verdict, not exit status.

``Orchestrator._dispatch_reviewer`` must key its retry decision on whether a verdict can be
extracted, not on the provider's ``result.ok`` success-hint heuristic. A parseable verdict
whose process exited non-zero must be accepted on the first try (no wasted re-dispatch),
while a genuinely empty/timeout response must be retried up to the limit before giving up.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from conclave.db import Database, Task
from conclave.engine.orchestrator import _REVIEWER_DISPATCH_RETRIES, Orchestrator
from conclave.engine.runner import AgentRunner
from conclave.events import EventBus
from conclave.providers import AgentResult, Provider


class _FakeRunner:
    """AgentRunner stand-in: returns canned results (last repeats) and counts dispatches."""

    def __init__(self, results: list[AgentResult]) -> None:
        self._results = results
        self.calls = 0

    async def run(self, **kwargs: object) -> AgentResult:
        result = self._results[min(self.calls, len(self._results) - 1)]
        self.calls += 1
        return result


def _orchestrator(db: Database, home: Path) -> Orchestrator:
    # _dispatch_reviewer dispatches through the injected runner and never reads
    # self._provider, so a real provider is unnecessary here.
    return Orchestrator(db, EventBus(db), cast(Provider, None), home)


def _task() -> Task:
    return Task(
        id="task-1", project_id="proj-1", request="do the thing",
        created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
    )


async def test_parseable_verdict_not_redispatched_even_when_not_ok(
    db: Database, tmp_path: Path
) -> None:
    # A valid JSON verdict whose process exited non-zero (ok=False) is exactly the answer
    # we asked for: accept it on the first try instead of burning the retry budget.
    pass_json = '```json\n{"verdict": "pass", "reason": "looks right", "evidence": []}\n```'
    runner = _FakeRunner([AgentResult(ok=False, exit_code=1, text=pass_json)])
    orch = _orchestrator(db, tmp_path)

    result, verdict = await orch._dispatch_reviewer(
        cast(AgentRunner, runner), "reviewer", "review please", _task(), tmp_path, "", ""
    )

    assert runner.calls == 1
    assert verdict.verdict == "pass"
    assert verdict.source == "json"
    assert result.ok is False


async def test_empty_dispatch_retries_to_limit_then_no_verdict(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A genuinely empty/timeout response carries no verdict and no text: retry it up to the
    # limit, then surface a source='none' verdict for the caller's non-blocking 'unknown'.
    monkeypatch.setattr("conclave.engine.orchestrator._REVIEWER_RETRY_BACKOFF_S", 0.0)
    runner = _FakeRunner([AgentResult(ok=False, text="", error="timeout")])
    orch = _orchestrator(db, tmp_path)

    result, verdict = await orch._dispatch_reviewer(
        cast(AgentRunner, runner), "reviewer", "review please", _task(), tmp_path, "", ""
    )

    assert runner.calls == _REVIEWER_DISPATCH_RETRIES + 1
    assert verdict.source == "none"
    assert verdict.verdict == "unknown"
    assert result.text == ""
