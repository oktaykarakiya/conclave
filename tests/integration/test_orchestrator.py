"""End-to-end orchestrator tests against a throwaway git repo + fake provider."""

from __future__ import annotations

import json
from pathlib import Path

from fake_provider import FakeProvider

from conclave.config import ArgMode
from conclave.db import Database, TaskState
from conclave.db import repositories as repo
from conclave.engine import Orchestrator, run_git
from conclave.events import EventBus
from conclave.providers import ResolvedProfile


async def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    await run_git(path, "config", "user.email", "test@example.com")
    await run_git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# test repo\n")
    await run_git(path, "add", "-A")
    await run_git(path, "commit", "-m", "initial commit")


async def _seed_l3_personas(db: Database) -> None:
    """Seed PM and Architect-as-Planner personas so AgentRunner injects their
    ``# Product Manager Agent`` / ``# Architect-as-Planner Agent`` headers into
    assembled prompts, which the FakeProvider keys on."""
    from conclave.agents import DEFAULT_PERSONAS

    for name in ("pm", "architect-as-planner"):
        role, text = DEFAULT_PERSONAS[name]
        if await repo.get_agent(db, name) is None:
            await repo.upsert_agent(db, name=name, role=role.value, persona_md=text)


async def test_happy_path_commits_and_merges(db: Database, tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )
    orchestrator = Orchestrator(db, EventBus(db), FakeProvider(), tmp_path / "home")

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True

    done = await repo.get_task(db, task.id)
    assert done is not None and done.state is TaskState.done

    code, out = await run_git(repo_path, "show", "main:FEATURE.txt")
    assert code == 0 and "done" in out

    verdicts = await repo.list_verdicts(db, task.id)
    assert {v.agent for v in verdicts} >= {"tester", "security", "reviewer"}
    assert all(v.verdict == "pass" for v in verdicts)

    types = {e.type for e in await repo.list_events(db, task_id=task.id)}
    assert {"task.started", "task.committed", "task.merged", "task.done"} <= types


async def test_planner_path_runs_and_persists_plan(db: Database, tmp_path: Path) -> None:
    # use_planner=True opts a short (trivial-length) request into planning: classify_level
    # bumps it to L1, so _maybe_plan runs the one-shot planner (exercising the FakeProvider's
    # plan branch, otherwise dead). Locks in the explicit-opt-in path after the level refactor.
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file",
        state=TaskState.approved, use_planner=True,
    )
    provider = FakeProvider()
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True

    # (a) the PLAN preamble reached a downstream (developer) prompt
    assert any("=== PLAN (from the Planner" in p for p in provider.prompts)

    # (b) a plan.artifact event was emitted for the task
    types = {e.type for e in await repo.list_events(db, task_id=task.id)}
    assert "plan.artifact" in types

    # (c) the parsed plan was persisted on the task, classified L1 (explicit opt-in)
    done = await repo.get_task(db, task.id)
    assert done is not None
    assert done.plan == {"approach": "create the file", "files_to_touch": ["FEATURE.txt"]}
    assert done.level == 1

    # (d) a single plan.level_selected event carried the L1 routing decision
    level_events = [
        e for e in await repo.list_events(db, task_id=task.id)
        if e.type == "plan.level_selected"
    ]
    assert len(level_events) == 1 and level_events[0].payload["level"] == 1

    # the planner path does not regress the happy path: still done + merged to main
    assert done.state is TaskState.done
    code, out = await run_git(repo_path, "show", "main:FEATURE.txt")
    assert code == 0 and "done" in out


