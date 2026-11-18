"""Hidden acceptance tests for W13 item 8 (an attempt's and a task's failure say why).

Written by the pilot from the spec alone, run against the task branch before merging; not part
of the repository's own suite. Measured on W12's branch: a task whose mandatory security
reviewer never answered ended "failed (max_retries) after 3 attempt(s)", and each
``attempt.failed`` event was only ``{"n": k, "stage": "review"}``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fake_provider import FakeProvider

from conclave.db import Database, TaskState
from conclave.db import repositories as repo
from conclave.engine import Orchestrator, run_git
from conclave.events import EventBus
from conclave.providers import AgentResult, OnChunk, ResolvedProfile

_NO_VERDICT = "I read the diff carefully and it looks fine to me overall."
_LONG = "the change drops input validation on the merge endpoint " * 20  # ~1,140 chars


def _fail(reason: str) -> str:
    return (
        '```json\n{"verdict": "fail", "reason": "' + reason + '", "evidence": []}\n```'
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
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "conclave.engine.orchestrator._REVIEWER_RETRY_BACKOFF_S", 0.0, raising=False
    )


def _role(prompt: str) -> str:
    for line in prompt.splitlines()[:4]:
        if line.startswith("You are the ") and " agent of " in line:
            return line.removeprefix("You are the ").split(" agent of ")[0]
    return "?"


class Scripted(FakeProvider):
    """Chosen reviewers always answer *text* (``None``: crash); everyone else as FakeProvider."""

    def __init__(self, replies: dict[str, str | None]) -> None:
        super().__init__()
        self.replies = replies

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
        role = _role(prompt)
        if role in self.replies and "Review the changes made for this task" in prompt:
            self.prompts.append(prompt)
            text = self.replies[role]
            if text is None:
                return AgentResult(ok=False, text="", error="CLI crashed (exit 1)")
            return AgentResult(ok=True, text=text, model_reported="fake", cost_usd=0.0)
        return await super().run_agent(
            profile=profile, prompt=prompt, timeout_seconds=timeout_seconds, cwd=cwd,
            on_chunk=on_chunk, cancel_event=cancel_event,
        )


async def _run(
    db: Database, tmp_path: Path, provider: FakeProvider, test_command: str = "echo ok"
) -> tuple[Any, list[dict[str, Any]]]:
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
        config={"execution": {"target_branch": "main", "baseline_test_command": test_command}},
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    await Orchestrator(db, EventBus(db), provider, tmp_path / "home").process_task(claimed)
    final = await repo.get_task(db, task.id)
    assert final is not None and final.state is TaskState.failed, final
    events = await repo.list_events(db, task_id=task.id)
    failed = [e.payload for e in events if e.type == "attempt.failed"]
    assert failed, "no attempt.failed events"
    return final, failed


async def test_a_mandatory_reviewer_without_a_verdict_is_named(
    db: Database, tmp_path: Path
) -> None:
    final, failed = await _run(db, tmp_path, Scripted({"security": _NO_VERDICT}))
    for payload in failed:
        assert payload["stage"] == "review" and payload["n"] >= 1  # existing keys kept
        assert "SECURITY" in payload["reason"].upper()
        assert "\n" not in payload["reason"] and len(payload["reason"]) <= 300
    assert "max_retries" in final.result_summary
    assert "last:" in final.result_summary and "SECURITY" in final.result_summary.upper()


async def test_a_crashed_mandatory_reviewer_is_named(db: Database, tmp_path: Path) -> None:
    final, failed = await _run(db, tmp_path, Scripted({"security": None}))
    assert all("SECURITY" in p["reason"].upper() for p in failed)
    assert "SECURITY" in final.result_summary.upper()


async def test_a_fail_verdict_names_the_reviewer_and_quotes_its_reason(
    db: Database, tmp_path: Path
) -> None:
    final, failed = await _run(
        db, tmp_path, Scripted({"reviewer": _fail("missing null check in parse_args")})
    )
    for payload in failed:
        assert "REVIEWER" in payload["reason"].upper()
        assert "missing null check" in payload["reason"]
    assert "missing null check" in final.result_summary


async def test_a_long_reviewer_reason_is_bounded_to_one_line(
    db: Database, tmp_path: Path
) -> None:
    _, failed = await _run(db, tmp_path, Scripted({"reviewer": _fail(_LONG.strip())}))
    for payload in failed:
        assert len(payload["reason"]) <= 300 and "\n" not in payload["reason"]
        assert "REVIEWER" in payload["reason"].upper()


async def test_a_red_gate_states_the_exit_code(db: Database, tmp_path: Path) -> None:
    # Green on the base branch (no FEATURE.txt yet), red once the developer has written it.
    final, failed = await _run(
        db, tmp_path, FakeProvider(), test_command="test ! -f FEATURE.txt"
    )
    gate = [p for p in failed if p["stage"] == "gate"]
    assert gate, failed
    for payload in gate:
        assert "exit_code" in payload  # existing key kept
        assert "exit 1" in payload["reason"] and len(payload["reason"]) <= 300
    assert "last:" in final.result_summary and "exit 1" in final.result_summary
