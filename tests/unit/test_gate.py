"""Unit tests for gate outcome classification (ENG-7) and quarantine exclusion injection.

Covers all four ``GateOutcome`` values plus the skipped / no-command path,
and the pure ``inject_quarantine_exclusions`` injector for pytest, jest,
unknown frameworks, empty patterns, multi-pattern scenarios, and shell escaping.
"""

from __future__ import annotations

from pathlib import Path

from conclave.engine.gate import GateResult, inject_quarantine_exclusions, run_tests


async def test_run_tests_passed(tmp_path: Path) -> None:
    """Exit 0 yields outcome='passed' and passed=True."""
    result = await run_tests(tmp_path, "true")
    assert result.passed is True
    assert result.exit_code == 0
    assert result.outcome == "passed"
    assert result.skipped is False


async def test_run_tests_failed(tmp_path: Path) -> None:
    """Exit 1 yields outcome='failed' and passed=False."""
    result = await run_tests(tmp_path, "exit 1")
    assert result.passed is False
    assert result.exit_code == 1
    assert result.outcome == "failed"
    assert result.skipped is False


async def test_run_tests_timed_out(tmp_path: Path) -> None:
    """Simulate 124 via a short timeout that triggers TimeoutError in run_shell."""
    result = await run_tests(tmp_path, "sleep 10", timeout_seconds=1)
    assert result.passed is False
    assert result.exit_code == 124
    assert result.outcome == "timed_out"
    assert result.skipped is False
    assert "timed out" in result.output.lower()


async def test_run_tests_missing_command(tmp_path: Path) -> None:
    """Run a nonexistent command → exit 127 → outcome='missing_command'."""
    result = await run_tests(tmp_path, "nonexistent_command_xyz_123")
    assert result.passed is False
    assert result.exit_code == 127
    assert result.outcome == "missing_command"
    assert result.skipped is False


async def test_run_tests_skipped(tmp_path: Path) -> None:
    """command=None yields skipped=True, outcome='passed'."""
    result = await run_tests(tmp_path, None)
    assert result.passed is True
    assert result.exit_code == 0
    assert result.outcome == "passed"
    assert result.skipped is True
    assert "no test command" in result.output.lower()


async def test_run_tests_nonzero_but_not_infra(tmp_path: Path) -> None:
    """A non-zero exit that is neither 124 nor 127 (e.g. exit 2) is classified as 'failed'."""
    result = await run_tests(tmp_path, "exit 2")
    assert result.passed is False
    assert result.exit_code == 2
    assert result.outcome == "failed"


# ---------------------------------------------------------------------------
# GateResult construction is a frozen dataclass — verify defaults and
# explicit fields so callers get what they expect.
# ---------------------------------------------------------------------------


def test_gate_result_defaults() -> None:
    """GateResult defaults: outcome='passed', skipped=False."""
    gr = GateResult(passed=True, exit_code=0, output="ok")
    assert gr.outcome == "passed"
    assert gr.skipped is False


def test_gate_result_explicit_outcome() -> None:
    """All fields can be set explicitly."""
    gr = GateResult(
        passed=False, exit_code=127, output="not found",
        outcome="missing_command", skipped=False,
    )
    assert gr.outcome == "missing_command"
    assert gr.passed is False
    assert gr.exit_code == 127


# ---------------------------------------------------------------------------
# inject_quarantine_exclusions — pure unit tests (no I/O)
# ---------------------------------------------------------------------------


def test_inject_pytest_gets_deselect_per_pattern() -> None:
    """pytest command gets a --deselect flag for each quarantine pattern."""
    result = inject_quarantine_exclusions(
        "pytest", ["tests/a.py", "tests/b.py::test_x"]
    )
    assert "--deselect " in result
    assert "tests/a.py" in result
    assert "tests/b.py::test_x" in result
    assert result.startswith("pytest ")