async def test_trivial_request_classifies_l0_and_skips_planner(
    db: Database, tmp_path: Path
) -> None:
    # Regression canary: a short (<= L0 ceiling) request with use_planner unset takes the
    # trivial fast-path — level 0, the planner persona never runs, no plan persisted. The
    # four existing process_task tests all use such requests, so this pins their behavior.
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add a feature file", state=TaskState.approved
    )
    provider = FakeProvider()
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True

    done = await repo.get_task(db, task.id)
    assert done is not None
    assert done.level == 0
    assert done.plan is None  # planner did not run

    events = await repo.list_events(db, task_id=task.id)
    level_events = [e for e in events if e.type == "plan.level_selected"]
    assert len(level_events) == 1 and level_events[0].payload["level"] == 0
    # no plan.artifact event, and the planner prompt was never assembled
    assert "plan.artifact" not in {e.type for e in events}
    assert not any("Produce a structured plan" in p for p in provider.prompts)


async def test_long_request_classifies_l3_and_runs_planner(
    db: Database, tmp_path: Path
) -> None:
    # A ~500-char request (use_planner unset) classifies to L3; the three-agent sequential
    # L3 path (PM → Architect-as-Planner → Planner) runs with all l3_settings flags on.
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    await _seed_l3_personas(db)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )
    # Benign filler keeps the request clear of the FakeProvider marker substrings
    # ('Produce a structured plan' / 'Review the changes made for this task'), so the
    # fake routes by persona rather than misreading the long request text.
    task = await repo.create_task(
        db, project_id=project.id, request="x" * 500, state=TaskState.approved
    )
    provider = FakeProvider()
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True

    done = await repo.get_task(db, task.id)
    assert done is not None
    assert done.level == 3
    # L3 combined plan: three sections keyed by stage.
    assert done.plan == {
        "prd_lite": {
            "approach": "product-manager: define MVP scope", "files_to_touch": ["PRD.md"]
        },
        "architecture_note": {
            "approach": "architect: design system components",
            "files_to_touch": ["ARCHITECTURE.md"],
        },
        "story_plan": {
            "approach": "create the file", "files_to_touch": ["FEATURE.txt"]
        },
    }

    # the L3 PLAN preamble reached a downstream (developer) prompt
    assert any("=== L3 PLAN (Scale-Adaptive)" in p for p in provider.prompts)
    assert any("PRD-lite" in p for p in provider.prompts)
    assert any("Architecture Note" in p for p in provider.prompts)
    assert any("Story/Plan" in p for p in provider.prompts)

    level_events = [
        e for e in await repo.list_events(db, task_id=task.id)
        if e.type == "plan.level_selected"
    ]
    assert len(level_events) == 1 and level_events[0].payload["level"] == 3


async def test_green_gate_passes_after_developer_change(db: Database, tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={
            "execution": {"target_branch": "main", "baseline_test_command": "test -f FEATURE.txt"}
        },
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add feature", state=TaskState.approved
    )
    orchestrator = Orchestrator(
        db, EventBus(db), FakeProvider(developer_writes=True), tmp_path / "home"
    )
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True
    done = await repo.get_task(db, task.id)
    assert done is not None and done.state is TaskState.done


async def test_baseline_snapshot_event_carries_task_id(db: Database, tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={
            "execution": {"target_branch": "main", "baseline_test_command": "test -f FEATURE.txt"}
        },
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add feature", state=TaskState.approved
    )
    orchestrator = Orchestrator(
        db, EventBus(db), FakeProvider(developer_writes=True), tmp_path / "home"
    )
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True

    # The baseline snapshot must be attributed to the task so it shows in the
    # per-task event view (list_events filters on task_id).
    events = await repo.list_events(db, task_id=task.id)
    snapshots = [e for e in events if e.type == "baseline.snapshot"]
    assert snapshots, "expected a baseline.snapshot event scoped to the task"
    assert all(e.task_id == task.id for e in snapshots)


async def test_failure_when_gate_never_green(db: Database, tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={
            "execution": {"target_branch": "main", "baseline_test_command": "test -f FEATURE.txt"},
            "agent_overrides": {"developer": {"max_retries": 2}},
        },
    )
    task = await repo.create_task(
        db, project_id=project.id, request="add feature", state=TaskState.approved
    )
    # developer never writes the file => gate stays red => fail after retries
    orchestrator = Orchestrator(
        db, EventBus(db), FakeProvider(developer_writes=False), tmp_path / "home"
    )
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is False

    failed = await repo.get_task(db, task.id)
    assert failed is not None and failed.state is TaskState.failed
    # the failed task branch was cleaned up
    _, branches = await run_git(repo_path, "branch", "--list", f"conclave/{task.id}")
    assert branches.strip() == ""


