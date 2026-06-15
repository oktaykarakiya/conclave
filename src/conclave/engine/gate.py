"""The verification test gate.

Runs the project's (learned or configured) test command inside the task worktree.
The green-gate requires a clean exit; quarantine patterns are injected as
framework-appropriate exclusions (pytest ``--deselect``, jest
``--testPathIgnorePatterns``) so quarantined flaky tests don't fail the gate.
Expired patterns are never injected — the SQL filter enforces ``until >= today``,
so the integrity metric (expired = unhealthy) is preserved.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ..util import today_iso
from .baseline import trim_output
from .gitio import run_shell

if TYPE_CHECKING:
    from ..db import Database

# --- Framework detection regexes -------------------------------------------
# Match pytest/jest as a standalone word or as a path component
# (e.g. ".venv/bin/pytest", "npx jest", "jest --coverage").
_PYTEST_RE = re.compile(r"(^|[\s/])pytest([\s$]|$)")
_JEST_RE = re.compile(r"(^|[\s/])jest([\s$]|$)")

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


# --- Quarantine exclusion injection ---------------------------------------


def inject_quarantine_exclusions(command: str, patterns: list[str]) -> str:
    """Inject test exclusions for active quarantine *patterns* into *command*.

    Detects the test framework from the command string via regex heuristics
    and appends framework-appropriate exclusion flags. Every pattern is
    shell-escaped via :func:`shlex.quote` before interpolation — the command
    runs through :func:`~conclave.engine.gitio.run_shell` (which delegates
    to ``create_subprocess_shell``), so shell metacharacters in a pattern
    would otherwise be interpreted by the shell.

    * pytest → ``--deselect <quoted-pattern>`` per pattern
    * jest   → ``--testPathIgnorePatterns=<quoted-regex>`` (patterns are
      ``re.escape``'d so literal file paths don't act as accidental regexes,
      then joined with ``|``, then shell-quoted as a single argument)
    * unknown → *command* returned unchanged (no injection)

    .. note::

       Operators should configure explicit ``pytest`` / ``jest`` commands in
       ``baseline_test_command`` or repo knowledge for quarantine to take
       effect.  Indirect runners (``npm test``, ``make test``) can't be
       framework-detected and will skip injection — quarantine patterns
       won't be excluded in those cases.
    """
    if not patterns:
        return command

    if _PYTEST_RE.search(command):
        deselections = " ".join(
            f"--deselect {shlex.quote(p)}" for p in patterns
        )
        return f"{command.rstrip()} {deselections}"

    if _JEST_RE.search(command):
        # re.escape so literal paths don't act as accidental regexes;
        # shlex.quote so the joined regex is a single shell-safe argument.
        escaped = [re.escape(p) for p in patterns]
        joined = "|".join(escaped)
        return f"{command.rstrip()} --testPathIgnorePatterns={shlex.quote(joined)}"

    return command


async def apply_quarantine(
    db: Database, project_id: str, command: str | None,
) -> str | None:
    """Fetch active (non-expired) quarantine patterns and inject them as
    framework exclusions via :func:`inject_quarantine_exclusions`.

    Returns *command* unchanged when it is ``None`` or there are no active
    patterns.  Expired patterns are filtered by the SQL query itself
    (``until >= today``) so they never reach the injector — the integrity
    metric (expired = unhealthy) is preserved.

    .. warning::

       The baseline cache is keyed by ``(project_id, checkpoint_sha)``.
       If quarantine patterns change between runs on the same SHA, the
       cached baseline may reflect the old exclusions.  This is rare
       (operator-driven) and the worst case is a stale baseline preamble.
    """
    if not command:
        return command

    from ..db import repositories as repo

    entries = await repo.active_quarantine(db, project_id, today_iso())
    patterns = [e.pattern for e in entries]
    return inject_quarantine_exclusions(command, patterns)


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
