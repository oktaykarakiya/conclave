"""Unit tests for the ported engine logic: verdicts, grounding, pipeline, memory,
budget checks, and diff truncation."""

from __future__ import annotations

from pathlib import Path

from conclave.config import AgentsPolicy
from conclave.engine import (
    AttemptMemory,
    build_baseline_preamble,
    check_grounding,
    get_agent_pipeline,
    parse_verdict,
    trim_output,
)
from conclave.engine.orchestrator import _MAX_DIFF_CHARS, _check_budget, _truncate_diff

# --- parse_verdict ----------------------------------------------------------


def test_parse_json_pass() -> None:
    v = parse_verdict('Some preamble\n```json\n{"verdict": "pass", "reason": "ok"}\n```')
    assert v.verdict == "pass"
    assert v.source == "json"
    assert v.reason == "ok"


def test_parse_json_fail_with_evidence() -> None:
    text = (
        '```json\n{"verdict": "fail", "reason": "bug", '
        '"evidence": [{"file": "a.js", "line": 3}]}\n```'
    )
    v = parse_verdict(text)
    assert v.verdict == "fail"
    assert v.evidence == [{"file": "a.js", "line": 3}]


def test_parse_legacy_strings() -> None:
    assert parse_verdict("VERDICT: PASS").verdict == "pass"
    assert parse_verdict("VERDICT: FAIL because x").verdict == "fail"
    assert parse_verdict("VERDICT: BLOCK").verdict == "block"
    assert parse_verdict("VERDICT: DECLINE edge case").verdict == "decline"


def test_parse_unknown_when_absent_or_invalid() -> None:
    assert parse_verdict("no verdict here").verdict == "unknown"
    assert parse_verdict('```json\n{"verdict": "weird"}\n```').verdict == "unknown"


# --- check_grounding --------------------------------------------------------