# A request in the L2 band: [51,499] chars carrying BOTH 'implement' and 'feature' so
# classify_level routes it to level 2 (the highest matching band), and clear of the
# FakeProvider marker substrings ('Produce a structured plan' / 'Review the changes made
# for this task') so the fake still routes by persona rather than misreading the request.
_L2_REQUEST = "implement a new feature that adds a configurable widget to the analytics dashboard"


async def test_l2_missing_acceptance_criteria_adds_corrective_note(
    db: Database, tmp_path: Path
) -> None:
    # The default FakeProvider plan omits acceptance_criteria/risks; default l2_settings
    # demand both, so the L2 path folds a corrective note into the developer's preamble
    # (rather than failing the task) and the run still completes.
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )
    task = await repo.create_task(
        db, project_id=project.id, request=_L2_REQUEST, state=TaskState.approved
    )
    provider = FakeProvider()
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True

    done = await repo.get_task(db, task.id)
    assert done is not None
    assert done.level == 2
    assert done.state is TaskState.done

    # the corrective note for the omitted acceptance_criteria reached a developer prompt,
    # carried alongside the PLAN preamble (the enhanced L2 one-shot)
    assert any(
        "=== PLAN (from the Planner" in p and "L2 NOTE" in p and "acceptance_criteria" in p
        for p in provider.prompts
    )


async def test_l2_flags_off_is_plain_l1_oneshot(db: Database, tmp_path: Path) -> None:
    # With both l2_settings flags False the L2 path collapses to a plain L1 one-shot:
    # no demand appended to the planner prompt, the FakeProvider plan persisted as-is,
    # and no L2 corrective note anywhere.
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={
            "execution": {"target_branch": "main"},
            "planning": {
                "l2_settings": {
                    "require_acceptance_criteria": False,
                    "require_risk_assessment": False,
                }
            },
        },
    )
    task = await repo.create_task(
        db, project_id=project.id, request=_L2_REQUEST, state=TaskState.approved
    )
    provider = FakeProvider()
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True

    done = await repo.get_task(db, task.id)
    assert done is not None
    assert done.level == 2
    assert done.plan == {"approach": "create the file", "files_to_touch": ["FEATURE.txt"]}

    assert any("=== PLAN (from the Planner" in p for p in provider.prompts)
    assert not any("L2 NOTE" in p for p in provider.prompts)


async def test_l2_malformed_plan_degrades_to_empty_preamble(
    db: Database, tmp_path: Path
) -> None:
    # A malformed (fence-less) planner reply makes _dispatch_plan return None; the L2 path
    # degrades exactly like L1 — empty preamble, no plan persisted/emitted — without raising.
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )
    task = await repo.create_task(
        db, project_id=project.id, request=_L2_REQUEST, state=TaskState.approved
    )
    provider = FakeProvider(plan_malformed=True)
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True

    done = await repo.get_task(db, task.id)
    assert done is not None
    assert done.level == 2
    assert done.plan is None

    types = {e.type for e in await repo.list_events(db, task_id=task.id)}
    assert "plan.artifact" not in types
    assert not any("=== PLAN" in p or "L2 NOTE" in p for p in provider.prompts)


async def test_crash_recovery_returns_in_progress_to_approved(db: Database, tmp_path: Path) -> None:
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )
    await repo.create_task(db, project_id=project.id, request="x", state=TaskState.approved)
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None and claimed.state is TaskState.in_progress

    orchestrator = Orchestrator(db, EventBus(db), FakeProvider(), tmp_path / "home")
    assert await orchestrator.recover(project.id) == 1
    again = await repo.get_task(db, claimed.id)
    assert again is not None and again.state is TaskState.approved


