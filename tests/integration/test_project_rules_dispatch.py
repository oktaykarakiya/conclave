"""Every dispatch of a task and of a planning turn gets each project rule file exactly once.

The repository has an AGENTS.md, and a CLAUDE.md that imports docs/style.md; the workspace-level
AGENTS.md sits beside it. An isolated Claude Code dispatch loads no rule file of its own, so its
prompt carries all four; an opencode dispatch loads the worktree's AGENTS.md itself (and never
CLAUDE.md, which AGENTS.md hides from it), so its prompt carries the workspace file only.
Deterministic: a throwaway repository and the FakeProvider, no LLM.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fake_provider import FakeProvider

from conclave.config import create_project_with_config
from conclave.db import Database, Project, TaskState
from conclave.db import repositories as repo
from conclave.engine import Orchestrator, run_git
from conclave.engine import orchestrator as orchestrator_module
from conclave.engine.project_rules import ProjectRules, read_project_rules
from conclave.events import EventBus
from conclave.planning.session import PlanningOrchestrator
from conclave.providers import Engine, resolve_engine

# One marker per rule file, free of the phrases FakeProvider routes on.
_WORKSPACE = "rulemark-workspace-0b7"
_AGENTS = "rulemark-agents-0b7"
_CLAUDE = "rulemark-claude-0b7"
_IMPORTED = "rulemark-imported-0b7"
_MARKERS = (_WORKSPACE, _AGENTS, _CLAUDE, _IMPORTED)


@pytest.fixture(autouse=True)
def _no_rule_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    # This suite also runs inside isolated workers, whose environment carries the first of these.
    for var in (
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS",
        "OPENCODE_DISABLE_CLAUDE_CODE",
        "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT",
        "OPENCODE_DISABLE_PROJECT_CONFIG",
    ):
        monkeypatch.delenv(var, raising=False)


def _write_rules(tmp_path: Path) -> Path:
    """The repository tmp_path/repo with its rule files, beside the workspace-level AGENTS.md."""
    (tmp_path / "AGENTS.md").write_text(f"{_WORKSPACE}\n", encoding="utf-8")
    repo_path = tmp_path / "repo"
    (repo_path / "docs").mkdir(parents=True)
    (repo_path / "AGENTS.md").write_text(f"{_AGENTS}\n", encoding="utf-8")
    (repo_path / "CLAUDE.md").write_text(f"{_CLAUDE}\nStyle: @docs/style.md\n", encoding="utf-8")
    (repo_path / "docs" / "style.md").write_text(f"{_IMPORTED}\n", encoding="utf-8")
    return repo_path


async def _project(db: Database, repo_path: Path, agent: str, model: str) -> Project:
    return await create_project_with_config(
        db,
        name="p",
        path=str(repo_path),
        default_branch="main",
        patch={
            "execution": {"baseline_test_command": "echo ok"},
            "agent_overrides": {agent: {"model": model}},
        },
    )


def _engine(model: str | None) -> Engine:
    # FakeProvider has no default engine: an un-prefixed model runs on opencode.
    return resolve_engine(model, Engine.opencode)[0]


def _assert_each_rule_file_once(provider: FakeProvider) -> None:
    assert provider.prompts
    for prompt, profile in zip(provider.prompts, provider.profiles, strict=True):
        counts = [prompt.count(marker) for marker in _MARKERS]
        if _engine(profile.model) == Engine.claude:
            assert counts == [1, 1, 1, 1], FakeProvider.role_of(prompt)
        else:
            assert counts == [1, 0, 0, 0], FakeProvider.role_of(prompt)


async def test_task_dispatches_get_each_rule_file_once_for_their_engine(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_path = _write_rules(tmp_path)
    await run_git(repo_path, "init", "-b", "main")
    await run_git(repo_path, "config", "user.email", "test@example.com")
    await run_git(repo_path, "config", "user.name", "Test")
    await run_git(repo_path, "add", "-A")
    await run_git(repo_path, "commit", "-m", "initial commit")
    reads: list[Path] = []

    def counting_read(root: Path, repo_path: Path | None = None) -> ProjectRules:
        reads.append(root)
        return read_project_rules(root, repo_path=repo_path)

    monkeypatch.setattr(orchestrator_module, "read_project_rules", counting_read)
    project = await _project(db, repo_path, "developer", "claude-code/sonnet")
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )
    provider = FakeProvider()
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None

    assert await orchestrator.process_task(claimed) is True

    done = await repo.get_task(db, task.id)
    assert done is not None and done.state is TaskState.done
    roles = [FakeProvider.role_of(prompt) for prompt in provider.prompts]
    engines = [_engine(profile.model) for profile in provider.profiles]
    # The developer ran on Claude Code; the planner and every reviewer on opencode.
    assert [r for r, e in zip(roles, engines, strict=True) if e == Engine.claude] == ["developer"]
    assert "planner" in roles and sum(r.startswith("reviewer:") for r in roles) >= 3
    _assert_each_rule_file_once(provider)
    assert len(reads) == 1  # the files were read once for the whole task


async def test_planning_turns_get_each_rule_file_once_for_their_engine(
    db: Database, tmp_path: Path
) -> None:
    repo_path = _write_rules(tmp_path)
    project = await _project(db, repo_path, "planner", "claude-code/opus")
    session = await repo.create_planning_session(
        db, project_id=project.id, title="T", prompt="Plan X"
    )
    provider = FakeProvider()
    orchestrator = PlanningOrchestrator(db, EventBus(db), provider)

    planned, _ = await orchestrator._agent_turn(
        session.id, "planner", "Planning Facilitator Agent", "Plan X"
    )
    reviewed, _ = await orchestrator._agent_turn(
        session.id, "reviewer", "Senior Reviewer Agent", "Review the plan"
    )

    assert planned is not None and "task_changes" in planned
    assert reviewed is not None and "pass" in reviewed
    assert [_engine(profile.model) for profile in provider.profiles] == [
        Engine.claude,
        Engine.opencode,
    ]
    _assert_each_rule_file_once(provider)
