"""Prompt context end to end: irreplaceable content reaches agents whole, recoverable bulk is
fitted per dispatching agent with a visible marker and exactly one Live event per cut.

Every test runs the real orchestrator against a throwaway git repo and the deterministic
FakeProvider (zero LLM cost), asserting on the full prompts the provider received.
"""

from __future__ import annotations

import asyncio
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
    build_default_workflow,
    load_project_config,
)
from conclave.db import Database, EventRow, Task, TaskState
from conclave.db import repositories as repo
from conclave.engine import Orchestrator, run_git
from conclave.engine.compactor import compact_task_context
from conclave.engine.runner import AgentRunner
from conclave.events import EventBus
from conclave.mcp.server import handle_mcp_message
from conclave.providers import AgentResult, OnChunk, ResolvedProfile
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
        (path / name).parent.mkdir(parents=True, exist_ok=True)
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
    """Attach ``tmp_path/repo`` with *config*, process one approved task, return it + result.

    The repos here have no test suite, so a task without a ``baseline_test_command`` in *config*
    runs ungated deliberately (``allow_no_test_command``) instead of failing up front.
    """
    execution = {"target_branch": "main", "allow_no_test_command": True}
    execution.update(config.get("execution", {}))
    project = await repo.create_project(
        db, name="t", path=str(tmp_path / "repo"), default_branch="main",
        config={**config, "execution": execution},
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
            "execution": {
                "target_branch": "main", "parallel_reviewers": parallel, "auto_merge": True
            },
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
            "execution": {"auto_merge": True},
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


_SECURITY_FAILS = AgentResult(
    ok=True,
    text='```json\n{"verdict": "fail", "reason": "rejected again", "evidence": []}\n```',
    model_reported="fake",
)
_SECURITY_PASSES = AgentResult(
    ok=True,
    text='```json\n{"verdict": "pass", "reason": "fine now", "evidence": []}\n```',
    model_reported="fake",
)


def _history_entry(prompt: str, attempt: int) -> str:
    """The PRIOR ATTEMPT HISTORY entry of *attempt* in a developer *prompt*."""
    history = prompt.split("PRIOR ATTEMPT HISTORY", 1)[1]
    entry = history.split(f"--- Attempt {attempt} (REJECTED) ---\n", 1)[1]
    return entry.split("\n\n--- Attempt ", 1)[0]


@pytest.mark.parametrize("bulk_max_chars", [None, 1_000], ids=["default", "small-limit"])
async def test_prior_attempt_memory_keeps_the_previous_diff_whole(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bulk_max_chars: int | None
) -> None:
    """The rejected attempts' diffs reach the retry's history whole, or fitted like every other
    bulk block (with the standard marker and one context_truncated event per previous diff)."""
    monkeypatch.setattr("conclave.engine.orchestrator._REVIEWER_RETRY_BACKOFF_S", 0.0)
    await _init_repo(tmp_path / "repo")
    content = _neutral_text(5_000)
    provider = FakeProvider(
        developer_content=content,
        replies={"reviewer:security": [_SECURITY_FAILS, _SECURITY_FAILS, _SECURITY_PASSES]},
    )
    config: dict[str, Any] = {"agent_overrides": {"developer": {"max_retries": 3}}}
    if bulk_max_chars is not None:
        config["context"] = {"bulk_max_chars": bulk_max_chars}

    task, ok = await _run_task(db, tmp_path, provider, config)

    assert ok is True and task.state is TaskState.done
    developer = _prompts_of(provider, "developer")
    assert len(developer) == 3
    entry = _history_entry(developer[2], 1)
    assert entry.startswith("Why it was rejected:\n")
    assert "(truncated)" not in entry
    added = [f"+{line}" for line in content.splitlines()]
    cuts = [c for c in await _cuts(db, task.id) if c["block"] == "previous_diff"]
    if bulk_max_chars is None:
        assert all(line in entry.splitlines() for line in added)
        assert _MARKER_RE.search(entry) is None
        assert cuts == []
    else:
        match = _MARKER_RE.search(entry)
        assert match is not None
        assert match.group(3, 4) == ("bulk_max_chars", "1000")
        assert match.group(5).startswith("Run `git diff ")
        assert added[0] in entry and added[-1] in entry  # head and tail are kept
        # One per previous diff (attempts 1 and 2), fitted once for the retry that shows it.
        assert len(cuts) == 2
        assert {(c["agent"], c["node_id"]) for c in cuts} == {("developer", "developer")}


class _WritesPerAttempt(FakeProvider):
    """FakeProvider whose developer writes ``contents[n]`` on its n-th dispatch, so every attempt
    leaves a diff of its own."""

    def __init__(self, contents: list[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.contents = contents
        self.developer_dispatches = 0

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
        if self.role_of(prompt) == "developer":
            self.developer_content = self.contents[self.developer_dispatches]
            self.developer_dispatches += 1
        return await super().run_agent(
            profile=profile, prompt=prompt, timeout_seconds=timeout_seconds, cwd=cwd,
            on_chunk=on_chunk, cancel_event=cancel_event,
        )


async def test_every_rejected_diff_reaches_the_retry_exactly_once(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The latest rejected diff is shown as YOUR IMMEDIATELY PRIOR DIFF, so its history entry
    points there instead of repeating it; earlier attempts keep their own diffs."""
    monkeypatch.setattr("conclave.engine.orchestrator._REVIEWER_RETRY_BACKOFF_S", 0.0)
    await _init_repo(tmp_path / "repo")
    contents = [
        "".join(f"attempt-{n} writes line {i:03d}\n" for i in range(40)) for n in (1, 2, 3)
    ]
    provider = _WritesPerAttempt(
        contents,
        replies={"reviewer:security": [_SECURITY_FAILS, _SECURITY_FAILS, _SECURITY_PASSES]},
    )

    task, ok = await _run_task(
        db, tmp_path, provider, {"agent_overrides": {"developer": {"max_retries": 3}}}
    )

    assert ok is True and task.state is TaskState.done
    third = _prompts_of(provider, "developer")[2]
    first_entry = _history_entry(third, 1)
    diff_1 = first_entry.split("Diff produced:\n", 1)[1].rstrip("\n")
    diff_2 = third.split("YOUR IMMEDIATELY PRIOR DIFF:\n```diff\n", 1)[1].split("\n```", 1)[0]
    assert "+attempt-1 writes line 000" in diff_1 and "+attempt-1 writes line 039" in diff_1
    assert "+attempt-2 writes line 000" in diff_2 and "+attempt-2 writes line 039" in diff_2
    assert third.count(diff_1) == 1
    assert third.count(diff_2) == 1
    assert third.count("+attempt-2 writes line 000") == 1
    second_entry = _history_entry(third, 2).split("\n\nLATEST REVIEWER FEEDBACK", 1)[0]
    assert second_entry.startswith("Why it was rejected:\n")
    assert second_entry.rstrip("\n").endswith(
        "\nDiff produced: shown below as YOUR IMMEDIATELY PRIOR DIFF"
    )


class _RebuildsTheBundle(FakeProvider):
    """FakeProvider whose developer also rewrites committed build output, as a rebuild does."""

    def __init__(self, bundle: str, content: str) -> None:
        super().__init__()
        self.bundle = bundle
        self.bundle_content = content

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
        if cwd is not None and self.role_of(prompt) == "developer":
            (Path(cwd) / self.bundle).write_text(self.bundle_content, encoding="utf-8")
        return await super().run_agent(
            profile=profile, prompt=prompt, timeout_seconds=timeout_seconds, cwd=cwd,
            on_chunk=on_chunk, cancel_event=cancel_event,
        )


async def test_output_marked_diff_unset_reaches_reviewers_as_one_binary_line(
    db: Database, tmp_path: Path
) -> None:
    """Build output a repository marks ``-diff`` (as Conclave marks its own SPA bundle) is one
    ``Binary files ... differ`` line in every reviewer's diff, so the real change keeps the room."""
    bundle = "dist/app.min.js"
    await _init_repo(
        tmp_path / "repo",
        {
            ".gitattributes": "dist/** -diff linguist-generated=true\n",
            bundle: "!function(){var a=1}();\n",
        },
    )
    marker = "REBUILT_BUNDLE_MARKER_4f1c"
    provider = _RebuildsTheBundle(bundle, f"!function(){{var {marker}=2}}();\n")

    task, ok = await _run_task(db, tmp_path, provider, {})

    assert ok is True and task.state is TaskState.done
    reviewers = [p for p in provider.prompts if _REVIEW in p]
    assert len(reviewers) >= 3
    for prompt in reviewers:
        assert f"Binary files a/{bundle} and b/{bundle} differ" in prompt
        assert "+++ b/FEATURE.txt\n@@ -0,0 +1 @@\n+done\n" in prompt
        assert marker not in prompt
    assert [c for c in await _cuts(db, task.id) if c["block"] == "diff"] == []


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


_BASELINE_COMMAND = "if [ ! -f FEATURE.txt ]; then cat baseline_output.txt; exit 1; fi"
_BASELINE_HINT = (
    f"Re-run `{_BASELINE_COMMAND}` in your working directory before changing anything for the "
    "full output."
)


async def test_baseline_output_is_cached_bounded_and_fitted_with_a_marker_for_each_reader(
    db: Database, tmp_path: Path
) -> None:
    output = _numbered_output("FIRST baseline failure: test_legacy", "LAST baseline: 401 failed")
    base = await _init_repo(tmp_path / "repo", {"baseline_output.txt": output})
    provider = FakeProvider()

    task, ok = await _run_task(
        db, tmp_path, provider,
        {
            "execution": {"target_branch": "main", "baseline_test_command": _BASELINE_COMMAND},
            "context": {"bulk_max_chars": 1_000},
        },
        use_planner=True,
    )

    assert ok is True and task.state is TaskState.done
    cached = await repo.get_baseline(db, task.project_id, base)
    assert cached is not None
    # The cache row is bounded, marker included, and keeps the head and tail.
    assert len(cached.output) <= 1_000
    assert cached.output.startswith("FIRST baseline failure: test_legacy\n")
    assert cached.output.endswith("LAST baseline: 401 failed\n")
    assert len(_MARKER_RE.findall(cached.output)) == 1
    stored = _MARKER_RE.search(cached.output)
    assert stored is not None
    omitted, total, field, limit, hint = stored.groups()
    assert (int(total), field, limit, hint) == (
        len(output), "bulk_max_chars", "1000", _BASELINE_HINT
    )
    head, tail = cached.output[: stored.start()], cached.output[stored.end() + 1 :]
    assert output.startswith(head) and output.endswith(tail)
    assert int(omitted) == len(output) - len(head) - len(tail)
    planner = [p for p in provider.prompts if _PLAN in p]
    developer = _prompts_of(provider, "developer")
    assert len(planner) == 1 and len(developer) == 1
    for prompt in (planner[0], developer[0]):
        preamble = prompt.split("PRE-EXISTING TEST FAILURES on `main`", 1)[1]
        assert "FIRST baseline failure: test_legacy" in preamble
        assert "LAST baseline: 401 failed" in preamble
        match = _MARKER_RE.search(preamble)
        assert match is not None
        assert match.group(3, 4, 5) == ("bulk_max_chars", "1000", _BASELINE_HINT)

    cuts = await _cuts(db, task.id)
    assert [(c["block"], c["agent"]) for c in cuts] == [
        ("baseline_output", None),
        ("gate_output", "planner"),
    ]
    assert cuts[0]["field"] == "bulk_max_chars"
    assert cuts[0]["original_chars"] == len(output)
    # This task's readers fitted the whole output, not the stored copy.
    assert cuts[1]["original_chars"] == len(output)


async def test_baseline_output_under_the_limit_is_cached_unchanged(
    db: Database, tmp_path: Path
) -> None:
    output = _numbered_output("FIRST baseline failure: test_legacy", "LAST baseline: 9 failed", 8)
    assert len(output) < 1_000
    base = await _init_repo(tmp_path / "repo", {"baseline_output.txt": output})

    task, ok = await _run_task(
        db, tmp_path, FakeProvider(),
        {
            "execution": {"target_branch": "main", "baseline_test_command": _BASELINE_COMMAND},
            "context": {"bulk_max_chars": 1_000},
        },
    )

    assert ok is True
    cached = await repo.get_baseline(db, task.project_id, base)
    assert cached is not None
    assert cached.output == output
    assert await _cuts(db, task.id) == []


async def test_a_cached_bounded_baseline_reaches_the_next_task_as_stored(
    db: Database, tmp_path: Path
) -> None:
    output = _numbered_output("FIRST baseline failure: test_legacy", "LAST baseline: 401 failed")
    base = await _init_repo(tmp_path / "repo", {"baseline_output.txt": output})
    provider = FakeProvider()
    # Nothing merges, so the second task starts from the same checkpoint and hits the cache.
    first, ok = await _run_task(
        db, tmp_path, provider,
        {
            "execution": {
                "target_branch": "main",
                "baseline_test_command": _BASELINE_COMMAND,
                "auto_merge": False,
            },
            "context": {"bulk_max_chars": 1_000},
        },
    )
    assert ok is True
    cached = await repo.get_baseline(db, first.project_id, base)
    assert cached is not None and len(cached.output) <= 1_000

    second = await repo.create_task(
        db, project_id=first.project_id, request="add another feature file",
        state=TaskState.approved,
    )
    claimed = await repo.claim_next_approved(db, first.project_id)
    assert claimed is not None and claimed.id == second.id
    provider.prompts.clear()
    orchestrator = Orchestrator(db, EventBus(db), provider, tmp_path / "home")
    assert await orchestrator.process_task(claimed) is True

    events = await repo.list_events(db, project_id=first.project_id, limit=10_000)
    assert [e.type for e in events].count("baseline.snapshot") == 1  # the first task's only
    developer = _prompts_of(provider, "developer")
    assert len(developer) == 1
    preamble = developer[0].split("PRE-EXISTING TEST FAILURES on `main`", 1)[1]
    assert f"```\n{cached.output}\n```" in preamble
    assert len(_MARKER_RE.findall(preamble)) == 1
    assert await _cuts(db, second.id) == []


_NO_VERDICT = AgentResult(
    ok=True, text="I read the diff but forgot the verdict block.", model_reported="fake"
)


def _preamble_of(prompt: str) -> str:
    """The pre-existing-failures section of a reviewer *prompt* (up to its diff)."""
    section = prompt.split("PRE-EXISTING TEST FAILURES on `main`", 1)[1]
    return section.split("\n\nDIFF:\n", 1)[0]


@pytest.mark.parametrize("parallel", [False, True], ids=["sequential", "parallel"])
async def test_every_reviewer_prompt_carries_the_pre_existing_failures(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parallel: bool
) -> None:
    """A reviewer that runs the suite and sees a pre-existing failure is told it is not this
    task's: every reviewer prompt, re-runs included, carries the developer's preamble, fitted
    to the reviewer's limit and cut (and reported) once for every reader at that limit."""
    monkeypatch.setattr("conclave.engine.orchestrator._REVIEWER_RETRY_BACKOFF_S", 0.0)
    output = _numbered_output("FIRST baseline failure: test_legacy", "LAST baseline: 401 failed")
    await _init_repo(tmp_path / "repo", {"baseline_output.txt": output})
    # The mandatory security reviewer first replies without a verdict, so it is re-run.
    provider = FakeProvider(replies={"reviewer:security": [_NO_VERDICT, _SECURITY_PASSES]})

    task, ok = await _run_task(
        db, tmp_path, provider,
        {
            "execution": {
                "target_branch": "main",
                "baseline_test_command": _BASELINE_COMMAND,
                "parallel_reviewers": parallel,
            },
            "context": {"bulk_max_chars": 1_000},
        },
    )

    assert ok is True and task.state is TaskState.done
    pipeline = await _pipeline(db, task.id)
    assert "security" in pipeline and len(pipeline) >= 3
    reviewers = [p for p in provider.prompts if _REVIEW in p]
    assert len(reviewers) == len(pipeline) + 1  # the security re-run
    assert len(_prompts_of(provider, "security")) == 2
    for prompt in reviewers:
        preamble = _preamble_of(prompt)
        assert "FIRST baseline failure: test_legacy" in preamble
        assert "LAST baseline: 401 failed" in preamble
        match = _MARKER_RE.search(preamble)
        assert match is not None
        assert match.group(3, 4, 5) == ("bulk_max_chars", "1000", _BASELINE_HINT)
        # The request comes first, then the preamble, as the planner and developer get it.
        assert prompt.index(task.request) < prompt.index("PRE-EXISTING TEST FAILURES")

    # The planner (it plans every task by default) is the first reader at bulk_max_chars: the
    # developer and every reviewer reuse its cut, so it is made and reported once.
    cuts = await _cuts(db, task.id)
    assert [(c["block"], c["agent"]) for c in cuts] == [
        ("baseline_output", None),
        ("gate_output", "planner"),
    ]


async def test_a_reviewer_node_fits_the_baseline_to_its_own_limit(
    db: Database, tmp_path: Path
) -> None:
    output = _numbered_output("FIRST baseline failure: test_legacy", "LAST baseline: 401 failed")
    await _init_repo(tmp_path / "repo", {"baseline_output.txt": output})
    workflow = build_default_workflow()
    for node in workflow.nodes:
        if node.id == "security":
            node.compact_before_start = True
    provider = FakeProvider()

    task, ok = await _run_task(
        db, tmp_path, provider,
        {
            "execution": {"target_branch": "main", "baseline_test_command": _BASELINE_COMMAND},
            "context": {"compaction_max_chars": 1_000},
            "workflow": workflow.model_dump(mode="json"),
        },
    )

    assert ok is True and task.state is TaskState.done
    security = _prompts_of(provider, "security")
    assert len(security) == 1
    match = _MARKER_RE.search(_preamble_of(security[0]))
    assert match is not None
    assert match.group(3, 4, 5) == ("compaction_max_chars", "1000", _BASELINE_HINT)
    others = [p for p in provider.prompts if _REVIEW in p and p not in security]
    assert len(others) >= 2
    for prompt in others:
        preamble = _preamble_of(prompt).replace("\r\n", "\n")
        assert f"```\n{output}" in preamble
        assert _MARKER_RE.search(preamble) is None

    cuts = await _cuts(db, task.id)
    assert [(c["block"], c["field"], c["agent"], c["node_id"]) for c in cuts] == [
        ("gate_output", "compaction_max_chars", "security", "security")
    ]


@pytest.mark.parametrize(
    "execution",
    [{"baseline_test_command": "true"}, {"allow_no_test_command": True}],
    ids=["green", "absent"],
)
async def test_no_reviewer_prompt_carries_a_preamble_without_baseline_failures(
    db: Database, tmp_path: Path, execution: dict[str, Any]
) -> None:
    await _init_repo(tmp_path / "repo")
    provider = FakeProvider()

    task, ok = await _run_task(
        db, tmp_path, provider, {"execution": {"target_branch": "main", **execution}}
    )

    assert ok is True and task.state is TaskState.done
    reviewers = [p for p in provider.prompts if _REVIEW in p]
    assert len(reviewers) >= 3
    assert all("PRE-EXISTING TEST FAILURES" not in p for p in provider.prompts)


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