# ---------------------------------------------------------------------------
# Scale-adaptive planning persona + decomposition tests (FakeProvider direct)
# ---------------------------------------------------------------------------


async def test_planning_personas_yield_distinct_artifacts() -> None:
    """Each planning-persona marker yields its OWN distinct artifact.

    The '# ' prefix disambiguates 'Test-Architect Agent' from 'Architect Agent'
    (the former is a superstring of the latter).  This test proves the routing
    keys don't collide and that each persona returns a unique plan.
    """
    provider = FakeProvider()
    profile = ResolvedProfile(name="fake", arg_mode=ArgMode.inherit)

    pm = await provider.run_agent(
        profile=profile,
        prompt="# Product Manager Agent\nWrite a short PRD-lite note.",
        timeout_seconds=30,
    )
    arch = await provider.run_agent(
        profile=profile,
        prompt="# Architect-as-Planner Agent\nProduce an architecture note.",
        timeout_seconds=30,
    )
    ta = await provider.run_agent(
        profile=profile,
        prompt="# Test-Architect Agent (Scale-Adaptive Planning)\nOutline a test strategy.",
        timeout_seconds=30,
    )

    # Each persona returns distinct text from every other.
    texts = {pm.text, arch.text, ta.text}
    assert len(texts) == 3, f"Expected 3 distinct artifacts, got {len(texts)}"

    # None collided with the default _PLAN (the generic one-shot planner).
    plan = await provider.run_agent(
        profile=profile,
        prompt="Produce a structured plan for the feature.",
        timeout_seconds=30,
    )
    assert plan.text not in texts, "Persona artifact collided with default _PLAN"


async def test_epic_decomposition_yields_child_tasks() -> None:
    """The decompose marker yields parseable child_tasks JSON with 3 entries."""
    provider = FakeProvider()
    profile = ResolvedProfile(name="fake", arg_mode=ArgMode.inherit)

    result = await provider.run_agent(
        profile=profile,
        prompt="Decompose this epic into child tasks for the dashboard feature.",
        timeout_seconds=30,
    )

    # Strip ```json fences and parse.
    json_text = result.text.replace("```json\n", "").replace("\n```", "")
    data = json.loads(json_text)
    child_tasks = data["child_tasks"]
    assert isinstance(child_tasks, list)
    assert len(child_tasks) == 3
    for task in child_tasks:
        assert isinstance(task["title"], str)
        assert isinstance(task["description"], str)


async def test_empty_decomposition_returns_empty_child_tasks() -> None:
    """empty_decomposition=True yields child_tasks: [] (still valid JSON)."""
    provider = FakeProvider(empty_decomposition=True)
    profile = ResolvedProfile(name="fake", arg_mode=ArgMode.inherit)

    result = await provider.run_agent(
        profile=profile,
        prompt="Decompose this epic into child tasks.",
        timeout_seconds=30,
    )

    json_text = result.text.replace("```json\n", "").replace("\n```", "")
    data = json.loads(json_text)
    assert data == {"child_tasks": []}


async def test_malformed_decompose_returns_no_json() -> None:
    """plan_malformed=True with the decompose marker yields fence-less text —
    no parseable ```json block anywhere in the output."""
    provider = FakeProvider(plan_malformed=True)
    profile = ResolvedProfile(name="fake", arg_mode=ArgMode.inherit)

    result = await provider.run_agent(
        profile=profile,
        prompt="Decompose this epic into child tasks.",
        timeout_seconds=30,
    )

    # There must be no ```json fence to parse.
    assert "```json" not in result.text
    assert "child_tasks" not in result.text
    # The text should be the raw degradation message.
    assert "No decomposition produced." in result.text


# ---------------------------------------------------------------------------
# Scale-adaptive planning L3 integration tests
# ---------------------------------------------------------------------------

# L3 band filler: 500 chars, clear of FakeProvider marker substrings.
_L3_REQUEST = "x" * 500


