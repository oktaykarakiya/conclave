"""A mandatory reviewer must render a verdict, and a failed planner is retried and reported.

The tests run the real orchestrator against a throwaway git repo and the deterministic
FakeProvider (zero LLM cost), whose ``replies`` script one role's answers while every other role
answers as usual. The retry backoff the reviewers and the planner share is patched to zero.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from fake_provider import FakeProvider

from conclave.config import ConclaveConfig, load_project_config, workflow_templates
from conclave.db import CheckpointStatus, Database, EventRow, Task, TaskState
from conclave.db import repositories as repo
from conclave.engine import Orchestrator, run_git
from conclave.engine.orchestrator import (
    _REVIEWER_DISPATCH_RETRIES,
    _VERDICT_REMINDER,
    _PlannerOutcome,
)
from conclave.engine.project_rules import ProjectRules
from conclave.engine.runner import AgentRunner
from conclave.events import EventBus
from conclave.providers import AgentResult, OnChunk, Provider, ResolvedProfile

_TRIES = _REVIEWER_DISPATCH_RETRIES + 1
_PASS = AgentResult(
    ok=True,
    text='```json\n{"verdict": "pass", "reason": "looks correct", "evidence": []}\n```',
    model_reported="fake",
)
_PROSE = AgentResult(ok=True, text="The change looks reasonable to me.", model_reported="fake")
_CRASH = AgentResult(ok=False, text="", error="exit 1: security crashed")
_PLAN_TEXT = '{"approach": "create the file", "files_to_touch": ["FEATURE.txt"]}'
_PLAN = AgentResult(ok=True, text=f"```json\n{_PLAN_TEXT}\n```", model_reported="fake")
_PLAN_HEADER = "=== PLAN (from the Planner — follow its scope strictly) ===\n"
_PLANNER_CRASH = AgentResult(ok=False, text="", error="planner crashed")
_FEEDBACK = "LATEST REVIEWER FEEDBACK (must fix these issues):\n"
_AGENT = re.compile(r"You are the (\S+) agent of")
_REVIEWERS = ("tester", "security", "reviewer")


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("conclave.engine.orchestrator._REVIEWER_RETRY_BACKOFF_S", 0.0)


async def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    await run_git(path, "config", "user.email", "test@example.com")
    await run_git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# test repo\n", encoding="utf-8")
    await run_git(path, "add", "-A")
    await run_git(path, "commit", "-m", "initial commit")


async def _run_task(
    db: Database,
    tmp_path: Path,
    provider: FakeProvider,
    config: dict[str, Any] | None = None,
    *,
    cancel_event: asyncio.Event | None = None,
) -> tuple[Task, bool]:
    """Process one approved task on a fresh ``tmp_path/repo`` with *config*; return it + result.

    The repo has no test suite, so the task runs without a test command deliberately
    (``allow_no_test_command``) instead of failing up front.
    """
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
    ok = await orchestrator.process_task(claimed, cancel_event=cancel_event)
    final = await repo.get_task(db, task.id)
    assert final is not None
    return final, ok


def _agents(provider: FakeProvider) -> list[str]:
    """The agent of every dispatch, in order (no seeded personas: the system line names it)."""
    return [m.group(1) if (m := _AGENT.search(p)) else "?" for p in provider.prompts]


def _prompts_of(provider: FakeProvider, agent: str) -> list[str]:
    return [p for p in provider.prompts if f"You are the {agent} agent of" in p]


async def _events(db: Database, task_id: str, type_: str) -> list[EventRow]:
    return [e for e in await repo.list_events(db, task_id=task_id, limit=10_000) if e.type == type_]


async def _warnings(db: Database, task_id: str, agent: str) -> list[str]:
    """The ``grounding.warning`` texts emitted for *agent*."""
    events = await _events(db, task_id, "grounding.warning")
    return [str(e.payload["warning"]) for e in events if e.agent == agent]


async def _planner_failed(db: Database, task_id: str) -> list[dict[str, Any]]:
    return [
        e.payload for e in await _events(db, task_id, "log")
        if e.payload.get("stage") == "planner_failed"
    ]


class _CancelAsItReturns(FakeProvider):
    """FakeProvider that sets the task's cancel event as *role*'s first reply comes back."""

    def __init__(self, role: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.cancel_role = role

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
        if self.role_of(prompt) == self.cancel_role and cancel_event is not None:
            cancel_event.set()
        return result


# --- mandatory reviewers ------------------------------------------------------------------


async def test_a_mandatory_reviewer_without_a_verdict_is_re_run_with_a_reminder(
    db: Database, tmp_path: Path
) -> None:
    provider = FakeProvider(replies={"reviewer:security": [_PROSE, _PASS]})

    task, ok = await _run_task(db, tmp_path, provider)

    assert ok is True and task.state is TaskState.done
    assert task.result_summary == "completed in 1 attempt(s); merged=False"
    first, second = _prompts_of(provider, "security")
    assert _VERDICT_REMINDER not in first
    # The re-run is the same prompt, ending with the reminder.
    assert second == first.removesuffix("\n") + f"\n\n{_VERDICT_REMINDER}\n"
    assert len(_prompts_of(provider, "tester")) == 1
    security = [v for v in await repo.list_verdicts(db, task.id) if v.agent == "security"]
    assert [(v.verdict, v.source) for v in security] == [("pass", "json")]
    assert await _warnings(db, task.id, "security") == []


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (_PROSE, "security replied without a verdict block"),
        (_CRASH, "security agent unavailable after retries: exit 1: security crashed"),
    ],
    ids=["no-verdict-block", "crash"],
)
async def test_a_mandatory_reviewer_that_never_renders_a_verdict_fails_the_task(
    db: Database, tmp_path: Path, reply: AgentResult, reason: str
) -> None:
    provider = FakeProvider(replies={"reviewer:security": [reply]})

    task, ok = await _run_task(
        db, tmp_path, provider, {"agent_overrides": {"developer": {"max_retries": 2}}}
    )

    assert ok is False and task.state is TaskState.failed
    why = (
        f"inconclusive: mandatory reviewer(s) SECURITY gave no usable verdict (SECURITY: {reason})"
    )
    assert task.result_summary == f"failed (max_retries) after 2 attempt(s); last: {why}"
    assert [e.payload for e in await _events(db, task.id, "attempt.failed")] == [
        {"n": 1, "stage": "review", "reason": why},
        {"n": 2, "stage": "review", "reason": why},
    ]
    types = {e.type for e in await repo.list_events(db, task_id=task.id, limit=10_000)}
    assert not {"task.committed", "task.merged", "task.done"} & types
    assert task.branch is None
    assert len(_prompts_of(provider, "security")) == 2 * _TRIES
    developer = _prompts_of(provider, "developer")
    assert len(developer) == 2
    feedback = developer[1].split(_FEEDBACK, 1)[1]
    assert feedback.startswith("\nREVIEW INCONCLUSIVE: mandatory reviewer(s)")
    assert f"\n- SECURITY: {reason}\n" in feedback
    security = [v for v in await repo.list_verdicts(db, task.id) if v.agent == "security"]
    assert [(v.attempt, v.verdict, v.source, v.reason) for v in security] == [
        (1, "unknown", "none", reason),
        (2, "unknown", "none", reason),
    ]
    warnings = await _warnings(db, task.id, "security")
    assert len(warnings) == 2 and all("is a mandatory reviewer" in w for w in warnings)


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel"])
async def test_a_review_round_fails_naming_the_mandatory_reviewer_and_its_last_error(
    db: Database, tmp_path: Path, parallel: bool
) -> None:
    project = await repo.create_project(
        db, name="t", path=str(tmp_path), default_branch="main",
        config={"execution": {"target_branch": "main", "parallel_reviewers": parallel}},
    )
    task = await repo.create_task(db, project_id=project.id, request="add a feature file")
    config = load_project_config(project.config)
    unfinished = AgentResult(ok=False, text="I could not finish.", error="exit 2: tool error")
    provider = FakeProvider(replies={"reviewer:security": [unfinished]})
    runner = AgentRunner(db, EventBus(db), provider, project.id, config)
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")

    failed, feedback, timed_out = await orchestrator._review(
        runner, task, project.id, tmp_path, "diff --git a/x.py b/x.py\n", 1, config, "",
        ProjectRules(), started=time.monotonic(), budget=0.0, diff_cmd="git diff --cached HEAD",
    )

    assert (failed, timed_out) == (True, False)
    assert feedback == (
        "\nREVIEW INCONCLUSIVE: mandatory reviewer(s) gave no usable verdict after "
        f"{_TRIES} tries each:\n"
        "- SECURITY: security replied without a verdict block (error: exit 2: tool error)\n"
        "Not merging without every mandatory verdict; retrying.\n"
    )
    assert len(_prompts_of(provider, "security")) == _TRIES
    verdicts = {v.agent: (v.verdict, v.source) for v in await repo.list_verdicts(db, task.id)}
    assert verdicts == {
        "tester": ("pass", "json"),
        "security": ("unknown", "none"),
        "reviewer": ("pass", "json"),
    }


async def test_an_optional_reviewer_without_a_verdict_warns_and_does_not_block(
    db: Database, tmp_path: Path
) -> None:
    provider = FakeProvider(replies={"reviewer:architect": [_PROSE]})

    task, ok = await _run_task(db, tmp_path, provider)

    assert ok is True and task.state is TaskState.done
    (derived,) = await _events(db, task.id, "pipeline.derived")
    assert derived.payload["pipeline"] == ["tester", "security", "reviewer", "architect"]
    (architect,) = _prompts_of(provider, "architect")
    assert _VERDICT_REMINDER not in architect
    verdicts = [v for v in await repo.list_verdicts(db, task.id) if v.agent == "architect"]
    assert [(v.verdict, v.source) for v in verdicts] == [("unknown", "none")]
    assert await _warnings(db, task.id, "architect") == [
        "architect replied without a verdict block; recorded unknown (non-blocking)"
    ]


async def test_a_cancel_during_a_mandatory_reviewers_re_run_stops_the_task(
    db: Database, tmp_path: Path
) -> None:
    provider = _CancelAsItReturns("reviewer:security", replies={"reviewer:security": [_PROSE]})

    task, ok = await _run_task(db, tmp_path, provider, cancel_event=asyncio.Event())

    assert ok is False and task.state is TaskState.cancelled
    # Security answered once; nothing was dispatched after it (no re-run, reviewer or retry).
    assert _agents(provider) == ["planner", "developer", "tester", "security"]
    types = [e.type for e in await repo.list_events(db, task_id=task.id, limit=10_000)]
    assert "task.failed" not in types and types.count("task.cancelled") == 1


class _RecordingRunner:
    """AgentRunner stand-in: answers every dispatch with *reply* and records its prompt."""

    def __init__(self, reply: AgentResult, on_call: Callable[[], object] | None = None) -> None:
        self.reply = reply
        self.on_call = on_call
        self.prompts: list[str] = []

    async def run(self, *, prompt: str, **kwargs: object) -> AgentResult:
        self.prompts.append(prompt)
        if self.on_call is not None:
            self.on_call()
        return self.reply


def _task() -> Task:
    return Task(
        id="task-1", project_id="proj-1", request="do the thing",
        created_at="2026-01-01T00:00:00Z", updated_at="2026-01-01T00:00:00Z",
    )


async def test_dispatch_re_runs_a_mandatory_reviewer_with_the_reminder_up_to_the_limit(
    db: Database, tmp_path: Path
) -> None:
    runner = _RecordingRunner(_PROSE)
    orchestrator = Orchestrator(db, EventBus(db), cast(Provider, None), tmp_path)

    _, verdict = await orchestrator._dispatch_reviewer(
        cast(AgentRunner, runner), "security", "review please", _task(), tmp_path, "",
        ProjectRules(), mandatory=True,
    )

    assert runner.prompts == ["review please"] + [
        f"review please\n\n{_VERDICT_REMINDER}"
    ] * _REVIEWER_DISPATCH_RETRIES
    assert verdict.source == "none"


async def test_dispatch_checks_the_cancel_after_the_backoff_before_a_re_run(
    db: Database, tmp_path: Path
) -> None:
    # The cancel lands after the verdict-less reply came back, while the re-run waits.
    cancel_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    runner = _RecordingRunner(_PROSE, on_call=lambda: loop.call_soon(cancel_event.set))
    orchestrator = Orchestrator(db, EventBus(db), cast(Provider, None), tmp_path)

    await orchestrator._dispatch_reviewer(
        cast(AgentRunner, runner), "security", "review please", _task(), tmp_path, "",
        ProjectRules(), cancel_event=cancel_event, mandatory=True,
    )

    assert runner.prompts == ["review please"]
    assert cancel_event.is_set()


# --- the planner ----------------------------------------------------------------------------


async def test_a_failing_planner_is_retried_and_its_plan_is_used(
    db: Database, tmp_path: Path
) -> None:
    provider = FakeProvider(
        replies={
            "planner": [
                AgentResult(ok=False, text="", error="exit 1: planner crashed"),
                AgentResult(ok=True, text="I would create the file.", model_reported="fake"),
                _PLAN,
            ]
        }
    )

    task, ok = await _run_task(db, tmp_path, provider)

    assert ok is True and task.state is TaskState.done
    planner = _prompts_of(provider, "planner")
    assert len(planner) == 3 and len(set(planner)) == 1  # the same prompt each try
    assert len(await _events(db, task.id, "plan.artifact")) == 1
    assert task.plan == {"approach": "create the file", "files_to_touch": ["FEATURE.txt"]}
    (developer,) = _prompts_of(provider, "developer")
    assert f"{_PLAN_HEADER}```json\n{_PLAN_TEXT}\n```" in developer
    assert await _planner_failed(db, task.id) == []
    assert task.result_summary == "completed in 1 attempt(s); merged=False"


async def test_a_planner_that_always_fails_is_reported_and_the_developer_runs_without_a_plan(
    db: Database, tmp_path: Path
) -> None:
    provider = FakeProvider(replies={"planner": [_PLANNER_CRASH]})

    task, ok = await _run_task(
        db, tmp_path, provider, {"agent_overrides": {"planner": {"max_retries": 2}}}
    )

    assert ok is True and task.state is TaskState.done
    assert len(_prompts_of(provider, "planner")) == 2
    (failed,) = await _planner_failed(db, task.id)
    assert (failed["attempts"], failed["error"], failed["agent"], failed["node_id"]) == (
        2, "planner crashed", "planner", "planner"
    )
    assert "planner crashed" in failed["message"]
    assert await _events(db, task.id, "plan.artifact") == []
    assert task.plan is None
    (developer,) = _prompts_of(provider, "developer")
    assert "=== PLAN" not in developer
    assert task.result_summary == (
        "completed in 1 attempt(s); merged=False; planned: no (planner failed: planner crashed)"
    )
    planner_cps = [
        cp for cp in await repo.list_task_checkpoints(db, task.id) if cp.node_id == "planner"
    ]
    assert [cp.status for cp in planner_cps] == [CheckpointStatus.failed]


async def test_a_failed_task_summary_also_says_it_ran_without_a_plan(
    db: Database, tmp_path: Path
) -> None:
    long_error = "trace line\n" * 100 + "the planner gave up"
    provider = FakeProvider(
        replies={
            "planner": [AgentResult(ok=False, text="", error=long_error)],
            "reviewer:security": [_CRASH],
        }
    )

    task, ok = await _run_task(
        db, tmp_path, provider,
        {"agent_overrides": {"planner": {"max_retries": 1}, "developer": {"max_retries": 1}}},
    )

    assert ok is False and task.state is TaskState.failed
    summary = task.result_summary or ""
    prefix = "failed (max_retries) after 1 attempt(s); planned: no (planner failed: ..."
    # The planner note, then why the last attempt failed.
    noted, last = summary.split("; last: ")
    assert noted.startswith(prefix) and noted.endswith("trace line the planner gave up)")
    assert last.startswith("inconclusive: mandatory reviewer(s) SECURITY gave no usable verdict")
    # One line, each part bounded; the event keeps the whole error.
    assert "\n" not in summary
    assert len(noted) <= len(prefix) + 300 and len(last) <= 300
    (failed,) = await _planner_failed(db, task.id)
    assert failed["error"] == long_error


@pytest.mark.parametrize(
    "config",
    [
        {"experimental": {"planner_enabled": False}},
        {"workflow": workflow_templates()["fast_track"].model_dump(mode="json")},
    ],
    ids=["planner-disabled", "workflow-without-a-planner"],
)
async def test_without_an_enabled_planner_nothing_changes(
    db: Database, tmp_path: Path, config: dict[str, Any]
) -> None:
    provider = FakeProvider(replies={"planner": [_PLANNER_CRASH]})

    task, ok = await _run_task(db, tmp_path, provider, config)

    assert ok is True and task.state is TaskState.done
    assert _prompts_of(provider, "planner") == []
    assert await _planner_failed(db, task.id) == []
    assert "planned:" not in (task.result_summary or "")


async def test_a_cancel_during_a_planner_retry_stops_the_task(
    db: Database, tmp_path: Path
) -> None:
    provider = _CancelAsItReturns("planner", replies={"planner": [_PLANNER_CRASH]})

    task, ok = await _run_task(db, tmp_path, provider, cancel_event=asyncio.Event())

    assert ok is False and task.state is TaskState.cancelled
    assert _agents(provider) == ["planner"]
    assert await _planner_failed(db, task.id) == []
    types = [e.type for e in await repo.list_events(db, task_id=task.id, limit=10_000)]
    assert "task.failed" not in types and types.count("task.cancelled") == 1
    planner_cps = [
        cp for cp in await repo.list_task_checkpoints(db, task.id) if cp.node_id == "planner"
    ]
    assert [cp.status for cp in planner_cps] == [CheckpointStatus.skipped]


async def test_planner_retry_checks_the_cancel_after_the_backoff(
    db: Database, tmp_path: Path
) -> None:
    # The cancel lands after the failed try came back, while the retry waits.
    project = await repo.create_project(db, name="p", path=str(tmp_path), default_branch="main")
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.in_progress
    )
    cancel_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    runner = _RecordingRunner(_PLANNER_CRASH, on_call=lambda: loop.call_soon(cancel_event.set))
    orchestrator = Orchestrator(db, EventBus(db), cast(Provider, None), tmp_path)
    outcome = _PlannerOutcome()

    preamble = await orchestrator._maybe_plan(
        cast(AgentRunner, runner), task, tmp_path, "", ProjectRules(), None, ConclaveConfig(),
        cancel_event=cancel_event, outcome=outcome,
    )

    assert preamble == ""
    assert len(runner.prompts) == 1
    assert outcome.failure is None
    assert await _planner_failed(db, task.id) == []


# --- why an attempt failed --------------------------------------------------------------------


def _verdict(verdict: str, reason: str) -> AgentResult:
    body = f'{{"verdict": "{verdict}", "reason": {json.dumps(reason)}, "evidence": []}}'
    return AgentResult(ok=True, text=f"```json\n{body}\n```", model_reported="fake")


async def _failure_payloads(db: Database, task_id: str) -> list[dict[str, Any]]:
    return [e.payload for e in await _events(db, task_id, "attempt.failed")]


async def _pipeline(db: Database, task_id: str) -> list[str]:
    (derived,) = await _events(db, task_id, "pipeline.derived")
    return [str(agent) for agent in derived.payload["pipeline"]]


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel"])
async def test_a_rejecting_reviewer_is_named_with_its_reason(
    db: Database, tmp_path: Path, parallel: bool
) -> None:
    provider = FakeProvider(
        replies={"reviewer:security": [_verdict("fail", "the input is never\nvalidated")]}
    )

    task, ok = await _run_task(
        db, tmp_path, provider,
        {
            "execution": {"parallel_reviewers": parallel},
            "agent_overrides": {"developer": {"max_retries": 2}},
        },
    )

    assert ok is False and task.state is TaskState.failed
    why = "rejected by SECURITY (fail): the input is never validated"
    assert await _failure_payloads(db, task.id) == [
        {"n": 1, "stage": "review", "reason": why},
        {"n": 2, "stage": "review", "reason": why},
    ]
    assert task.result_summary == f"failed (max_retries) after 2 attempt(s); last: {why}"


async def test_parallel_reviewers_rejecting_together_are_all_named(
    db: Database, tmp_path: Path
) -> None:
    provider = FakeProvider(
        replies={
            "reviewer:tester": [_verdict("fail", "no test covers the empty file")],
            "reviewer:security": [_verdict("block", "the path is not sanitized")],
        }
    )

    task, _ = await _run_task(
        db, tmp_path, provider,
        {
            "execution": {"parallel_reviewers": True},
            "agent_overrides": {"developer": {"max_retries": 1}},
        },
    )

    assert await _failure_payloads(db, task.id) == [
        {
            "n": 1,
            "stage": "review",
            "reason": (
                "rejected by TESTER (fail): no test covers the empty file; "
                "rejected by SECURITY (block): the path is not sanitized"
            ),
        }
    ]


async def test_no_usable_verdict_at_all_is_said_so(db: Database, tmp_path: Path) -> None:
    # No mandatory reviewer: the new file brings in the optional architect alone, who never
    # renders a verdict.
    provider = FakeProvider(replies={"reviewer:architect": [_PROSE]})

    task, _ = await _run_task(
        db, tmp_path, provider,
        {"agents": {"mandatory": []}, "agent_overrides": {"developer": {"max_retries": 1}}},
    )

    assert await _pipeline(db, task.id) == ["architect"]
    assert await _failure_payloads(db, task.id) == [
        {"n": 1, "stage": "review", "reason": "no reviewer returned a usable verdict"}
    ]
    assert task.result_summary == (
        "failed (max_retries) after 1 attempt(s); last: no reviewer returned a usable verdict"
    )


async def test_long_reviewer_reasons_give_a_one_line_reason_of_at_most_300_chars(
    db: Database, tmp_path: Path
) -> None:
    long_reason = "the change breaks this case\n" * 72  # about 2,000 characters on many lines
    assert len(long_reason) >= 2_000
    provider = FakeProvider(
        replies={f"reviewer:{a}": [_verdict("fail", long_reason)] for a in _REVIEWERS}
    )

    task, _ = await _run_task(
        db, tmp_path, provider,
        {
            "execution": {"parallel_reviewers": True},
            "agent_overrides": {"developer": {"max_retries": 1}},
        },
    )

    (failure,) = await _failure_payloads(db, task.id)
    reason = failure["reason"]
    assert "\n" not in reason and "  " not in reason
    assert len(reason) == 300 and reason.endswith("...")
    assert reason.startswith("rejected by TESTER (fail): the change breaks this case the change")
    assert "; rejected by SECURITY (fail): " in reason
    assert (task.result_summary or "").endswith(f"; last: {reason}")


async def test_a_red_gate_is_named_with_its_exit_code(db: Database, tmp_path: Path) -> None:
    task, ok = await _run_task(
        db, tmp_path, FakeProvider(),
        {
            "execution": {"baseline_test_command": "if [ -f FEATURE.txt ]; then exit 3; fi"},
            "agent_overrides": {"developer": {"max_retries": 1}},
        },
    )

    assert ok is False and task.state is TaskState.failed
    assert await _failure_payloads(db, task.id) == [
        {"n": 1, "stage": "gate", "exit_code": 3, "reason": "test gate not green (exit 3)"}
    ]
    assert task.result_summary == (
        "failed (max_retries) after 1 attempt(s); last: test gate not green (exit 3)"
    )


async def test_a_developer_error_is_the_reason_on_one_line(db: Database, tmp_path: Path) -> None:
    error = "exit 1:\n  Traceback (most recent call last):\n    RuntimeError: boom\n"
    provider = FakeProvider(replies={"developer": [AgentResult(ok=False, error=error)]})

    task, ok = await _run_task(
        db, tmp_path, provider, {"agent_overrides": {"developer": {"max_retries": 1}}}
    )

    assert ok is False and task.state is TaskState.failed
    why = "exit 1: Traceback (most recent call last): RuntimeError: boom"
    assert await _failure_payloads(db, task.id) == [
        {"n": 1, "stage": "developer", "error": error, "reason": why}
    ]
    assert task.result_summary == f"failed (max_retries) after 1 attempt(s); last: {why}"


_REJECTION = "not what I asked:\n use the helper"
_LONG_REJECTION = "use the helper, not a new module\n" * 40  # about 1,300 characters, many lines


@pytest.mark.parametrize(
    ("checkpoint", "stage"),
    [("developer", "developer"), ("gate", "gate"), ("reviewer", "review")],
)
@pytest.mark.parametrize("message", [_REJECTION, _LONG_REJECTION], ids=["short", "long"])
async def test_an_operator_rejection_at_a_checkpoint_is_why_the_attempt_failed(
    db: Database, tmp_path: Path, checkpoint: str, stage: str, message: str
) -> None:
    await _init_repo(tmp_path / "repo")
    project = await repo.create_project(
        db, name="t", path=str(tmp_path / "repo"), default_branch="main",
        config={
            "execution": {"target_branch": "main", "baseline_test_command": "true"},
            "agent_overrides": {"developer": {"max_retries": 1}},
        },
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    bus = EventBus(db)
    orchestrator = Orchestrator(db, bus, FakeProvider(), tmp_path / "home")
    orchestrator.set_hitl_checkpoint(task.id, checkpoint, True)
    with bus.subscribe(task_id=task.id, types=["hitl.waiting"]) as waiting:
        running = asyncio.create_task(orchestrator.process_task(claimed))
        hitl = await asyncio.wait_for(anext(waiting), timeout=30)
        assert hitl.payload["node_id"] == checkpoint
        assert orchestrator.resolve_node(task.id, checkpoint, status="failed", message=message)
        assert await asyncio.wait_for(running, timeout=30) is False

    (failure,) = await _failure_payloads(db, task.id)
    assert (set(failure), failure["n"], failure["stage"]) == ({"n", "stage", "reason"}, 1, stage)
    reason = failure["reason"]
    if message == _REJECTION:
        assert reason == "operator rejected: not what I asked: use the helper"
    else:
        assert reason.startswith("operator rejected: use the helper, not a new module use the")
        assert "\n" not in reason and len(reason) <= 300 and reason.endswith("...")
    final = await repo.get_task(db, task.id)
    assert final is not None and final.state == TaskState.failed
    assert final.result_summary == f"failed (max_retries) after 1 attempt(s); last: {reason}"
    if checkpoint == "gate":  # an operator's rejection, not a red test run
        gates = [c for c in await repo.list_task_checkpoints(db, task.id) if c.node_id == "gate"]
        assert [(c.status, c.artifact.get("cause")) for c in gates] == [
            (CheckpointStatus.failed, "operator")
        ]


# --- retry bounds -----------------------------------------------------------------------------
# The developer's max_retries bounds the attempts, execution.review_rounds_max the attempts that
# reach review, and each on_fail edge's max_iterations the retries taken through it: the tightest
# ends the task, and the summary names it with its value.


def _standard_workflow(**edge_iterations: int) -> dict[str, Any]:
    """The standard workflow with the on_fail edges named in *edge_iterations* (by id, with
    ``-`` as ``_``) set to that many iterations, and ``all`` setting every on_fail edge."""
    graph = workflow_templates()["standard"]
    for edge in graph.edges:
        if edge.condition != "on_fail":
            continue
        edge.max_iterations = edge_iterations.get(
            edge.id.replace("-", "_"), edge_iterations.get("all", edge.max_iterations)
        )
    return graph.model_dump(mode="json")


async def test_review_rounds_max_ends_the_task_before_another_review_round(
    db: Database, tmp_path: Path
) -> None:
    provider = FakeProvider(replies={"reviewer:security": [_verdict("fail", "still wrong")]})

    task, ok = await _run_task(
        db, tmp_path, provider,
        {
            "execution": {"review_rounds_max": 1},
            "agent_overrides": {"developer": {"max_retries": 5}},
        },
    )

    assert ok is False and task.state is TaskState.failed
    assert len(_prompts_of(provider, "developer")) == 1  # no attempt it could not review
    why = "rejected by SECURITY (fail): still wrong"
    assert task.result_summary == (
        "failed (review_rounds_max) after 1 attempt(s); "
        f"stopped by execution.review_rounds_max=1; last: {why}"
    )
    (failed,) = await _events(db, task.id, "task.failed")
    assert failed.payload == {"reason": "review_rounds_max", "attempts": 1}


async def test_an_on_fail_edge_is_taken_at_most_its_max_iterations(
    db: Database, tmp_path: Path
) -> None:
    # Security's edge allows one retry, the reviewer's the default three: security rejects
    # attempt 1 (its one retry), the reviewer attempt 2 (its own edge still allows it), and
    # security attempt 3, whose retry its edge no longer allows.
    provider = FakeProvider(
        replies={
            "reviewer:security": [
                _verdict("fail", "unsafe"), _PASS, _verdict("fail", "unsafe again")
            ],
            "reviewer:reviewer": [_verdict("fail", "unclear")],
        }
    )

    task, ok = await _run_task(
        db, tmp_path, provider,
        {
            "workflow": _standard_workflow(e_security_developer_fail=1),
            "agent_overrides": {"developer": {"max_retries": 5}},
        },
    )

    assert ok is False and task.state is TaskState.failed
    assert len(_prompts_of(provider, "developer")) == 3
    assert [p["reason"] for p in await _failure_payloads(db, task.id)] == [
        "rejected by SECURITY (fail): unsafe",
        "rejected by REVIEWER (fail): unclear",
        "rejected by SECURITY (fail): unsafe again",
    ]
    assert task.result_summary == (
        "failed (max_iterations) after 3 attempt(s); stopped by max_iterations=1 of on_fail edge "
        "e-security-developer-fail (security -> developer); "
        "last: rejected by SECURITY (fail): unsafe again"
    )


async def test_the_gate_on_fail_edge_bounds_the_retries_of_a_red_gate(
    db: Database, tmp_path: Path
) -> None:
    provider = FakeProvider()

    task, ok = await _run_task(
        db, tmp_path, provider,
        {
            "execution": {"baseline_test_command": "if [ -f FEATURE.txt ]; then exit 3; fi"},
            "workflow": _standard_workflow(e_gate_developer_fail=1),
            "agent_overrides": {"developer": {"max_retries": 5}},
        },
    )

    assert ok is False and task.state is TaskState.failed
    assert len(_prompts_of(provider, "developer")) == 2  # attempt 1, and its one retry
    assert task.result_summary == (
        "failed (max_iterations) after 2 attempt(s); stopped by max_iterations=1 of on_fail edge "
        "e-gate-developer-fail (gate -> developer); last: test gate not green (exit 3)"
    )


@pytest.mark.parametrize(
    "workflow",
    [None, _standard_workflow(all=6)],
    ids=["default-graph", "on-fail-edges-at-6"],
)
async def test_the_default_bounds_still_make_three_attempts(
    db: Database, tmp_path: Path, workflow: dict[str, Any] | None
) -> None:
    # max_retries 3, on_fail edges at 3 (or this project's 6) and review_rounds_max 20: the
    # developer's max_retries ends the task, as before the other bounds were enforced.
    provider = FakeProvider(replies={"reviewer:security": [_verdict("fail", "still wrong")]})

    task, ok = await _run_task(
        db, tmp_path, provider, {"workflow": workflow} if workflow is not None else None
    )

    assert ok is False and task.state is TaskState.failed
    assert len(_prompts_of(provider, "developer")) == 3
    assert task.result_summary == (
        "failed (max_retries) after 3 attempt(s); last: rejected by SECURITY (fail): still wrong"
    )
    (failed,) = await _events(db, task.id, "task.failed")
    assert failed.payload == {"reason": "max_retries", "attempts": 3}


async def test_a_resumed_task_counts_the_review_rounds_before_its_crash(
    db: Database, tmp_path: Path
) -> None:
    # The daemon stopped after attempt 2's developer finished; attempt 1 had been reviewed. On
    # resume, attempt 2 is the task's second review round, and with review_rounds_max 2 its last.
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    (repo_path / "FEATURE.txt").write_text("done\n", encoding="utf-8")
    await run_git(repo_path, "add", "FEATURE.txt")
    _, tree = await run_git(repo_path, "write-tree")  # attempt 2's work, kept as a tree object
    await run_git(repo_path, "reset", "-q")
    (repo_path / "FEATURE.txt").unlink()
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={
            "execution": {
                "target_branch": "main", "allow_no_test_command": True, "review_rounds_max": 2
            },
            "agent_overrides": {"developer": {"max_retries": 5}},
        },
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )
    await repo.save_task_checkpoint(
        db, task_id=task.id, node_id="developer", attempt=1, status=CheckpointStatus.completed
    )
    await repo.save_task_checkpoint(
        db, task_id=task.id, node_id="review", attempt=1, status=CheckpointStatus.failed
    )
    await repo.save_task_checkpoint(
        db, task_id=task.id, node_id="developer", attempt=2,
        status=CheckpointStatus.completed, git_tree_sha=tree.strip(),
    )
    provider = FakeProvider(replies={"reviewer:security": [_verdict("fail", "still wrong")]})
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None

    ok = await Orchestrator(db, EventBus(db), provider, tmp_path / "home").process_task(claimed)

    final = await repo.get_task(db, task.id)
    assert ok is False and final is not None and final.state is TaskState.failed
    assert _prompts_of(provider, "developer") == []  # attempt 2 resumed from its checkpoint
    assert final.result_summary == (
        "failed (review_rounds_max) after 2 attempt(s); stopped by execution.review_rounds_max=2; "
        "last: rejected by SECURITY (fail): still wrong"
    )