def test_grounding_keeps_real_evidence(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    diff = "diff --git a/src/a.py b/src/a.py\n@@\n+x = 1"
    v = parse_verdict(
        '```json\n{"verdict": "fail", "reason": "bad", '
        '"evidence": [{"file": "src/a.py", "line": 1}]}\n```'
    )
    out, warnings = check_grounding(v, diff, tmp_path)
    assert out.verdict == "fail"
    assert warnings == []


def test_grounding_downgrades_when_not_in_diff(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x")
    v = parse_verdict('```json\n{"verdict": "fail", "evidence": [{"file": "a.py"}]}\n```')
    out, warnings = check_grounding(v, "diff --git a/other.py b/other.py", tmp_path)
    assert out.verdict == "unknown"
    assert "DOWNGRADED" in out.reason
    assert any("not in this task's diff" in w for w in warnings)


def test_grounding_downgrades_when_not_on_disk(tmp_path: Path) -> None:
    diff = "diff --git a/ghost.py b/ghost.py\n+x"
    v = parse_verdict('```json\n{"verdict": "decline", "evidence": [{"file": "ghost.py"}]}\n```')
    out, _ = check_grounding(v, diff, tmp_path)
    assert out.verdict == "unknown"


def test_grounding_skips_pass_and_string_source(tmp_path: Path) -> None:
    assert check_grounding(parse_verdict("VERDICT: PASS"), "", tmp_path)[0].verdict == "pass"
    # string-sourced fail cannot be grounded, so it is preserved
    assert check_grounding(parse_verdict("VERDICT: FAIL"), "", tmp_path)[0].verdict == "fail"


# --- pipeline ---------------------------------------------------------------


def test_pipeline_mandatory_only_for_trivial_diff() -> None:
    diff = "diff --git a/README.md b/README.md\n@@\n+hello"
    assert get_agent_pipeline(diff, AgentsPolicy()) == ["tester", "security", "reviewer"]


def test_pipeline_db_migration_adds_specialists() -> None:
    diff = (
        "diff --git a/db/migrations/005_x.sql b/db/migrations/005_x.sql\n"
        "new file mode 100644\n+CREATE TABLE foo (id int);"
    )
    pipeline = set(get_agent_pipeline(diff, AgentsPolicy()))
    assert {"architect", "risk", "performance", "devops"} <= pipeline
    assert "legal" not in pipeline


def test_pipeline_auth_change_adds_legal_and_risk() -> None:
    diff = "diff --git a/auth/login.js b/auth/login.js\n@@\n+const token = jwt.sign(user)"
    pipeline = set(get_agent_pipeline(diff, AgentsPolicy()))
    assert "legal" in pipeline
    assert "risk" in pipeline


# --- memory -----------------------------------------------------------------


def test_attempt_memory_trims_and_renders() -> None:
    mem = AttemptMemory(max_entries=2)
    mem.add(1, "diff-1", "rejected because A")
    mem.add(2, "diff-2", "rejected because B")
    mem.add(3, "diff-3", "rejected because C")
    preamble = mem.build_preamble()
    assert "rejected because A" not in preamble  # trimmed to last 2
    assert "rejected because B" in preamble
    assert "rejected because C" in preamble
    assert "PRIOR ATTEMPT HISTORY" in preamble


def test_attempt_memory_empty() -> None:
    assert AttemptMemory().build_preamble() == ""


# --- baseline ---------------------------------------------------------------


def test_baseline_preamble_and_trim() -> None:
    assert build_baseline_preamble("main", "") == ""
    preamble = build_baseline_preamble("vibes", "FAIL suite-x")
    assert "PRE-EXISTING TEST FAILURES on `vibes`" in preamble
    assert "FAIL suite-x" in preamble

    trimmed = trim_output("\n".join(str(i) for i in range(500)), max_lines=10)
    assert trimmed.splitlines() == [str(i) for i in range(490, 500)]


# --- _test_command ----------------------------------------------------------


def test_test_command_returns_baseline_when_set() -> None:
    from conclave.config import ConclaveConfig
    from conclave.engine.orchestrator import _test_command

    config = ConclaveConfig()
    config.execution.baseline_test_command = "cargo test"
    assert _test_command(config, None) == "cargo test"


def test_test_command_falls_back_to_knowledge_commands_test() -> None:
    from conclave.config import ConclaveConfig
    from conclave.engine.orchestrator import _test_command

    config = ConclaveConfig()
    knowledge = {"commands": {"test": "pytest -q"}}
    assert _test_command(config, knowledge) == "pytest -q"


def test_test_command_returns_none_when_neither() -> None:
    from conclave.config import ConclaveConfig
    from conclave.engine.orchestrator import _test_command

    config = ConclaveConfig()
    assert _test_command(config, None) is None
    assert _test_command(config, {}) is None
    assert _test_command(config, {"commands": {}}) is None


# --- _build_venv_guidance ---------------------------------------------------


def test_build_venv_guidance_returns_empty_when_no_venv(tmp_path: Path) -> None:
    from conclave.config import ConclaveConfig
    from conclave.engine.orchestrator import _build_venv_guidance

    config = ConclaveConfig()
    config.execution.baseline_test_command = "pytest"
    # tmp_path has no .venv/ directory.
    assert _build_venv_guidance(tmp_path, config, None) == ""


def test_build_venv_guidance_derives_pythonic_commands(tmp_path: Path) -> None:
    from conclave.config import ConclaveConfig
    from conclave.engine.orchestrator import _build_venv_guidance

    (tmp_path / ".venv").mkdir()
    config = ConclaveConfig()
    config.execution.baseline_test_command = "pytest -q"
    knowledge = {"commands": {"lint": "ruff check src tests", "check": "mypy"}}

    result = _build_venv_guidance(tmp_path, config, knowledge)

    assert ".venv/bin/pytest -q" in result
    assert ".venv/bin/ruff check src tests" in result
    assert ".venv/bin/mypy" in result
    assert "Do NOT use system-wide" in result
    # Must NOT mention hard-coded tools — it derives from configured commands.
    assert "pytest" in result.lower()
    # The tool_refs line mentions the unique tool names.
    assert "`pytest`" in result and "`ruff`" in result and "`mypy`" in result


def test_build_venv_guidance_derives_non_python_commands(tmp_path: Path) -> None:
    from conclave.config import ConclaveConfig
    from conclave.engine.orchestrator import _build_venv_guidance

    (tmp_path / ".venv").mkdir()
    config = ConclaveConfig()
    config.execution.baseline_test_command = "cargo test"

    result = _build_venv_guidance(tmp_path, config, None)

    # Must mention the actual test command, never hard-coded pytest.
    assert "cargo test" in result
    assert "pytest" not in result
    assert "mypy" not in result
    assert ".venv/bin/cargo test" in result


def test_build_venv_guidance_handles_empty_config(tmp_path: Path) -> None:
    from conclave.config import ConclaveConfig
    from conclave.engine.orchestrator import _build_venv_guidance

    (tmp_path / ".venv").mkdir()
    config = ConclaveConfig()
    # No baseline_test_command, no knowledge.

    result = _build_venv_guidance(tmp_path, config, None)

    # When no commands are known, emit a generic .venv/bin/ hint.
    assert result != ""
    assert ".venv/bin/" in result
    assert "MANDATORY" in result
    # Must NOT contain hard-coded pytest/mypy/ruff.
    assert "pytest" not in result
    assert "mypy" not in result
    assert "ruff" not in result


# --- _check_budget -----------------------------------------------------------


def test_check_budget_returns_true_when_exceeded() -> None:
    """A budget of 0.001 minutes started 0.1 seconds ago is already exceeded."""
    import time
    started = time.monotonic() - 0.1  # started 100ms ago
    assert _check_budget(started, 0.001) is True


def test_check_budget_returns_false_when_not_exceeded() -> None:
    """A budget of 60 minutes started just now is not exceeded."""
    import time
    started = time.monotonic()
    assert _check_budget(started, 60.0) is False


def test_check_budget_returns_false_when_budget_is_zero() -> None:
    """A budget of 0 means 'no cap' — never exceeded."""
    import time
    started = time.monotonic() - 3600  # started an hour ago
    assert _check_budget(started, 0.0) is False


# --- _truncate_diff ----------------------------------------------------------


def test_truncate_diff_truncates_oversized_diff_and_includes_marker() -> None:
    """A diff larger than _MAX_DIFF_CHARS is truncated with a clear marker."""
    big = "x" * (_MAX_DIFF_CHARS + 100)
    result = _truncate_diff(big)
    assert len(result) < len(big)
    assert "[diff truncated — original was" in result
    assert str(len(big)) in result
    # The truncated content should be the first _MAX_DIFF_CHARS chars plus the marker.
    assert result.startswith("x" * _MAX_DIFF_CHARS)


def test_truncate_diff_leaves_small_diff_unchanged() -> None:
    """A diff under the cap is returned verbatim."""
    small = "diff --git a/x b/x\n+hello"
    result = _truncate_diff(small)
    assert result == small


def test_truncate_diff_at_exact_boundary_is_unchanged() -> None:
    """A diff exactly at _MAX_DIFF_CHARS is not truncated."""
    exact = "y" * _MAX_DIFF_CHARS
    result = _truncate_diff(exact)
    assert result == exact
    assert "[diff truncated" not in result


# --- check_grounding path traversal ------------------------------------------


def test_grounding_rejects_path_traversal_with_dot_dot(tmp_path: Path) -> None:
    """Evidence paths containing '..' escape the worktree and must be rejected.

    Uses a subdirectory as the worktree so that ``..`` truly escapes to a parent
    directory outside the sandbox.
    """
    workdir = tmp_path / "worktree"
    workdir.mkdir()
    (workdir / "src").mkdir()
    (workdir / "src" / "a.py").write_text("x = 1\n")
    # File OUTSIDE the worktree (in tmp_path, parent of workdir).
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    # Include the traversal path in the diff and also create a valid in-diff file
    # so the evidence doesn't fail solely on the "not in diff" check.
    diff = (
        "diff --git a/src/a.py b/src/a.py\n@@ -0,0 +1 @@\n+x = 1\n"
        "diff --git a/src/../../outside.txt b/src/../../outside.txt\n@@ -0,0 +1 @@\n+secret"
    )
    v = parse_verdict(
        '```json\n{"verdict": "fail", "reason": "bad", '
        '"evidence": [{"file": "src/../../outside.txt", "line": 1}]}\n```'
    )
    out, warnings = check_grounding(v, diff, workdir)
    assert out.verdict == "unknown"
    assert "DOWNGRADED" in out.reason
    assert any("outside the worktree" in w for w in warnings)


def test_grounding_rejects_absolute_path(tmp_path: Path) -> None:
    """Evidence paths that are absolute escape the worktree and must be rejected."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    # The diff must contain the absolute path so the evidence passes the
    # "not in diff" check and reaches the path-traversal guard.
    diff = (
        "diff --git a/src/a.py b/src/a.py\n@@ -0,0 +1 @@\n+x = 1\n"
        "diff --git a//etc/passwd b//etc/passwd\n@@ -0,0 +1 @@\n+secret"
    )
    v = parse_verdict(
        '```json\n{"verdict": "fail", "reason": "bad", '
        '"evidence": [{"file": "/etc/passwd", "line": 1}]}\n```'
    )
    out, warnings = check_grounding(v, diff, tmp_path)
    assert out.verdict == "unknown"
    assert "DOWNGRADED" in out.reason
    assert any("outside the worktree" in w for w in warnings)


def test_grounding_rejects_path_traversal_with_symlink(tmp_path: Path) -> None:
    """A symlink pointing outside the worktree must not bypass the traversal guard."""
    workdir = tmp_path / "worktree"
    workdir.mkdir()
    (workdir / "src").mkdir()
    (workdir / "src" / "a.py").write_text("x = 1\n")
    # File OUTSIDE the worktree.
    outside = tmp_path / "secret.txt"
    outside.write_text("secret\n")
    # Create a symlink that resolves outside the worktree.
    (workdir / "src" / "escape").symlink_to(outside)
    diff = (
        "diff --git a/src/a.py b/src/a.py\n@@ -0,0 +1 @@\n+x = 1\n"
        "diff --git a/src/escape b/src/escape\n@@ -0,0 +1 @@\n+secret"
    )
    v = parse_verdict(
        '```json\n{"verdict": "fail", "reason": "bad", '
        '"evidence": [{"file": "src/escape", "line": 1}]}\n```'
    )
    out, warnings = check_grounding(v, diff, workdir)
    assert out.verdict == "unknown"
    assert "DOWNGRADED" in out.reason
    assert any("outside the worktree" in w for w in warnings)