async def test_l3_sequential_dispatch_order(db: Database, tmp_path: Path) -> None:
    """Acceptance criterion (a): for a level-3 task the three planning agents are
    dispatched IN ORDER — PM → Architect-as-Planner → Planner — asserted via
    prompts.index() using each persona's distinct header."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    await _seed_l3_personas(db)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )
    await repo.create_task(
        db, project_id=project.id, request=_L3_REQUEST, state=TaskState.approved
    )
    provider = FakeProvider()
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True

    prompts = provider.prompts
    pm_idx = next(
        i for i, p in enumerate(prompts) if "# Product Manager Agent" in p
    )
    arch_idx = next(
        i for i, p in enumerate(prompts) if "# Architect-as-Planner Agent" in p
    )
    plan_idx = next(
        i for i, p in enumerate(prompts) if "Produce a structured plan" in p
    )
    assert (
        pm_idx < arch_idx < plan_idx
    ), f"Expected PM({pm_idx}) < Arch({arch_idx}) < Planner({plan_idx})"


async def test_l3_preamble_contains_all_enabled_sections(
    db: Database, tmp_path: Path
) -> None:
    """Acceptance criterion (b): the assembled developer preamble contains all
    enabled section labels (PRD-lite, Architecture Note, Story/Plan) when all
    l3_settings flags are on."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    await _seed_l3_personas(db)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )
    await repo.create_task(
        db, project_id=project.id, request=_L3_REQUEST, state=TaskState.approved
    )
    provider = FakeProvider()
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True

    # The developer prompt (and only it) carries the L3 preamble with all sections.
    dev_prompts = [
        p for p in provider.prompts if "=== L3 PLAN (Scale-Adaptive)" in p
    ]
    assert dev_prompts, "expected a developer prompt carrying the L3 preamble"
    dev = dev_prompts[0]
    assert "--- PRD-lite ---" in dev
    assert "--- Architecture Note ---" in dev
    assert "--- Story/Plan ---" in dev


async def test_l3_flag_toggle_removes_stage(db: Database, tmp_path: Path) -> None:
    """Acceptance criterion (c): toggling produce_arch_note off removes exactly
    that stage — no Architect header in prompts, and the preamble lacks the
    Architecture Note section."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    await _seed_l3_personas(db)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={
            "execution": {"target_branch": "main"},
            "planning": {"l3_settings": {"produce_arch_note": False}},
        },
    )
    task = await repo.create_task(
        db, project_id=project.id, request=_L3_REQUEST, state=TaskState.approved
    )
    provider = FakeProvider()
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True

    # Architect persona header never appeared in any prompt.
    assert not any(
        "# Architect-as-Planner Agent" in p for p in provider.prompts
    ), "Architect-as-Planner was dispatched despite produce_arch_note=False"

    # PM and Planner WERE dispatched.
    assert any(
        "# Product Manager Agent" in p for p in provider.prompts
    ), "PM was not dispatched"
    assert any(
        "Produce a structured plan" in p for p in provider.prompts
    ), "Planner was not dispatched"

    # The developer preamble contains PRD-lite and Story/Plan but NOT Architecture Note.
    dev_prompts = [
        p for p in provider.prompts if "=== L3 PLAN (Scale-Adaptive)" in p
    ]
    assert dev_prompts, "expected a developer prompt carrying the L3 preamble"
    dev = dev_prompts[0]
    assert "--- PRD-lite ---" in dev
    assert "--- Story/Plan ---" in dev
    assert "--- Architecture Note ---" not in dev

    # Combined plan lacks the architecture_note key.
    done = await repo.get_task(db, task.id)
    assert done is not None
    assert done.plan is not None
    assert "prd_lite" in done.plan
    assert "story_plan" in done.plan
    assert "architecture_note" not in done.plan


async def test_l3_final_stage_none_degradation(
    db: Database, tmp_path: Path
) -> None:
    """Acceptance criterion (d): when the final planner stage returns None
    (plan_malformed=True), the path degrades to an L1-style empty preamble
    without raising — no plan persisted, no plan.artifact event, no '=== PLAN'
    in any developer prompt."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    await _seed_l3_personas(db)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )
    task = await repo.create_task(
        db, project_id=project.id, request=_L3_REQUEST, state=TaskState.approved
    )
    provider = FakeProvider(plan_malformed=True)
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    # The run still succeeds — L3 degradation is never a task failure.
    assert await orchestrator.process_task(claimed) is True

    done = await repo.get_task(db, task.id)
    assert done is not None
    assert done.level == 3
    # No plan persisted (L1 degradation path).
    assert done.plan is None

    # No plan.artifact event was emitted.
    types = {e.type for e in await repo.list_events(db, task_id=task.id)}
    assert "plan.artifact" not in types

    # No '=== PLAN' block in any prompt (empty preamble).
    assert not any("=== PLAN" in p for p in provider.prompts)


