"""The verification test gate.

Runs the project's (learned or configured) test command inside the task worktree.
The green-gate requires a clean exit; quarantine governance (expiry-enforced) lives
in the verification layer. Selective per-suite exclusion is framework-specific and a
later refinement — the command itself is the source of truth here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .baseline import trim_output
from .gitio import run_shell

GateOutcome = Literal["passed", "failed", "timed_out", "missing_command"]
"""Classification of a gate run so the orchestrator can distinguish infra failures
(exit 124 = timeout, exit 127 = command not found) from real test failures."""


@dataclass(frozen=True)
class GateResult:
    passed: bool
    exit_code: int
    output: str
    outcome: GateOutcome = "passed"
    skipped: bool = False


async def run_tests(
    worktree: Path,
    command: str | None,
    *,
    env: dict[str, str] | None = None,
    timeout_seconds: int = 1800,
) -> GateResult:
    """Run the test command; ``passed`` is a clean (exit 0) run. No command => skipped."""
    if not command:
        return GateResult(
            passed=True, exit_code=0, output="(no test command configured)", skipped=True,
            outcome="passed",
        )
    code, output = await run_shell(worktree, command, env=env, timeout_seconds=timeout_seconds)
    return GateResult(
        passed=code == 0,
        exit_code=code,
        output=trim_output(output),
        outcome=_classify(code),
    )


# --- Internal helpers ---------------------------------------------------------


def _classify(exit_code: int) -> GateOutcome:
    """Map an exit code to a ``GateOutcome``.

    * 0   → passed
    * 124 → timed_out (the shell / ``run_shell`` timeout path uses 124)
    * 127 → missing_command (shell can't find the first word)
    * anything else → failed (real test failure)
    """
    if exit_code == 0:
        return "passed"
    if exit_code == 124:
        return "timed_out"
    if exit_code == 127:
        return "missing_command"
    return "failed"