def test_inject_pytest_path_prefixed() -> None:
    """pytest invoked via a path (e.g. .venv/bin/pytest) is still detected."""
    result = inject_quarantine_exclusions(
        ".venv/bin/pytest", ["tests/flaky.py"]
    )
    assert "--deselect " in result
    assert "tests/flaky.py" in result


def test_inject_jest_gets_testpathignorepatterns() -> None:
    """jest command gets --testPathIgnorePatterns with escaped+joined patterns."""
    result = inject_quarantine_exclusions(
        "jest", ["tests/foo.test.js", "tests/bar.test.js"]
    )
    # The . in .test.js should be regex-escaped to \.test\.js
    assert "--testPathIgnorePatterns=" in result
    assert r"tests/foo\.test\.js" in result
    assert r"tests/bar\.test\.js" in result
    assert "|" in result
    assert result.startswith("jest ")


def test_inject_jest_single_pattern_no_pipe() -> None:
    """Single jest pattern: no pipe separator needed."""
    result = inject_quarantine_exclusions("jest --coverage", ["tests/x.test.ts"])
    assert "--testPathIgnorePatterns=" in result
    after_flag = result.split("--testPathIgnorePatterns=", 1)[1]
    assert "|" not in after_flag


def test_inject_unknown_framework_unchanged() -> None:
    """cargo test (unknown framework) returns command unchanged."""
    result = inject_quarantine_exclusions(
        "cargo test", ["tests/a.rs"]
    )
    assert result == "cargo test"


def test_inject_go_test_unchanged() -> None:
    """go test (unknown framework) returns command unchanged."""
    result = inject_quarantine_exclusions(
        "go test ./...", ["pkg/flaky_test.go"]
    )
    assert result == "go test ./..."


def test_inject_empty_patterns_unchanged() -> None:
    """Empty patterns list returns command unchanged."""
    result = inject_quarantine_exclusions("pytest -q", [])
    assert result == "pytest -q"


def test_inject_multiple_patterns_all_injected() -> None:
    """All patterns are injected; none are silently dropped."""
    patterns = ["a.py", "b.py", "c.py"]
    result = inject_quarantine_exclusions("pytest", patterns)
    for p in patterns:
        assert "--deselect " in result
        assert p in result


def test_inject_compound_pytest_command() -> None:
    """Compound pytest command (with flags) appends --deselect correctly."""
    result = inject_quarantine_exclusions(
        "pytest -q --cov=src", ["tests/x.py"]
    )
    assert result.startswith("pytest -q --cov=src ")
    assert "--deselect " in result
    assert "tests/x.py" in result


def test_inject_npx_jest_detected() -> None:
    """npx jest is detected as a jest framework."""
    result = inject_quarantine_exclusions(
        "npx jest", ["tests/x.test.js"]
    )
    assert "--testPathIgnorePatterns=" in result


def test_inject_jest_with_flags() -> None:
    """jest with flags still gets the exclusion appended."""
    result = inject_quarantine_exclusions(
        "jest --verbose --coverage", ["tests/a.test.js"]
    )
    assert result.startswith("jest --verbose --coverage ")
    assert "--testPathIgnorePatterns=" in result


def test_inject_shell_safe_pytest() -> None:
    """Patterns with shell metacharacters are shlex.quote'd for pytest."""
    result = inject_quarantine_exclusions(
        "pytest", ["tests/foo; rm -rf /"]
    )
    # The semicolon and spaces must be quoted, not raw.
    assert "; rm -rf /" not in result or "'" in result
    # shlex.quote wraps in single quotes; the raw semicolon should not appear
    # as a shell command separator.
    raw_semi = result.split("--deselect ", 1)[1]
    assert raw_semi.startswith("'")


def test_inject_shell_safe_jest() -> None:
    """Patterns with shell metacharacters are shlex.quote'd for jest."""
    result = inject_quarantine_exclusions(
        "jest", ["tests/foo$(whoami).test.js"]
    )
    # The $(...) must be quoted so the shell doesn't execute it.
    after_flag = result.split("--testPathIgnorePatterns=", 1)[1]
    assert after_flag.startswith("'")