# ---------------------------------------------------------------------------
# Scale-adaptive planning L4 integration tests
# ---------------------------------------------------------------------------

# L4 band filler: 1000 chars, clear of FakeProvider marker substrings so routing
# stays persona-based (decomposer detects "Decompose this epic into child tasks").
_L4_REQUEST = "x" * 1000


async def test_l4_epic_decomposes_into_children_and_short_circuits(
    db: Database, tmp_path: Path
) -> None:
    """Acceptance criterion: a level-4 task with auto_create_children=True
    decomposes into child tasks, short-circuits the dev loop, and leaves the
    epic terminal (done)."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )
    task = await repo.create_task(
        db, project_id=project.id, request=_L4_REQUEST, state=TaskState.approved
    )
    provider = FakeProvider()
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True

    # Epic is classified L4 and marked done.
    done = await repo.get_task(db, task.id)
    assert done is not None
    assert done.level == 4
    assert done.state is TaskState.done

    # Child tasks created in inbox with parent=epic.id.
    all_tasks = await repo.list_tasks(db, project_id=project.id)
    children = [t for t in all_tasks if t.parent_task_id == task.id]
    assert len(children) == 3  # _DECOMPOSE returns 3 entries
    for child in children:
        assert child.state == TaskState.inbox
        assert child.parent_task_id == task.id
        assert child.title
        assert child.request

    # plan.artifact event was emitted with the decomposition JSON.
    events = await repo.list_events(db, task_id=task.id)
    plan_events = [e for e in events if e.type == "plan.artifact"]
    assert len(plan_events) == 1, f"expected 1 plan.artifact, got {len(plan_events)}"
    artifact = plan_events[0].payload["plan"]
    assert "child_tasks" in artifact
    assert len(artifact["child_tasks"]) == 3

    # plan.decomposition_complete event emitted with child_count and child_ids.
    complete_events = [e for e in events if e.type == "plan.decomposition_complete"]
    assert len(complete_events) == 1
    assert complete_events[0].payload["child_count"] == 3
    assert len(complete_events[0].payload["child_ids"]) == 3

    # NO developer dispatch event — the dev loop never ran.
    dev_dispatches = [
        e for e in events
        if e.type == "agent.dispatched" and e.agent == "developer"
    ]
    assert len(dev_dispatches) == 0, "Developer was dispatched but should not be"

    # plan.decomposition_fallback was NOT emitted.
    assert not any(
        e.type == "plan.decomposition_fallback" for e in events
    )


async def test_l4_empty_decomposition_falls_back_to_l1(
    db: Database, tmp_path: Path
) -> None:
    """Acceptance criterion: a level-4 task with empty decomposition creates zero
    children, emits the fallback event, and runs the developer loop as L1."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )
    task = await repo.create_task(
        db, project_id=project.id, request=_L4_REQUEST, state=TaskState.approved
    )
    provider = FakeProvider(empty_decomposition=True)
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True

    done = await repo.get_task(db, task.id)
    assert done is not None
    assert done.level == 4
    # Task completes through the normal L1 developer/review loop.
    assert done.state is TaskState.done

    # Zero children created — decomposition was empty.
    all_tasks = await repo.list_tasks(db, project_id=project.id)
    children = [t for t in all_tasks if t.parent_task_id == task.id]
    assert len(children) == 0

    # plan.decomposition_fallback event was emitted.
    events = await repo.list_events(db, task_id=task.id)
    fallback_events = [
        e for e in events if e.type == "plan.decomposition_fallback"
    ]
    assert len(fallback_events) == 1

    # plan.decomposition_complete was NOT emitted.
    assert not any(
        e.type == "plan.decomposition_complete" for e in events
    )

    # Developer WAS dispatched — dev loop ran as L1 fallback.
    dev_dispatches = [
        e for e in events
        if e.type == "agent.dispatched" and e.agent == "developer"
    ]
    assert len(dev_dispatches) >= 1, "Developer should have been dispatched"


