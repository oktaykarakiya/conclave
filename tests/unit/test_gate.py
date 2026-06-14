"""Unit tests for gate outcome classification (ENG-7).

Covers all four ``GateOutcome`` values plus the skipped / no-command path.
"""

from __future__ import annotations

from pathlib import Path

from conclave.engine.gate import GateResult, run_tests


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
