"""Hidden acceptance tests for W18 items 3-5 and 7 (follow-ups from the W12/W13 reviews).

Written by the pilot from the spec alone, run against the task branch before merging; not part
of the repository's own suite.
- 3: the workflow evolver learns only from quality failures (not from a test command removed
  mid-task, nor from an infra gate failure);
- 4: a resumed Claude Code run whose running total restarted is charged in full, not 0;
- 5: an operator rejection at a HITL checkpoint is an ``attempt.failed`` with its reason;
- 7: only ``providers/`` sets ``resume_session_id``.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import stat
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fake_provider import FakeProvider

import conclave
from conclave.config import ArgMode
from conclave.db import Database, TaskState
from conclave.db import repositories as repo
from conclave.engine import Orchestrator, run_git
from conclave.events import EventBus
from conclave.providers import AgentResult, OnChunk, QuotaGate, ResolvedProfile
from conclave.providers.quota import dispatch_with_quota

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
RESET = int((T0 + timedelta(hours=1)).timestamp())


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "conclave.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


async def _project(db: Database, tmp_path: Path, test_command: str) -> str:
    rp = tmp_path / "repo"
    rp.mkdir()
    await run_git(rp, "init", "-b", "main")
    await run_git(rp, "config", "user.email", "t@example.com")
    await run_git(rp, "config", "user.name", "T")
    (rp / "README.md").write_text("# t\n")
    await run_git(rp, "add", "-A")
    await run_git(rp, "commit", "-m", "init")
    project = await repo.create_project(
        db, name="t", path=str(rp), default_branch="main",
        config={
            "execution": {"target_branch": "main", "baseline_test_command": test_command},
            "experimental": {"workflow_evolution": "suggest"},
        },
    )
    return project.id


async def _run(
    db: Database, pid: str, provider: FakeProvider, home: Path,
    orchestrator: Orchestrator | None = None,
) -> Any:
    task = await repo.create_task(
        db, project_id=pid, request="add a feature file", state=TaskState.approved
    )
    claimed = await repo.claim_next_approved(db, pid)
    assert claimed is not None
    orch = orchestrator or Orchestrator(db, EventBus(db), provider, home)
    await orch.process_task(claimed)
    final = await repo.get_task(db, task.id)
    assert final is not None
    return final


# --- 3: the evolver learns only from quality failures -----------------------------------------


class _RemovesTestCommand(FakeProvider):
    """The developer's dispatch also removes the project's test command (an operator edit)."""

    def __init__(self, db: Database, pid: str) -> None:
        super().__init__()
        self.db = db
        self.pid = pid

    async def run_agent(
        self, *, profile: ResolvedProfile, prompt: str, timeout_seconds: int,
        cwd: Path | None = None, on_chunk: OnChunk | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AgentResult:
        if self.role_of(prompt) == "developer":
            project = await repo.get_project(self.db, self.pid)
            assert project is not None
            config = dict(project.config or {})
            execution = dict(config.get("execution") or {})
            execution["baseline_test_command"] = ""
            config["execution"] = execution
            await repo.update_project_config(self.db, self.pid, config)
        return await super().run_agent(
            profile=profile, prompt=prompt, timeout_seconds=timeout_seconds, cwd=cwd,
            on_chunk=on_chunk, cancel_event=cancel_event,
        )


async def test_a_test_command_removed_mid_task_is_no_evolution_signal(
    db: Database, tmp_path: Path
) -> None:
    pid = await _project(db, tmp_path, "echo ok")
    final = await _run(db, pid, _RemovesTestCommand(db, pid), tmp_path / "home")
    assert final.state is TaskState.failed, final.result_summary  # W13 3b
    assert await repo.list_workflow_proposals(db, pid) == []


async def test_an_infra_gate_failure_is_no_grow_signal(db: Database, tmp_path: Path) -> None:
    # Exit 127 (command not found) is an infra outcome; green on the base branch.
    pid = await _project(db, tmp_path, "if [ -f FEATURE.txt ]; then exit 127; fi")
    final = await _run(db, pid, FakeProvider(), tmp_path / "home")
    assert final.state is TaskState.failed, final.result_summary
    proposals = await repo.list_workflow_proposals(db, pid)
    assert not [p for p in proposals if (p.reason or {}).get("action") == "grow"], proposals


async def test_a_real_red_gate_after_a_passing_review_still_proposes_growth(
    db: Database, tmp_path: Path
) -> None:
    pid = await _project(db, tmp_path, "test ! -f FEATURE.txt")
    final = await _run(db, pid, FakeProvider(), tmp_path / "home")
    assert final.state is TaskState.failed
    proposals = await repo.list_workflow_proposals(db, pid)
    assert [p for p in proposals if (p.reason or {}).get("action") == "grow"], proposals


# --- 4: a resumed run whose running total restarted -----------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.current = T0

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)
        await asyncio.sleep(0)


class _Recorder:
    def __init__(self) -> None:
        self.interrupted: list[float | None] = []

    async def waiting(self, wait: Any, interrupted: AgentResult | None) -> None:
        if interrupted is not None:
            self.interrupted.append(interrupted.cost_usd)

    async def resumed(self, wait: Any, *, waited_seconds: float, resumed_session: bool) -> None:
        return None


def _fake_claude(tmp_path: Path, run_costs: list[float], *, restore: bool) -> Path:
    """Refuses its first run, then completes. With *restore* False, a ``--resume`` run starts
    the session's running total again from 0 (the CLI did not save it), as after a stopped run."""
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
        "    st = {'runs': 0, 'totals': {}}\n"
        "resume = args[args.index('--resume') + 1] if '--resume' in args else None\n"
        "n = st['runs']\n"
        "sid = resume or ('s-%d' % n)\n"
        f"base = st['totals'].get(sid, 0.0) if {restore!r} else 0.0\n"
        f"total = round(base + {run_costs!r}[n], 4)\n"
        "st['runs'] = n + 1\n"
        "st['totals'][sid] = total\n"
        "json.dump(st, open(state_path, 'w'))\n"
        "out = lambda e: print(json.dumps({**e, 'session_id': sid}), flush=True)\n"
        "out({'type': 'system', 'subtype': 'init', 'model': 'claude-test'})\n"
        "if n == 0:\n"
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