async def test_l4_malformed_decomposition_falls_back_to_l1(
    db: Database, tmp_path: Path
) -> None:
    """Acceptance criterion: a level-4 task whose decomposer dispatch returns
    malformed output (no JSON fence) falls back to the L1 developer loop."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={"execution": {"target_branch": "main"}},
    )
    task = await repo.create_task(
        db, project_id=project.id, request=_L4_REQUEST, state=TaskState.approved
    )
    provider = FakeProvider(plan_malformed=True)
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True

    done = await repo.get_task(db, task.id)
    assert done is not None
    assert done.level == 4
    assert done.state is TaskState.done

    # Zero children — dispatch returned None.
    all_tasks = await repo.list_tasks(db, project_id=project.id)
    children = [t for t in all_tasks if t.parent_task_id == task.id]
    assert len(children) == 0

    # plan.decomposition_fallback event with dispatch reason.
    events = await repo.list_events(db, task_id=task.id)
    fallback_events = [
        e for e in events if e.type == "plan.decomposition_fallback"
    ]
    assert len(fallback_events) == 1
    assert "dispatch" in fallback_events[0].payload["reason"]

    # Developer WAS dispatched — dev loop ran.
    dev_dispatches = [
        e for e in events
        if e.type == "agent.dispatched" and e.agent == "developer"
    ]
    assert len(dev_dispatches) >= 1, "Developer should have been dispatched"


async def test_l4_auto_create_children_false_treats_as_l1(
    db: Database, tmp_path: Path
) -> None:
    """A level-4 task with auto_create_children=False degrades to L1: no
    decomposer dispatch, no children, dev loop runs normally."""
    repo_path = tmp_path / "repo"
    await _init_repo(repo_path)
    project = await repo.create_project(
        db, name="t", path=str(repo_path), default_branch="main",
        config={
            "execution": {"target_branch": "main"},
            "planning": {"l4_settings": {"auto_create_children": False}},
        },
    )
    task = await repo.create_task(
        db, project_id=project.id, request=_L4_REQUEST, state=TaskState.approved
    )
    provider = FakeProvider()
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")

    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    assert await orchestrator.process_task(claimed) is True

    done = await repo.get_task(db, task.id)
    assert done is not None
    assert done.level == 4
    assert done.state is TaskState.done

    # Zero children — decomposer never ran.
    all_tasks = await repo.list_tasks(db, project_id=project.id)
    children = [t for t in all_tasks if t.parent_task_id == task.id]
    assert len(children) == 0

    # No decomposition events at all.
    events = await repo.list_events(db, task_id=task.id)
    assert not any(
        e.type in ("plan.decomposition_complete", "plan.decomposition_fallback")
        for e in events
    )

    # Decomposer prompt was never assembled.
    assert not any("Decompose this epic into child tasks" in p for p in provider.prompts)

    # Developer was dispatched (L1 path ran).
    dev_dispatches = [
        e for e in events
        if e.type == "agent.dispatched" and e.agent == "developer"
    ]
    assert len(dev_dispatches) >= 1, "Developer should have been dispatched"
