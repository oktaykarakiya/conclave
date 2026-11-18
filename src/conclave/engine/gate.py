"""The verification test gate.

Runs the project's (learned or configured) test command inside the task worktree.
The green-gate requires a clean exit; quarantine patterns are injected as
framework-appropriate exclusions (pytest ``--deselect``, jest
``--testPathIgnorePatterns``) so quarantined flaky tests don't fail the gate.
Expired patterns are never injected — the SQL filter enforces ``until >= today``,
so the integrity metric (expired = unhealthy) is preserved.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ..util import today_iso
from .gitio import run_shell

if TYPE_CHECKING:
    from ..db import Database

# --- Framework detection regexes -------------------------------------------
# Match pytest/jest as a standalone word or as a path component
# (e.g. ".venv/bin/pytest", "npx jest", "jest --coverage").
_PYTEST_RE = re.compile(r"(^|[\s/])pytest([\s$]|$)")
_JEST_RE = re.compile(r"(^|[\s/])jest([\s$]|$)")

GateOutcome = Literal["passed", "failed", "timed_out", "missing_command", "cancelled"]
"""Classification of a gate run so the orchestrator can distinguish infra failures
(exit 124 = timeout, exit 127 = command not found) from real test failures. ``cancelled`` is a run
stopped (or never started) by its cancel event: no evidence either way."""


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
    cancel_event: asyncio.Event | None = None,
) -> GateResult:
    """Run the test command; ``passed`` is a clean (exit 0) run. No command => skipped.

    ``output`` is the command's complete combined output. Callers that put it in a prompt fit
    it there (:mod:`conclave.engine.context_limits`), so no cut is hidden here. Once
    *cancel_event* is set the command is stopped (its process group killed) or never started,
    and the run is ``cancelled``.
    """
    if not command:
        return GateResult(
            passed=True, exit_code=0, output="(no test command configured)", skipped=True,
            outcome="passed",
        )
    code, output = await run_shell(
        worktree, command, env=env, timeout_seconds=timeout_seconds, cancel_event=cancel_event
    )
    if cancel_event is not None and cancel_event.is_set():
        return GateResult(passed=False, exit_code=code, output=output, outcome="cancelled")
    return GateResult(
        passed=code == 0,
        exit_code=code,
        output=output,
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

    The flags go to the end of the last command of *command* that runs the framework, so a
    compound gate (``pytest -q && mypy``, ``pytest -q | tee log``) passes them to the test
    runner, not to whatever follows it (see :func:`_append_to_runner`).

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
        return _append_to_runner(command, _PYTEST_RE, deselections)

    if _JEST_RE.search(command):
        # re.escape so literal paths don't act as accidental regexes;
        # shlex.quote so the joined regex is a single shell-safe argument.
        escaped = [re.escape(p) for p in patterns]
        joined = "|".join(escaped)
        return _append_to_runner(
            command, _JEST_RE, f"--testPathIgnorePatterns={shlex.quote(joined)}"
        )

    return command


def _append_to_runner(command: str, runner: re.Pattern[str], flags: str) -> str:
    """*command* with *flags* at the end of its last command matching *runner*.

    When that command is the last one, or *command* cannot be split with confidence (see
    :func:`_shell_segments`), the flags go to the end of *command*, as they always did.
    """
    segments = _shell_segments(command)
    matching = [
        end for start, end in segments or () if runner.search(command[start:end])
    ]
    if not matching or not command[matching[-1] :].strip():
        return f"{command.rstrip()} {flags}"
    head = command[: matching[-1]].rstrip()
    return f"{head} {flags}{command[len(head) :]}"


def _shell_segments(command: str) -> list[tuple[int, int]] | None:
    """The ``(start, end)`` spans of *command*'s top-level commands, or ``None`` when unsure.

    Splits on the control operators outside quotes, escapes and parentheses: ``&&``, ``||``,
    ``;``, a newline, ``|`` (and ``|&``) and a background ``&``, never on the ``&`` of a
    redirection (``2>&1``, ``>&``, ``<&``, ``&>``). ``None`` for an unclosed quote or unbalanced
    parentheses (e.g. a ``case`` pattern), so the caller keeps its old placement.
    """
    spans: list[tuple[int, int]] = []
    start = depth = i = 0
    quote = ""
    n = len(command)
    while i < n:
        ch = command[i]
        if quote:
            if ch == "\\" and quote != "'":
                i += 2
                continue
            if ch == quote:
                quote = ""
            i += 1
            continue
        operator = 0  # length of the control operator at i, if any
        if ch == "\\":
            i += 2
            continue
        if ch in "'\"`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return None
        elif depth == 0:
            nxt = command[i + 1] if i + 1 < n else ""
            if ch in ";\n":
                operator = 1
            elif ch == "|":
                operator = 2 if nxt and nxt in "|&" else 1
            elif ch == "&":
                if nxt == "&":
                    operator = 2
                elif not (nxt == ">" or (i and command[i - 1] in "<>")):
                    operator = 1
        if operator:
            spans.append((start, i))
            start = i + operator
            i += operator
            continue
        i += 1
    if quote or depth:
        return None
    spans.append((start, n))
    return spans


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
