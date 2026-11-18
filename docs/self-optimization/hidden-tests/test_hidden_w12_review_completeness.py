"""Hidden acceptance tests for W12 (mandatory reviewers must answer; planner failures visible).

Written by the pilot from the spec alone, run against the task branch before merging; not part
of the repository's own suite. Needs `tests/integration` on PYTHONPATH (FakeProvider).
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from fake_provider import FakeProvider

import conclave
from conclave.db import Database, TaskState
from conclave.db import repositories as repo
from conclave.engine import Orchestrator, run_git
from conclave.events import EventBus
from conclave.providers import AgentResult, OnChunk, ResolvedProfile

_PASS = '```json\n{"verdict": "pass", "reason": "looks correct", "evidence": []}\n```'
_NO_VERDICT = "I read the diff carefully and it looks fine to me overall."


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


def _is_review(prompt: str) -> bool:
    return "Review the changes made for this task" in prompt


class ScriptedProvider(FakeProvider):
    """FakeProvider whose replies for chosen roles follow a script, one entry per dispatch.

    An entry is "ok" (defer to FakeProvider), "noverdict" (non-empty text without a verdict
    block) or "crash" (ok=False, an error, no text). When a role's script runs out, the last
    entry repeats.
    """

    def __init__(self, scripts: dict[str, list[str]], **kw: Any) -> None:
        super().__init__(**kw)
        self.scripts = scripts
        self.calls: dict[str, list[str]] = {}

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
        self.calls.setdefault(role, []).append(prompt)
        script = self.scripts.get(role)
        if script:
            n = len(self.calls[role]) - 1
            step = script[min(n, len(script) - 1)]
            if step == "noverdict":
                self.prompts.append(prompt)
                return AgentResult(ok=True, text=_NO_VERDICT, model_reported="fake", cost_usd=0.0)
            if step == "crash":
                self.prompts.append(prompt)
                return AgentResult(ok=False, text="", error="CLI crashed (exit 1)")
        return await super().run_agent(
            profile=profile, prompt=prompt, timeout_seconds=timeout_seconds, cwd=cwd,
            on_chunk=on_chunk, cancel_event=cancel_event,
        )


async def _project(db: Database, tmp_path: Path, extra: dict[str, Any] | None = None) -> str:
    rp = tmp_path / "repo"
    rp.mkdir()
    await run_git(rp, "init", "-b", "main")
    await run_git(rp, "config", "user.email", "t@example.com")
    await run_git(rp, "config", "user.name", "T")
    (rp / "README.md").write_text("# t\n")
    await run_git(rp, "add", "-A")
    await run_git(rp, "commit", "-m", "init")
    config: dict[str, Any] = {
        "execution": {"target_branch": "main", "baseline_test_command": "echo ok"}
    }
    for k, v in (extra or {}).items():
        config.setdefault(k, {}).update(v)
    p = await repo.create_project(db, name="t", path=str(rp), default_branch="main", config=config)
    return p.id


async def _run(db: Database, tmp_path: Path, provider: FakeProvider,
               extra: dict[str, Any] | None = None) -> tuple[Any, list[Any]]:
    pid = await _project(db, tmp_path, extra)
    task = await repo.create_task(
        db, project_id=pid, request="add a feature file", state=TaskState.approved
    )
    orch = Orchestrator(db, EventBus(db), provider, tmp_path / "home")
    claimed = await repo.claim_next_approved(db, pid)
    assert claimed is not None
    await orch.process_task(claimed)
    final = await repo.get_task(db, task.id)
    events = await repo.list_events(db, task_id=task.id)
    return final, events


def _reviews(provider: ScriptedProvider, role: str) -> list[str]:
    return [p for p in provider.calls.get(role, []) if _is_review(p)]


async def test_mandatory_reviewer_without_verdict_once_is_rerun_with_reminder(
    db: Database, tmp_path: Path
) -> None:
    provider = ScriptedProvider({"security": ["noverdict", "ok"]})
    final, _ = await _run(db, tmp_path, provider)
    assert final.state is TaskState.done, final.result_summary
    sec = _reviews(provider, "security")
    assert len(sec) == 2
    assert "previous reply had no verdict block" not in sec[0].lower()
    assert "previous reply had no verdict block" in sec[1].lower()
    verdicts = [v for v in await repo.list_verdicts(db, final.id) if v.agent == "security"]
    assert verdicts and verdicts[-1].verdict == "pass"


@pytest.mark.parametrize("failure", ["noverdict", "crash"])
async def test_mandatory_reviewer_never_answering_blocks_the_task(
    db: Database, tmp_path: Path, failure: str
) -> None:
    provider = ScriptedProvider({"security": [failure]})
    final, events = await _run(db, tmp_path, provider)
    assert final.state is not TaskState.done, "a task without a security verdict went green"
    types = [e.type for e in events]
    assert "task.committed" not in types and "task.merged" not in types
    # first round: the reviewer itself plus 2 re-runs
    first_round = _reviews(provider, "security")
    assert len(first_round) >= 3
    # The reviewer is named where the pilot and the developer see the failure: a warning event,
    # and the retry feedback the next developer attempt receives.
    warnings = " ".join(str(e.payload) for e in events if e.type == "grounding.warning")
    assert "security" in warnings and "mandatory" in warnings
    retries = provider.calls.get("developer", [])[1:]
    assert retries and all("REVIEW INCONCLUSIVE" in p and "SECURITY" in p for p in retries)


async def test_optional_reviewer_without_verdict_warns_but_passes(
    db: Database, tmp_path: Path
) -> None:
    provider = ScriptedProvider({"architect": ["noverdict"]})
    final, events = await _run(db, tmp_path, provider)
    assert len(_reviews(provider, "architect")) >= 1, "architect was not triggered; test invalid"
    assert final.state is TaskState.done, final.result_summary
    # a warning: an event naming the architect that is not one of the pre-existing kinds
    pre_existing = {"agent.dispatched", "agent.result", "agent.output_chunk", "node.started",
                    "node.completed", "interaction.logged", "usage.recorded", "agent.verdict",
                    "pipeline.derived", "reviewers.selected"}
    warn = [e for e in events if "architect" in str(e.payload) and e.type not in pre_existing]
    assert warn, "no warning event names the optional reviewer"


async def test_planner_retried_then_used(db: Database, tmp_path: Path) -> None:
    provider = ScriptedProvider({"planner": ["crash", "crash", "ok"]})
    final, events = await _run(
        db, tmp_path, provider, {"agent_overrides": {"planner": {"max_retries": 3}}}
    )
    assert len(provider.calls["planner"]) == 3
    assert any(e.type == "plan.artifact" for e in events)
    assert final.state is TaskState.done


async def test_planner_always_failing_is_visible(db: Database, tmp_path: Path) -> None:
    provider = ScriptedProvider({"planner": ["crash"]})
    final, events = await _run(
        db, tmp_path, provider, {"agent_overrides": {"planner": {"max_retries": 2}}}
    )
    assert len(provider.calls["planner"]) == 2
    assert not any(e.type == "plan.artifact" for e in events)
    assert any(e.type == "log" and (e.payload or {}).get("stage") == "planner_failed"
               for e in events)
    assert "planned: no" in (final.result_summary or "")
    assert final.state is TaskState.done  # the planner node is optional; the work still lands


def test_gitattributes_marks_spa_bundle_generated(tmp_path: Path) -> None:
    # The tree under test may be a plain snapshot, not a checkout: check its .gitattributes in a
    # scratch repository.
    root = Path(conclave.__file__).resolve().parents[2]
    assert (root / ".gitattributes").is_file()
    scratch = tmp_path / "attrs"
    scratch.mkdir()
    subprocess.run(["git", "init", "-q", str(scratch)], check=True)
    (scratch / ".gitattributes").write_bytes((root / ".gitattributes").read_bytes())
    out = subprocess.run(
        ["git", "-C", str(scratch), "check-attr", "diff", "linguist-generated", "--",
         "src/conclave/web/static/assets/index.js"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "diff: unset" in out, out
    assert "linguist-generated: true" in out, out
