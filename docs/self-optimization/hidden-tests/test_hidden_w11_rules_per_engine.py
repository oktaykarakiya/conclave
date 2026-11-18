"""Hidden acceptance tests for W11 (project rules sent once per engine).

Written by the pilot from the spec alone, run against the task branch before merging; not part
of the repository's own suite. Needs `tests/integration` on PYTHONPATH (FakeProvider).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from fake_provider import FakeProvider

from conclave.config import ArgMode
from conclave.db import Database, TaskState
from conclave.db import repositories as repo
from conclave.engine import Orchestrator, run_git
from conclave.events import EventBus
from conclave.providers.profiles import ResolvedProfile, build_invocation

WS = "RULE-WORKSPACE-000"
AG = "RULE-AGENTS-123"
CL = "RULE-CLAUDE-456"
IM = "RULE-IMPORT-789"


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "conclave.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()


def test_isolated_claude_disables_native_rule_files() -> None:
    iso = build_invocation(ResolvedProfile(name="p", arg_mode=ArgMode.flag, isolated=True))
    assert iso.env.get("CLAUDE_CODE_DISABLE_CLAUDE_MDS") == "1"
    plain = build_invocation(ResolvedProfile(name="p", arg_mode=ArgMode.flag, isolated=False))
    assert "CLAUDE_CODE_DISABLE_CLAUDE_MDS" not in plain.env
    over = build_invocation(ResolvedProfile(
        name="p", arg_mode=ArgMode.flag, isolated=True,
        extra_env={"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "0"},
    ))
    assert over.env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "0"


def _role(prompt: str) -> str:
    for line in prompt.splitlines()[:4]:
        if line.startswith("You are the ") and " agent of " in line:
            return line.removeprefix("You are the ").split(" agent of ")[0]
    return "?"


async def test_mixed_engine_task_gets_each_rule_once(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CONCLAVE_ENGINE", raising=False)
    monkeypatch.delenv("OPENCODE_DISABLE_CLAUDE_CODE_PROMPT", raising=False)
    ws = tmp_path / "ws"
    rp = ws / "repo"
    rp.mkdir(parents=True)
    (ws / "AGENTS.md").write_text(f"# Workspace\n{WS}\n")
    await run_git(rp, "init", "-b", "main")
    await run_git(rp, "config", "user.email", "t@example.com")
    await run_git(rp, "config", "user.name", "T")
    (rp / "README.md").write_text("# t\n")
    (rp / "AGENTS.md").write_text(f"# Agents\n{AG}\n")
    (rp / "CLAUDE.md").write_text(
        f"# Claude\n{CL}\nSee @docs/extra.md for more.\n"
        "Inline `@docs/never.md` and an escape @../../outside.md stay literal.\n"
    )
    (rp / "docs").mkdir()
    (rp / "docs" / "extra.md").write_text(f"# Extra\n{IM}\n")
    (rp / "docs" / "never.md").write_text("RULE-NEVER-IMPORTED\n")
    await run_git(rp, "add", "-A")
    await run_git(rp, "commit", "-m", "init")

    project = await repo.create_project(
        db, name="t", path=str(rp), default_branch="main",
        config={
            "execution": {"target_branch": "main", "baseline_test_command": "echo ok"},
            "agent_defaults": {"model": "deepseek/deepseek-flash"},
            "agent_overrides": {"developer": {"model": "claude-code/claude-test"}},
        },
    )
    task = await repo.create_task(db, project_id=project.id, request="add a feature file",
                                  state=TaskState.approved)
    provider = FakeProvider()
    orch = Orchestrator(db, EventBus(db), provider, tmp_path / "home")
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    await orch.process_task(claimed)
    final = await repo.get_task(db, task.id)
    assert final is not None and final.state is TaskState.done, final

    by_role: dict[str, list[str]] = {}
    for prompt, profile in zip(provider.prompts, provider.profiles, strict=True):
        by_role.setdefault(_role(prompt), []).append(prompt)
        if _role(prompt) == "developer":
            assert (profile.model or "").startswith("claude"), profile.model

    dev = by_role["developer"][0]
    # Claude Code, isolated: Conclave is the only source of every rule file.
    assert dev.count(WS) == 1
    assert dev.count(AG) == 1
    assert dev.count(CL) == 1
    assert dev.count(IM) == 1  # @docs/extra.md resolved once
    assert "RULE-NEVER-IMPORTED" not in dev  # inline code is not an import
    assert "@../../outside.md" in dev  # an escape stays literal

    reviewers = [p for r in ("tester", "security", "reviewer") for p in by_role.get(r, [])]
    assert reviewers, by_role.keys()
    for prompt in reviewers:
        # opencode loads the worktree AGENTS.md natively: Conclave must not send it again,
        # and CLAUDE.md is not opencode's selected file when AGENTS.md exists.
        assert prompt.count(WS) == 1
        assert AG not in prompt
        assert CL not in prompt
