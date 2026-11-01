"""Prompt context end to end: irreplaceable content reaches agents whole, recoverable bulk is
fitted per dispatching agent with a visible marker and exactly one Live event per cut.

Every test runs the real orchestrator against a throwaway git repo and the deterministic
FakeProvider (zero LLM cost), asserting on the full prompts the provider received.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from fake_provider import FakeProvider
from httpx import ASGITransport

from conclave.config import (
    EdgeCondition,
    NodeKind,
    WorkflowEdge,
    WorkflowGraph,
    WorkflowNode,
    load_project_config,
)
from conclave.db import Database, EventRow, Task, TaskState
from conclave.db import repositories as repo
from conclave.engine import Orchestrator, run_git
from conclave.engine.compactor import compact_task_context
from conclave.engine.runner import AgentRunner
from conclave.events import EventBus
from conclave.mcp.server import handle_mcp_message
from conclave.runtime import Daemon
from conclave.web import create_app

_MARKER_RE = re.compile(
    r"\[\.\.\. (\d+) of (\d+) chars omitted \(limit: context\.(\w+) = (\d+)\)\. (.+?) \.\.\.\]"
)
_REVIEW = "Review the changes made for this task"
_PLAN = "Produce a structured plan"


def _neutral_text(size: int) -> str:
    """*size* chars of multi-line prose that contains none of FakeProvider's routing phrases."""
    line = "The importer keeps every record byte-identical and reports each skipped row.\n"
    text = "".join(f"{i:05d} {line}" for i in range(size // len(line) + 1))
    return text[:size]


def _numbered_output(first: str, last: str, lines: int = 400) -> str:
    """Test-runner-like output whose first and last lines are easy to find."""
    body = "".join(f"case {i:04d}: expected value differs from the fixture\n" for i in range(lines))
    return f"{first}\n{body}{last}\n"


async def _init_repo(path: Path, files: dict[str, str] | None = None) -> str:
    """Create a repo with one commit holding README.md plus *files*; return its SHA."""
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    await run_git(path, "config", "user.email", "test@example.com")
    await run_git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# test repo\n", encoding="utf-8")
    for name, content in (files or {}).items():
        (path / name).write_text(content, encoding="utf-8")
    await run_git(path, "add", "-A")
    await run_git(path, "commit", "-m", "initial commit")
    _, sha = await run_git(path, "rev-parse", "HEAD")
    return sha.strip()


async def _run_task(
    db: Database,
    tmp_path: Path,
    provider: FakeProvider,
    config: dict[str, Any],
    *,
    request: str = "add a feature file",
    use_planner: bool | None = None,
) -> tuple[Task, bool]:
    """Attach ``tmp_path/repo`` with *config*, process one approved task, return it + result."""
    project = await repo.create_project(
        db, name="t", path=str(tmp_path / "repo"), default_branch="main",
        config={"execution": {"target_branch": "main"}, **config},
    )
    task = await repo.create_task(
        db, project_id=project.id, request=request, state=TaskState.approved,
        use_planner=use_planner,
    )
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")
    claimed = await repo.claim_next_approved(db, project.id)
    assert claimed is not None
    ok = await orchestrator.process_task(claimed)
    final = await repo.get_task(db, task.id)
    assert final is not None
    return final, ok


def _prompts_of(provider: FakeProvider, agent: str) -> list[str]:
    """Prompts dispatched to *agent* (no seeded personas, so the default system line names it)."""
    return [p for p in provider.prompts if f"You are the {agent} agent of" in p]


async def _cuts(db: Database, task_id: str) -> list[dict[str, Any]]:
    events: list[EventRow] = await repo.list_events(db, task_id=task_id, limit=10_000)
    return [
        e.payload for e in events
        if e.type == "log" and e.payload.get("stage") == "context_truncated"
    ]


async def _pipeline(db: Database, task_id: str) -> list[str]:
    events = await repo.list_events(db, task_id=task_id, limit=10_000)
    derived = [e.payload["pipeline"] for e in events if e.type == "pipeline.derived"]
    assert derived
    return [str(a) for a in derived[0]]


# --- irreplaceable content ----------------------------------------------------


async def test_long_request_reaches_planner_developer_and_every_reviewer_verbatim(
    db: Database, tmp_path: Path
) -> None:
    await _init_repo(tmp_path / "repo")
    request = _neutral_text(30_000)
    provider = FakeProvider()

    task, ok = await _run_task(db, tmp_path, provider, {}, request=request)

    assert ok is True and task.state is TaskState.done
    planner = [p for p in provider.prompts if _PLAN in p]
    developer = _prompts_of(provider, "developer")
    reviewers = [p for p in provider.prompts if _REVIEW in p]
    assert len(planner) == 1 and len(developer) == 1
    assert len(reviewers) >= 3
    assert f"TASK:\n{request}" in planner[0]
    assert f"TASK:\n{request}" in developer[0]
    for prompt in reviewers:
        assert f"TASK:\n{_REVIEW}:\n{request}" in prompt
    assert all("[... compacted" not in p for p in provider.prompts)
    assert await _cuts(db, task.id) == []


async def test_runner_sends_the_prompt_unchanged_even_with_compaction_on(
    db: Database, tmp_path: Path
) -> None:
    project = await repo.create_project(
        db, name="p", path=str(tmp_path), default_branch="main"
    )
    config = load_project_config(
        {"agent_defaults": {"compact_before_start": True}, "context": {"compaction_enabled": True}}
    )
    provider = FakeProvider(developer_writes=False)
    runner = AgentRunner(db, EventBus(db), provider, project.id, config)
    prompt = _neutral_text(30_000)

    await runner.run(agent="developer", prompt=prompt, worktree=tmp_path)

    assert len(provider.prompts) == 1
    assert f"TASK:\n{prompt}\n" in provider.prompts[0]
    assert "[... compacted" not in provider.prompts[0]


async def test_compact_task_context_never_rewrites_the_plan(db: Database) -> None:
    project = await repo.create_project(db, name="p", path="/tmp/p", default_branch="main")
    request = "Refactor the importer.\n" + "x" * 4_000
    task = await repo.create_task(db, project_id=project.id, request=request)
    plan = {
        "approach": "a" * 2_000,
        "files_to_touch": ["src/importer.py"],
        "steps": ["b" * 900, {"detail": "c" * 700}],
    }
    await repo.update_task_fields(db, task.id, plan=plan)

    result = await compact_task_context(db, task.id)

    assert result["compacted"] is True
    assert result["chars_before"] > result["chars_after"]
    after = await repo.get_task(db, task.id)
    assert after is not None and after.plan == plan and after.request == request
    compaction = [
        cp for cp in await repo.list_task_checkpoints(db, task.id) if cp.node_id == "compaction"
    ]
    assert len(compaction) == 1
    assert compaction[0].artifact["chars_before"] == result["chars_before"]


# --- recoverable bulk: diffs ------------------------------------------------------


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel"])
async def test_oversized_diff_reaches_every_reviewer_fitted_with_one_event(
    db: Database, tmp_path: Path, parallel: bool
) -> None:
    base = await _init_repo(tmp_path / "repo")
    provider = FakeProvider(developer_content=_neutral_text(12_000))

    task, ok = await _run_task(
        db, tmp_path, provider,
        {
            "execution": {"target_branch": "main", "parallel_reviewers": parallel},
            "context": {"bulk_max_chars": 1_000},
        },
    )

    assert ok is True and task.state is TaskState.done
    reviewers = [p for p in provider.prompts if _REVIEW in p]
    assert len(reviewers) >= 3
    hint = f"Run `git diff --cached {base}` in your working directory for the full diff."
    for prompt in reviewers:
        assert "\n\nDIFF:\n```diff\n" in prompt
        match = _MARKER_RE.search(prompt)
        assert match is not None
        assert match.group(3, 4, 5) == ("bulk_max_chars", "1000", hint)

    cuts = await _cuts(db, task.id)
    assert len(cuts) == 1
    cut = cuts[0]
    first_reviewer = (await _pipeline(db, task.id))[0]
    assert (cut["block"], cut["field"], cut["agent"], cut["node_id"]) == (
        "diff", "bulk_max_chars", first_reviewer, first_reviewer
    )
    assert cut["kept_chars"] <= 1_000 < cut["original_chars"]
    # The command in the marker reproduces the whole diff the reviewers were shown a part of.
    _, merged_diff = await run_git(tmp_path / "repo", "diff", base, "main")
    assert len(merged_diff) == cut["original_chars"]


async def test_compaction_for_one_agent_only_fits_only_that_agents_bulk(
    db: Database, tmp_path: Path
) -> None:
    base = await _init_repo(tmp_path / "repo")
    provider = FakeProvider(developer_content=_neutral_text(12_000))

    task, ok = await _run_task(
        db, tmp_path, provider,
        {
            "context": {"compaction_max_chars": 1_000},
            "agent_overrides": {"security": {"compact_before_start": True}},
        },
    )

    assert ok is True
    _, full_diff = await run_git(tmp_path / "repo", "diff", base, "main")
    assert len(full_diff) > 12_000
    security = _prompts_of(provider, "security")
    assert len(security) == 1
    match = _MARKER_RE.search(security[0])
    assert match is not None
    assert match.group(3, 4) == ("compaction_max_chars", "1000")
    others = [p for p in provider.prompts if _REVIEW in p and p not in security]
    assert len(others) >= 2
    for prompt in others:
        assert f"DIFF:\n```diff\n{full_diff}\n```" in prompt
        assert _MARKER_RE.search(prompt) is None

    cuts = await _cuts(db, task.id)
    assert [(c["block"], c["field"], c["agent"]) for c in cuts] == [
        ("diff", "compaction_max_chars", "security")
    ]


async def test_previous_diff_is_fitted_and_recoverable_with_the_command_in_its_marker(
    db: Database, tmp_path: Path
) -> None:
    base = await _init_repo(tmp_path / "repo")
    provider = FakeProvider(developer_content=_neutral_text(12_000))

    task, ok = await _run_task(
        db, tmp_path, provider,
        {
            "execution": {
                "target_branch": "main",
                "baseline_test_command": "if [ -f FEATURE.txt ]; then echo broken; exit 1; fi",
            },
            "context": {"bulk_max_chars": 1_000},
            "agent_overrides": {"developer": {"max_retries": 2}},
        },
    )

    assert ok is False and task.state is TaskState.failed
    developer = _prompts_of(provider, "developer")
    assert len(developer) == 2
    retry = developer[1]
    prior = retry.split("YOUR IMMEDIATELY PRIOR DIFF:\n```diff\n", 1)[1]
    match = _MARKER_RE.search(prior)
    assert match is not None
    assert match.group(3, 4) == ("bulk_max_chars", "1000")
    command = re.fullmatch(
        r"Run `git diff (\S+) (\S+)` in your working directory for the full previous diff\.",
        match.group(5),
    )
    assert command is not None
    checkpoint, tree = command.groups()
    assert checkpoint == base

    cuts = [c for c in await _cuts(db, task.id) if c["block"] == "previous_diff"]
    assert len(cuts) == 1
    assert (cuts[0]["agent"], cuts[0]["node_id"]) == ("developer", "developer")
    # The previous attempt was reset, yet its full diff is still recoverable.
    rc, recovered = await run_git(tmp_path / "repo", "diff", checkpoint, tree)
    assert rc == 0 and len(recovered) == cuts[0]["original_chars"]


# --- recoverable bulk: gate, baseline and setup output ------------------------------------


async def test_gate_output_keeps_its_first_and_last_line_in_the_developer_feedback(
    db: Database, tmp_path: Path
) -> None:
    output = _numbered_output("FIRST failure: test_import_alpha", "LAST summary: 401 failed")
    await _init_repo(tmp_path / "repo", {"gate_output.txt": output})
    command = "if [ -f FEATURE.txt ]; then cat gate_output.txt; exit 1; fi"
    provider = FakeProvider()

    task, ok = await _run_task(
        db, tmp_path, provider,
        {
            "execution": {"target_branch": "main", "baseline_test_command": command},
            "context": {"bulk_max_chars": 1_000},
            "agent_overrides": {"developer": {"max_retries": 2}},
        },
    )

    assert ok is False
    retry = _prompts_of(provider, "developer")[1]
    feedback = retry.split("LATEST REVIEWER FEEDBACK (must fix these issues):\n", 1)[1]
    assert feedback.startswith("TEST GATE is not green (exit 1). Fix the failing test:\n")
    assert "FIRST failure: test_import_alpha" in feedback
    assert "LAST summary: 401 failed" in feedback
    match = _MARKER_RE.search(feedback)
    assert match is not None
    assert match.group(3, 4) == ("bulk_max_chars", "1000")
    assert match.group(5) == (
        f"Re-run `{command}` in your working directory for the full output."
    )

    gate_cuts = [c for c in await _cuts(db, task.id) if c["block"] == "gate_output"]
    assert len(gate_cuts) == 2  # one per failed gate
    assert all(c["agent"] == "developer" for c in gate_cuts)


async def test_baseline_output_is_cached_whole_and_fitted_with_a_marker_for_each_reader(
    db: Database, tmp_path: Path
) -> None:
    output = _numbered_output("FIRST baseline failure: test_legacy", "LAST baseline: 401 failed")
    base = await _init_repo(tmp_path / "repo", {"baseline_output.txt": output})
    command = "if [ ! -f FEATURE.txt ]; then cat baseline_output.txt; exit 1; fi"
    provider = FakeProvider()

    task, ok = await _run_task(
        db, tmp_path, provider,
        {
            "execution": {"target_branch": "main", "baseline_test_command": command},
            "context": {"bulk_max_chars": 1_000},
        },
        use_planner=True,
    )

    assert ok is True and task.state is TaskState.done
    cached = await repo.get_baseline(db, task.project_id, base)
    assert cached is not None
    assert cached.output.splitlines() == output.splitlines()  # nothing cut before caching
    planner = [p for p in provider.prompts if _PLAN in p]
    developer = _prompts_of(provider, "developer")
    assert len(planner) == 1 and len(developer) == 1
    hint = (
        f"Re-run `{command}` in your working directory before changing anything for the "
        "full output."
    )
    for prompt in (planner[0], developer[0]):
        preamble = prompt.split("PRE-EXISTING TEST FAILURES on `main`", 1)[1]
        assert "FIRST baseline failure: test_legacy" in preamble
        assert "LAST baseline: 401 failed" in preamble
        match = _MARKER_RE.search(preamble)
        assert match is not None
        assert match.group(3, 4, 5) == ("bulk_max_chars", "1000", hint)

    cuts = await _cuts(db, task.id)
    assert [(c["block"], c["agent"]) for c in cuts] == [("gate_output", "planner")]
    assert cuts[0]["original_chars"] == len(cached.output)


async def test_setup_failure_reason_carries_fitted_output_with_its_marker(
    db: Database, tmp_path: Path
) -> None:
    output = _numbered_output("FIRST setup error: resolver", "LAST setup: install aborted")
    await _init_repo(tmp_path / "repo", {"setup_output.txt": output})
    command = "cat setup_output.txt; exit 3"

    task, ok = await _run_task(
        db, tmp_path, FakeProvider(),
        {
            "execution": {"target_branch": "main", "setup_command": command},
            "context": {"bulk_max_chars": 1_000},
        },
    )

    assert ok is False and task.state is TaskState.failed
    summary = task.result_summary or ""
    assert summary.startswith("worktree setup failed (exit 3):\nFIRST setup error: resolver")
    assert "LAST setup: install aborted" in summary
    match = _MARKER_RE.search(summary)
    assert match is not None
    assert match.group(3, 4, 5) == (
        "bulk_max_chars", "1000",
        f"Re-run `{command}` in your working directory for the full output.",
    )
    cuts = await _cuts(db, task.id)
    assert [(c["block"], c["agent"], c["node_id"]) for c in cuts] == [
        ("setup_output", None, None)
    ]


async def test_pass_through_gate_output_is_fitted_per_post_gate_reader(
    db: Database, tmp_path: Path
) -> None:
    output = _numbered_output("FIRST audit failure", "LAST audit summary", lines=100)
    await _init_repo(tmp_path / "repo", {"gate_output.txt": output})
    workflow = WorkflowGraph(
        nodes=[
            WorkflowNode(id="reviewer", kind=NodeKind.reviewer, agent="reviewer"),
            WorkflowNode(id="gate", kind=NodeKind.gate),
            WorkflowNode(
                id="submitter-a", kind=NodeKind.custom, agent="submitter-a",
                compact_before_start=True,
            ),
            WorkflowNode(id="submitter-b", kind=NodeKind.custom, agent="submitter-b"),
        ],
        edges=[
            WorkflowEdge(id="e1", source="reviewer", target="gate"),
            WorkflowEdge(
                id="e2", source="gate", target="submitter-a", condition=EdgeCondition.always
            ),
            WorkflowEdge(
                id="e3", source="gate", target="submitter-b", condition=EdgeCondition.always
            ),
        ],
    )
    provider = FakeProvider()

    task, ok = await _run_task(
        db, tmp_path, provider,
        {
            "execution": {
                "target_branch": "main",
                "baseline_test_command": "cat gate_output.txt; exit 1",
            },
            "context": {"compaction_max_chars": 1_000},
            "workflow": workflow.model_dump(mode="json"),
        },
    )

    assert ok is True and task.state is TaskState.done
    compacted = _prompts_of(provider, "submitter-a")
    whole = _prompts_of(provider, "submitter-b")
    assert len(compacted) == 1 and len(whole) == 1
    summary_a = compacted[0].split("REVIEW_VERDICTS_SUMMARY:\n", 1)[1]
    match = _MARKER_RE.search(summary_a)
    assert match is not None
    assert match.group(3, 4) == ("compaction_max_chars", "1000")
    assert "FIRST audit failure" in summary_a and "LAST audit summary" in summary_a
    summary_b = whole[0].split("REVIEW_VERDICTS_SUMMARY:\n", 1)[1]
    # Newline-agnostic: a Windows checkout may hand `cat` the file with CRLF endings.
    assert f"TEST GATE (exit 1):\n{output}" in summary_b.replace("\r\n", "\n")
    assert _MARKER_RE.search(summary_b) is None

    cuts = await _cuts(db, task.id)
    assert [(c["block"], c["field"], c["agent"]) for c in cuts] == [
        ("gate_output", "compaction_max_chars", "submitter-a")
    ]


# --- configuration surface: REST + MCP ----------------------------------------------


async def _mcp(daemon: Daemon, name: str, arguments: dict[str, Any]) -> tuple[bool, str]:
    response = await handle_mcp_message(
        daemon,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response is not None
    result = response["result"]
    return bool(result["isError"]), str(result["content"][0]["text"])


async def test_context_settings_round_trip_through_rest_and_mcp(
    db: Database, tmp_path: Path
) -> None:
    await _init_repo(tmp_path / "repo")
    daemon = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    app = create_app(daemon, manage_lifecycle=False)
    defaults = {
        "compaction_enabled": False, "compaction_max_chars": 60_000, "bulk_max_chars": 200_000
    }
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            schema = (await client.get("/api/config/schema")).json()
            assert schema["properties"]["context"] == {"$ref": "#/$defs/ContextSettings"}
            assert (await client.get("/api/config/global")).json()["context"] == defaults

            created = await client.post(
                "/api/projects",
                json={"name": "ctx", "path": str(tmp_path / "repo"), "default_branch": "main"},
            )
            assert created.status_code == 200, created.text
            pid = created.json()["id"]

            bad = await client.patch(
                f"/api/projects/{pid}/config", json={"config": {"context": {"bulk_max_chars": 999}}}
            )
            assert bad.status_code == 422
            assert (await client.get(f"/api/projects/{pid}/config")).json()["context"] == defaults

        is_error, _ = await _mcp(
            daemon, "conclave_patch_config",
            {"project_id": pid, "config": {"context": {"compaction_enabled": True}}},
        )
        assert is_error is False
        is_error, _ = await _mcp(
            daemon, "conclave_patch_config",
            {"project_id": pid, "context": {"compaction_max_chars": 30_000}},
        )
        assert is_error is False
        expected = {**defaults, "compaction_enabled": True, "compaction_max_chars": 30_000}
        is_error, text = await _mcp(daemon, "conclave_get_config", {"project_id": pid})
        assert is_error is False and json.loads(text)["context"] == expected

        is_error, text = await _mcp(
            daemon, "conclave_patch_config",
            {"project_id": pid, "config": {"context": {"bulk_max_chars": 2_000_001}}},
        )
        assert is_error is True and "bulk_max_chars" in text
        _, text = await _mcp(daemon, "conclave_get_config", {"project_id": pid})
        assert json.loads(text)["context"] == expected
    finally:
        await daemon.shutdown()