async def _dispatch(tmp_path: Path, run_costs: list[float], *, restore: bool) -> tuple[
    AgentResult, _Recorder
]:
    from conclave.providers.claude_cli import ClaudeCliProvider

    clock = _Clock()
    recorder = _Recorder()
    profile = ResolvedProfile(
        name="claude-code", arg_mode=ArgMode.flag,
        cli=str(_fake_claude(tmp_path, run_costs, restore=restore)),
        model="claude-code/claude-test",
    )
    result = await dispatch_with_quota(
        ClaudeCliProvider(), QuotaGate(clock=clock.now, sleep=clock.sleep),
        profile=profile, prompt="do the task", timeout_seconds=30, retry_minutes=15,
        cwd=tmp_path, listener=recorder,
    )
    return result, recorder


@pytest.mark.skipif(os.name == "nt", reason="POSIX fake executables")
async def test_a_resumed_run_whose_total_restarted_is_charged_in_full(tmp_path: Path) -> None:
    result, recorder = await _dispatch(tmp_path, [4.52, 2.00], restore=False)
    assert result.ok
    assert recorder.interrupted == [pytest.approx(4.52)]
    assert result.cost_usd == pytest.approx(2.00)


@pytest.mark.skipif(os.name == "nt", reason="POSIX fake executables")
async def test_a_restored_running_total_is_still_charged_its_share(tmp_path: Path) -> None:
    result, recorder = await _dispatch(tmp_path, [4.52, 3.97], restore=True)
    assert result.ok
    assert recorder.interrupted == [pytest.approx(4.52)]
    assert result.cost_usd == pytest.approx(3.97)


# --- 5: an operator rejection is an attempt.failed with its reason ----------------------------


async def test_an_operator_rejection_at_the_developer_checkpoint_says_why(
    db: Database, tmp_path: Path
) -> None:
    pid = await _project(db, tmp_path, "echo ok")
    orch = Orchestrator(db, EventBus(db), FakeProvider(), tmp_path / "home")
    task = await repo.create_task(
        db, project_id=pid, request="add a feature file", state=TaskState.approved
    )
    orch.set_hitl_checkpoint(task.id, "developer", True)
    claimed = await repo.claim_next_approved(db, pid)
    assert claimed is not None
    run = asyncio.create_task(orch.process_task(claimed))
    message = "no, put it behind a helper function"
    rejections = 0
    try:
        for _ in range(600):  # up to 60 s
            if run.done():
                break
            waiting = orch.get_hitl_state(task.id)["waiting"]
            if "developer" in waiting:
                assert orch.resolve_node(task.id, "developer", status="failed", message=message)
                rejections += 1
            await asyncio.sleep(0.1)
        await asyncio.wait_for(run, 30)
    finally:
        if not run.done():
            run.cancel()
    assert rejections >= 1
    final = await repo.get_task(db, task.id)
    assert final is not None and final.state is TaskState.failed, final
    events = await repo.list_events(db, task_id=task.id)
    failed = [e.payload for e in events if e.type == "attempt.failed"]
    assert len(failed) == rejections, failed
    for payload in failed:
        assert payload["stage"] == "developer"
        assert payload["reason"].startswith("operator rejected: ")
        assert message in payload["reason"] and len(payload["reason"]) <= 300
    assert (final.result_summary or "").endswith(f"; last: operator rejected: {message}")


# --- 7: only providers/ sets resume_session_id ----------------------------------------------


def test_only_providers_set_resume_session_id() -> None:
    src = Path(conclave.__file__).resolve().parent
    offenders: list[str] = []
    for path in src.rglob("*.py"):
        if "providers" in path.relative_to(src).parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "resume_session_id":
                offenders.append(f"{path.relative_to(src)}:{node.value.lineno}")
            if isinstance(node, ast.Attribute) and node.attr == "resume_session_id" and (
                isinstance(node.ctx, ast.Store)
            ):
                offenders.append(f"{path.relative_to(src)}:{node.lineno}")
    assert offenders == [], offenders


def test_the_dispatch_docstring_states_the_resume_contract() -> None:
    doc = dispatch_with_quota.__doc__ or ""
    assert "resume_session_id" in doc, json.dumps(doc[:400])
