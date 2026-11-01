"""Unit tests for Necessity & YAGNI enforcement, mid-task config hot-plug,
Windows/POSIX .venv guidance, and run_shell auto-PATH injection."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

from conclave.agents.personas import DEFAULT_PERSONAS
from conclave.config import ConclaveConfig, load_project_config, resolve_agent
from conclave.db import Database, TaskState
from conclave.db import repositories as repo
from conclave.engine.gitio import run_git, run_shell
from conclave.engine.orchestrator import (
    Orchestrator,
    _build_developer_prompt,
    _build_venv_guidance,
)
from conclave.engine.runner import AgentRunner
from conclave.events import EventBus
from conclave.providers import AgentResult, OnChunk, ResolvedProfile

# ---------------------------------------------------------------------------
# 1. Mandatory Necessity & YAGNI Verification
# ---------------------------------------------------------------------------


def test_seed_personas_include_necessity_and_yagni_verification() -> None:
    _, planner_md = DEFAULT_PERSONAS["planner"]
    _, reviewer_md = DEFAULT_PERSONAS["reviewer"]

    for persona_md in (planner_md, reviewer_md):
        assert "Necessity & YAGNI Verification" in persona_md
        assert "strictly necessary" in persona_md
        assert "redundant or speculative" in persona_md


def test_build_developer_prompt_includes_necessity_and_yagni_check() -> None:
    prompt = _build_developer_prompt(
        "Fix the off-by-one bug in parser",
        plan_preamble="\nPLAN",
        baseline_preamble="\nBASELINE",
        test_command="pytest -q",
        check_commands=["ruff check src tests"],
    )
    assert "NECESSITY & YAGNI CHECK" in prompt
    assert "strictly necessary" in prompt
    assert "before adding files or abstractions" in prompt
    assert "pytest -q" in prompt
    assert "ruff check src tests" in prompt


async def test_maybe_plan_prompt_includes_necessity_and_yagni_check(
    db: Database, tmp_path: Path
) -> None:
    bus = EventBus(db)
    captured_prompts: list[str] = []

    class _CapturingProvider:
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
            captured_prompts.append(prompt)
            return AgentResult(
                ok=True,
                text='```json\n{"approach": "minimal fix", "files_to_touch": ["a.py"]}\n```',
                model_reported="fake",
                cost_usd=0.0,
            )

    provider = _CapturingProvider()
    project = await repo.create_project(
        db, name="proj", path=str(tmp_path), default_branch="main"
    )
    config = ConclaveConfig()
    config.experimental.planner_enabled = True
    runner = AgentRunner(db, bus, provider, project.id, config)
    orch = Orchestrator(db, bus, provider, tmp_path / "home")
    task = await repo.create_task(
        db,
        project_id=project.id,
        request="Implement feature X with minimal footprint",
        use_planner=True,
        state=TaskState.in_progress,
    )

    plan_preamble = await orch._maybe_plan(
        runner,
        task,
        tmp_path,
        knowledge="",
        rules="",
        baseline=None,
        config=config,
    )
    assert "PLAN" in plan_preamble
    assert len(captured_prompts) == 1
    assert "NECESSITY & YAGNI CHECK" in captured_prompts[0]
    assert "strictly necessary" in captured_prompts[0]


# ---------------------------------------------------------------------------
# 2. Mid-Task Flow Hot-Plug (_refresh_task_config)
# ---------------------------------------------------------------------------


async def test_refresh_task_config_updates_runner_and_reloads_project_config(
    db: Database, tmp_path: Path
) -> None:
    bus = EventBus(db)

    class _NoopProvider:
        async def run_agent(self, **kwargs: Any) -> AgentResult:
            return AgentResult(ok=True, text="", model_reported="fake")

    provider = _NoopProvider()
    project = await repo.create_project(
        db,
        name="hotplug-proj",
        path=str(tmp_path),
        default_branch="main",
        config={"execution": {"baseline_test_command": "pytest -k old", "auto_merge": True}},
    )
    initial_cfg = load_project_config(project.config)
    runner = AgentRunner(db, bus, provider, project.id, initial_cfg)
    orch = Orchestrator(db, bus, provider, tmp_path / "home")

    assert runner._config.execution.baseline_test_command == "pytest -k old"
    assert runner._config.execution.auto_merge is True

    # Mutate project.config in SQLite while task is "in-flight"
    await repo.update_project_config(
        db,
        project.id,
        {
            "execution": {
                "baseline_test_command": "pytest -k new_hotplugged",
                "auto_merge": False,
                "require_full_green": False,
            },
            "agent_overrides": {"developer": {"max_retries": 5, "model": "deepseek/deepseek-r1"}},
        },
    )

    refreshed = await orch._refresh_task_config(project.id, runner, initial_cfg)

    assert refreshed is runner._config
    assert refreshed.execution.baseline_test_command == "pytest -k new_hotplugged"
    assert refreshed.execution.auto_merge is False
    assert refreshed.execution.require_full_green is False
    assert resolve_agent(refreshed, "developer").max_retries == 5
    assert resolve_agent(refreshed, "developer").model == "deepseek/deepseek-r1"

    # Missing project falls back gracefully to current_config
    fallback = await orch._refresh_task_config("nonexistent-id", runner, refreshed)
    assert fallback is refreshed


async def test_process_task_hotplugs_config_mid_task(
    db: Database, tmp_path: Path
) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    await run_git(repo_dir, "init", "-b", "main")
    (repo_dir / "README.md").write_text("hello\n", encoding="utf-8")
    await run_git(repo_dir, "add", "README.md")
    await run_git(repo_dir, "commit", "-m", "initial")

    project = await repo.create_project(
        db,
        name="live-hotplug",
        path=str(repo_dir),
        default_branch="main",
        config={
            "execution": {
                "baseline_test_command": "",
                "require_full_green": True,
                "auto_merge": True,
            },
            "experimental": {"planner_enabled": False},
            "agent_overrides": {"developer": {"max_retries": 1}},
        },
    )
    bus = EventBus(db)

    class _HotplugProvider:
        def __init__(self) -> None:
            self.dev_calls = 0

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
            if "Review the changes made for this task" in prompt:
                return AgentResult(
                    ok=True,
                    text='```json\n{"verdict": "pass", "reason": "ok", "evidence": []}\n```',
                    model_reported="fake",
                )
            self.dev_calls += 1
            if cwd is not None:
                (Path(cwd) / "HOTPLUG.txt").write_text("updated\n", encoding="utf-8")
            # Mid-task: while developer is running, mutate project.config to disable auto_merge
            await repo.update_project_config(
                db,
                project.id,
                {
                    "execution": {
                        "baseline_test_command": "",
                        "require_full_green": False,
                        "auto_merge": False,
                    },
                    "experimental": {"planner_enabled": False},
                    "agent_overrides": {"developer": {"max_retries": 2}},
                },
            )
            return AgentResult(ok=True, text="done", model_reported="fake")

    provider = _HotplugProvider()
    orch = Orchestrator(db, bus, provider, tmp_path / "home")
    task = await repo.create_task(
        db,
        project_id=project.id,
        request="Create HOTPLUG.txt",
        state=TaskState.in_progress,
    )

    ok = await orch.process_task(task)
    assert ok is True
    updated_task = await repo.get_task(db, task.id)
    assert updated_task is not None
    assert updated_task.state == TaskState.done
    # Because auto_merge was hot-plugged to False mid-task before gate/merge,
    # result_summary must record merged=False!
    assert "merged=False" in (updated_task.result_summary or "")


# ---------------------------------------------------------------------------
# 3. _build_venv_guidance Windows (.venv/Scripts/) & repo_path fallback
# ---------------------------------------------------------------------------


def test_build_venv_guidance_emits_scripts_on_windows_layout(tmp_path: Path) -> None:
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    config = ConclaveConfig()
    config.execution.baseline_test_command = "pytest -q"
    knowledge = {"commands": {"lint": "ruff check src tests", "check": "mypy"}}

    guidance = _build_venv_guidance(tmp_path, config, knowledge)

    assert ".venv/Scripts/pytest -q" in guidance
    assert ".venv/Scripts/ruff check src tests" in guidance
    assert ".venv/Scripts/mypy" in guidance
    assert ".venv/bin/" not in guidance


def test_build_venv_guidance_emits_scripts_when_no_commands_configured(
    tmp_path: Path,
) -> None:
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    config = ConclaveConfig()

    guidance = _build_venv_guidance(tmp_path, config, None)

    assert ".venv/Scripts/" in guidance
    assert ".venv/bin/" not in guidance


def test_build_venv_guidance_checks_repo_path_venv(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    repo_path = tmp_path / "repo"
    (repo_path / ".venv" / "Scripts").mkdir(parents=True)

    config = ConclaveConfig()
    config.execution.baseline_test_command = "pytest -q"

    bus_db_dummy: Any = None
    orch = Orchestrator(bus_db_dummy, bus_db_dummy, bus_db_dummy, tmp_path / "home")
    orch._repo_path = repo_path

    guidance = orch._build_venv_guidance(worktree, config, None)
    assert ".venv/Scripts/pytest -q" in guidance
    assert ".venv/bin/" not in guidance


# ---------------------------------------------------------------------------
# 4. run_shell Auto-PATH Injection (.venv/Scripts, .venv/bin, sys.executable)
# ---------------------------------------------------------------------------


async def test_run_shell_prepends_venv_and_sys_executable_to_path(
    tmp_path: Path,
) -> None:
    scripts_dir = tmp_path / ".venv" / "Scripts"
    bin_dir = tmp_path / ".venv" / "bin"
    scripts_dir.mkdir(parents=True)
    bin_dir.mkdir(parents=True)

    captured_env: dict[str, str] = {}

    class _FakeProcess:
        returncode = 0
        pid = 1234

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"ok\n", b""

    async def _fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        if "env" in kwargs and kwargs["env"] is not None:
            captured_env.update(kwargs["env"])
        return _FakeProcess()

    async def _fake_shell(*args: Any, **kwargs: Any) -> _FakeProcess:
        if "env" in kwargs and kwargs["env"] is not None:
            captured_env.update(kwargs["env"])
        return _FakeProcess()

    with (
        patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
        patch("asyncio.create_subprocess_shell", side_effect=_fake_shell),
    ):
        rc, out = await run_shell(tmp_path, "pytest -q")

    assert rc == 0
    assert out.strip() == "ok"
    path_entries = captured_env.get("PATH", "").split(os.pathsep)
    assert str(scripts_dir) in path_entries
    assert str(bin_dir) in path_entries
    assert str(Path(sys.executable).parent) in path_entries
    # .venv/Scripts and .venv/bin must appear at the very front of PATH
    assert path_entries[0] == str(scripts_dir)
    assert path_entries[1] == str(bin_dir)
