"""Bug-Fixer MODE CONTROLLER — the keystone that drives one autonomous fix cycle.

The discovery sweep, the ``repro`` synthesis/parse, the reproduction gate, the green-gate, and the
status ledger already exist as independent pieces; this controller is what wires them into a single
self-driving loop. :meth:`BugFixerController.run_cycle` runs ONE candidate end-to-end:

1. :func:`conclave.engine.discovery.discover_bug` → a fresh ``discovered`` candidate, or ``None``
   when there is nothing to hunt this turn (the loop's "idle" signal).
2. Dispatch the ``repro`` persona through the existing :class:`AgentRunner`/:class:`Provider`
   plumbing and parse its one block with :func:`conclave.engine.repro.parse_repro_test`. A reply
   that breaks the one-block contract yields no usable :class:`ReproTest`, so the candidate is
   *deferred* (a legal ``discovered → deferred`` edge) rather than forced down an illegal path.
3. :func:`conclave.engine.repro_gate.reproduce_bug` proves (or refutes) the candidate. Every
   :class:`ReproOutcome` is handled: ``reproduced`` advances to the fix step; ``dismissed`` /
   ``declined`` were already DB-transitioned inside the gate (the controller only logs them);
   ``infra`` / ``rejected`` / ``ineligible`` changed nothing, so the controller skips without
   touching the status.
4. A ``reproduced`` candidate is FIXED as a real Task: the controller advances it to ``fixing``,
   creates a :attr:`TaskOrigin.bug_fixer` Task whose request hands the developer the bug (file /
   symbol / claim) AND the PINNED repro test (path + body) with explicit instructions to ADD that
   test to the worktree and make the FULL green-gate pass, and runs it through
   :meth:`Orchestrator.process_task`. A merged task advances the candidate to ``fixed``; a failed
   one parks it at ``deferred`` for a later retry.

HOW THE REPRO TEST REACHES THE GATE: ``process_task`` runs the project's whole-suite green-gate
inside the task worktree. The controller does not run the test itself — it instructs the developer
to write the pinned test (verbatim body, at its vetted path) INTO that worktree, so the orchestrator
collects and runs it as part of the authoritative gate. A real fix therefore has to make the suite
green WITH the repro test present, which is exactly the "earn the green" guarantee the bug-fixer is
built around.

SOLE WRITER: this controller is the ONLY component that writes ``bug_candidates.status`` from the
discovered → fixing → {fixed, deferred} side. Every write goes through
:func:`conclave.db.repositories.transition_bug_status`, which guards each edge against
:data:`conclave.db.models.BUG_STATUS_TRANSITIONS`; an illegal edge raises rather than silently
no-opping. The reproduction gate owns its own three transitions (reproduced / dismissed / declined)
— the controller never re-drives those.

CONSENSUS FOLLOW-UP: a ``declined`` candidate already lands in ``declined_needs_human`` (surfaced
via :func:`conclave.db.repositories.list_needs_human`), which is the v1 human-handoff. A full
multi-agent *decline consensus* round (every mandatory reviewer voting before an auto-fix is
trusted) is a deliberate, documented follow-up — see the ``# TODO(bug-fixer-consensus)`` note on
:meth:`_fix_reproduced` — and is intentionally NOT built here.

SECRETS HYGIENE: discovery's and the repro gate's local-only-sink discipline carries over. The
repro-test body is persisted only to this project's SQLite row and written only into the throwaway /
task worktrees; it is never logged or emitted off-box. The Task request does embed the body so the
developer can re-create the test, but a Task row is the same local SQLite sink as the candidate row.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..config import ConclaveConfig, resolve_agent
from ..config.models import BugFixerSessionConfig
from ..db import BugCandidate, BugStatus, Database, Project, Task, TaskOrigin, TaskState
from ..db import repositories as repo
from ..events import EventBus, EventType
from ..providers import Provider
from .discovery import discover_bug
from .orchestrator import Orchestrator
from .repro import ReproTest, parse_repro_test
from .repro_gate import ReproOutcome, reproduce_bug
from .runner import AgentRunner
from .worktree import WorktreeManager

# Match pytest as a standalone word or path component (".venv/bin/pytest", "python -m pytest"),
# mirroring :func:`conclave.engine.gate.inject_quarantine_exclusions`. When the project test command
# is pytest-shaped we scope the reproduction run to JUST the repro path; otherwise we fall back to
# the whole-suite command (an indirect runner like ``npm test`` / ``make test`` cannot be narrowed).
_PYTEST_RE = re.compile(r"(^|[\s/])pytest([\s$]|$)")


class CycleOutcome(StrEnum):
    """How one :meth:`BugFixerController.run_cycle` resolved — the loop's branch signal.

    The worker only needs to know whether the cycle DID work (so it resets idle backoff) or found
    nothing (so it backs off); the finer outcomes are for tests and event/audit clarity.
    """

    idle = "idle"  # nothing to hunt — no candidate discovered this turn
    fixed = "fixed"  # reproduced → fix merged → candidate fixed
    deferred = "deferred"  # reproduced → fix failed (or no usable repro) → candidate deferred
    dismissed = "dismissed"  # repro passed on unfixed code → false positive (gate transitioned)
    declined = "declined"  # covered-behaviour change → needs a human (gate transitioned)
    skipped = "skipped"  # infra / rejected / ineligible — nothing changed, try again later

    @property
    def did_work(self) -> bool:
        """True when the cycle acted on a candidate (anything but a no-candidate idle turn)."""
        return self is not CycleOutcome.idle


@dataclass(frozen=True)
class CycleResult:
    """The outcome of one cycle plus the candidate it acted on (``None`` for an idle turn)."""

    outcome: CycleOutcome
    candidate: BugCandidate | None = None


class BugFixerController:
    """Drives the autonomous bug-fixer mode: discover → reproduce → fix, one candidate per cycle.

    Constructed from an :class:`Orchestrator` so a fix reuses the exact same task pipeline (and its
    green-gate, merge, and cancellation handling) that operator tasks run through — there is no
    second execution path to keep in sync. The controller borrows the orchestrator's db / bus /
    provider / home rather than taking its own, so both always observe one shared state.
    """

    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orchestrator = orchestrator
        self._db: Database = orchestrator._db
        self._bus: EventBus = orchestrator._bus
        self._provider: Provider = orchestrator._provider
        self._home: Path = orchestrator._home

    async def run_cycle(
        self,
        project: Project,
        config: ConclaveConfig,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> CycleResult:
        """Run one discover → reproduce → fix cycle for *project*; return what it resolved.

        Read-only-then-act: discovery and the reproduction gate never advance a candidate the
        controller would not, and the gate owns its own dismissed / declined / infra handling, so
        the controller only ever drives the discovered → fixing → {fixed, deferred} edges itself —
        always through the guarded :func:`transition_bug_status`. ``cancel_event`` is forwarded to
        discovery, the ``repro`` dispatch, and (via the task's own registration) the fix, so a stop
        request is honoured between and within stages.
        """
        # (a) Discover — a genuinely new `discovered` candidate, or nothing to hunt this turn.
        candidate = await discover_bug(
            self._db, self._bus, self._provider, project, config, cancel_event=cancel_event,
        )
        if candidate is None:
            return CycleResult(CycleOutcome.idle)

        # (b) Synthesize + parse the repro test. No usable repro → defer (a legal discovered edge),
        # never a forced illegal transition.
        repro = await self._synthesize_repro(project, config, candidate, cancel_event=cancel_event)
        if repro is None:
            deferred = await self._defer(
                candidate, "no usable repro test synthesized — deferring for a later sweep"
            )
            return CycleResult(CycleOutcome.deferred, deferred)

        # (c) Prove it. The gate transitions the candidate for reproduced / dismissed / declined and
        # leaves it untouched for infra / rejected / ineligible — handle EVERY outcome. v1 does not
        # compute the covered-behaviour signal (which would need coverage/existing-test context), so
        # ``asserts_covered_behavior_change`` keeps its safe default of ``False``; the gate's
        # ``declined`` route is still fully handled for when a richer caller does supply it.
        result = await reproduce_bug(
            self._db, self._bus, project, config, candidate, repro,
            wm=self._worktree_manager(project),
            test_command=self._repro_test_command(config, repro),
            timeout_seconds=self._gate_timeout(config),
        )

        if result.outcome is ReproOutcome.reproduced:
            return await self._fix_reproduced(project, result.candidate, repro)
        if result.outcome is ReproOutcome.dismissed:
            await self._log(project.id, result.candidate, "dismissed false positive (gate)")
            return CycleResult(CycleOutcome.dismissed, result.candidate)
        if result.outcome is ReproOutcome.declined:
            await self._log(project.id, result.candidate, "declined → needs human (gate)")
            return CycleResult(CycleOutcome.declined, result.candidate)
        # infra / rejected / ineligible — the gate changed nothing; skip and retry a later cycle.
        await self._log(
            project.id, result.candidate, f"repro inconclusive ({result.outcome.value}) — skipping"
        )
        return CycleResult(CycleOutcome.skipped, result.candidate)

    # --- fix step -----------------------------------------------------------

    async def _fix_reproduced(
        self,
        project: Project,
        candidate: BugCandidate,
        repro: ReproTest,
    ) -> CycleResult:
        """Fix a ``reproduced`` candidate as a real ``bug_fixer`` Task; record the outcome.

        Advances the candidate to ``fixing`` (which bumps ``attempts``), builds a task that hands
        the developer the bug AND the pinned repro test, and runs it through the orchestrator's full
        pipeline. A merged task → ``fixed``; a failed task → ``deferred`` (parked for a later retry,
        a legal edge the controller drives itself).

        TODO(bug-fixer-consensus): before trusting an auto-fix, run a decline-consensus round (every
        mandatory reviewer votes; ``DeclineConsensus`` thresholds in ``AgentsPolicy``) so the team
        can ABSTAIN on an edge-case-risky candidate and route it to ``declined_needs_human`` instead
        of fixing it. v1 trusts the reproduction gate's proof and fixes directly; consensus is a
        documented follow-up, deliberately not built here.
        """
        # The pinned body/hash live on the row after the gate's set_repro_artifacts; prefer them so
        # the task carries EXACTLY what was proven, falling back to the in-memory ReproTest.
        pinned_path = candidate.repro_test_path or repro.path
        pinned_body = candidate.repro_test_body or repro.body

        task = await repo.create_task(
            self._db,
            project_id=project.id,
            title=self._task_title(candidate),
            request=_fix_request(candidate, pinned_path, pinned_body),
            origin=TaskOrigin.bug_fixer,
            state=TaskState.approved,  # ready to run; the controller drives it, not an operator
        )
        # Link the driving task onto the candidate as we enter `fixing` (also bumps attempts).
        await repo.transition_bug_status(
            self._db, candidate.id, BugStatus.fixing, task_id=task.id,
        )

        merged = await self._run_fix_task(task)

        if merged:
            fixed = await repo.transition_bug_status(self._db, candidate.id, BugStatus.fixed)
            await self._log(project.id, fixed, f"fix merged (task {task.id}) → fixed")
            return CycleResult(CycleOutcome.fixed, fixed)

        # A failed fix falls back along the designed edge ``fixing → reproduced`` (the table's
        # "retry" route), THEN parks at ``deferred`` so a later sweep can un-park it
        # (``deferred → reproduced`` is legal). Going straight ``fixing → deferred`` is NOT a legal
        # edge, so the controller never forces it — it walks the two real edges instead.
        await repo.transition_bug_status(self._db, candidate.id, BugStatus.reproduced)
        deferred = await repo.transition_bug_status(
            self._db, candidate.id, BugStatus.deferred,
            decline_reason=f"auto-fix task {task.id} did not merge — deferring for a later retry",
        )
        await self._log(project.id, deferred, f"fix task {task.id} failed → deferred")
        return CycleResult(CycleOutcome.deferred, deferred)

    async def _run_fix_task(self, task: Task) -> bool:
        """Run a fix task through the orchestrator with cancellation wired exactly like the worker.

        Registers a per-task cancel event in the orchestrator's table so an operator stop reaches
        an in-flight fix, and always cleans the entry afterwards — the same belt-and-suspenders the
        task worker uses. Returns ``process_task``'s bool (``True`` == done/merged).
        """
        cancel_event = asyncio.Event()
        self._orchestrator._cancel_events[task.id] = cancel_event
        try:
            return await self._orchestrator.process_task(task, cancel_event=cancel_event)
        finally:
            self._orchestrator._cancel_events.pop(task.id, None)

    # --- repro synthesis ----------------------------------------------------

    async def _synthesize_repro(
        self,
        project: Project,
        config: ConclaveConfig,
        candidate: BugCandidate,
        *,
        cancel_event: asyncio.Event | None,
    ) -> ReproTest | None:
        """Dispatch the ``repro`` persona over the candidate and parse its single test block.

        Read-only on the working tree (the persona only proposes a test; the reproduction gate is
        the writer). A dispatch that errors or whose reply breaks the one-block contract yields
        ``None``, which the caller turns into a *deferred* candidate rather than a forced edge.
        """
        runner = AgentRunner(self._db, self._bus, self._provider, project.id, config)
        result = await runner.run(
            agent="repro",
            prompt=_repro_prompt(candidate),
            worktree=Path(project.path),
            cancel_event=cancel_event,
        )
        if not result.ok or not result.text:
            return None
        return parse_repro_test(result.text)

    # --- status helpers (always guarded) ------------------------------------

    async def _defer(self, candidate: BugCandidate, reason: str) -> BugCandidate:
        """Park a candidate at ``deferred`` (a legal discovered/reproduced edge) with a reason."""
        deferred = await repo.transition_bug_status(
            self._db, candidate.id, BugStatus.deferred, decline_reason=reason,
        )
        await self._log(candidate.project_id, deferred, reason)
        return deferred

    # --- derivations --------------------------------------------------------

    def _worktree_manager(self, project: Project) -> WorktreeManager:
        """The shared per-project WorktreeManager (same root layout the orchestrator uses)."""
        return WorktreeManager(
            Path(project.path), self._home / "projects" / project.id / "worktrees",
        )

    def _repro_test_command(self, config: ConclaveConfig, repro: ReproTest) -> str | None:
        """Build the command that runs JUST the repro test inside the throwaway worktree.

        Scopes the project's configured test command to the single repro path when that command is
        pytest-shaped (the ``repro`` persona's target), so the gate observes the repro's own pass /
        fail not the whole suite's. A non-pytest / indirect runner cannot be narrowed safely,
        so it falls back to the full command; a project with no configured command yields ``None``
        (the gate then reports infra/inconclusive, never a false dismissal).
        """
        base = config.execution.baseline_test_command
        if not base:
            return None
        if _PYTEST_RE.search(base):
            return f"{base.rstrip()} {shlex.quote(repro.path)}"
        return base

    def _gate_timeout(self, config: ConclaveConfig) -> int:
        """Reproduction-gate timeout in seconds, reusing the tester agent's per-dispatch timeout."""
        return resolve_agent(config, "tester").timeout_minutes * 60

    def _task_title(self, candidate: BugCandidate) -> str:
        """A short, human-scannable task title naming the buggy symbol/file."""
        where = candidate.symbol or candidate.file or candidate.region or "unknown location"
        return f"bug-fix: {where}"

    async def _log(self, project_id: str, candidate: BugCandidate, message: str) -> None:
        """Emit a local ``log`` event tagged to the bug-fixer phase (LOCAL-ONLY sink).

        Carries the candidate id and its current status but never the repro body — same hygiene as
        the discovery and reproduction-gate events.
        """
        await self._bus.emit(
            type=EventType.log,
            project_id=project_id,
            payload={
                "stage": "bug_fixer",
                "message": message,
                "candidate_id": candidate.id,
                "status": candidate.status.value,
            },
        )


# --- session-budget metering (worker-side) ----------------------------------


@dataclass
class SessionBudget:
    """Per-session caps the worker meters around :meth:`BugFixerController.run_cycle`.

    The controller drives ONE candidate per cycle; the worker loop enforces a whole session's
    bounds, so the metering lives here next to the controller it gates rather than inside it. Built
    from a resolved :class:`BugFixerSessionConfig` (caps + already-applied wall-clock fallback).

    * ``max_candidates`` counts cycles that actually acted on a candidate (an idle no-candidate turn
      is not a candidate pursued), matching the policy field's "candidates the controller pursues".
    * ``wall_clock_budget_minutes`` of ``0`` disables the wall, mirroring
      ``execution.wall_clock_budget_minutes``.
    """

    max_candidates: int
    wall_clock_seconds: float
    candidates_pursued: int = 0

    @classmethod
    def from_config(cls, session: BugFixerSessionConfig) -> SessionBudget:
        return cls(
            max_candidates=session.max_candidates,
            wall_clock_seconds=session.wall_clock_budget_minutes * 60.0,
        )

    def record(self, result: CycleResult) -> None:
        """Count a completed cycle that pursued a candidate toward the candidate cap."""
        if result.outcome.did_work:
            self.candidates_pursued += 1

    def exhausted(self, *, elapsed_seconds: float) -> bool:
        """True once the candidate cap is hit or the (enabled) wall-clock budget is exceeded."""
        if self.candidates_pursued >= self.max_candidates:
            return True
        if self.wall_clock_seconds > 0 and elapsed_seconds >= self.wall_clock_seconds:
            return True
        return False


# --- prompt builders --------------------------------------------------------


def _repro_prompt(candidate: BugCandidate) -> str:
    """The candidate-scoping task body for the ``repro`` persona; its persona carries the contract.

    Hands the persona the suspected bug's coordinates and asks for exactly one focused failing test.
    The persona owns the output contract (the single fenced ``repro`` block); this only supplies the
    target so the synthesized test asserts the CORRECT behaviour for this specific claim.
    """
    lines = ["A suspected bug has been discovered. Write ONE focused test that proves it.", ""]
    if candidate.file:
        lines.append(f"FILE: {candidate.file}")
    if candidate.symbol:
        lines.append(f"SYMBOL: {candidate.symbol}")
    lines.append(f"CLAIM: {candidate.claim}")
    lines.append("")
    lines.append(
        "Write a single test that asserts the CORRECT behaviour — one that FAILS on the current "
        "(buggy) code and will pass once the bug is fixed. End your reply with the single fenced "
        "`repro` block your persona specifies (first line `path: <relative test path>`, then the "
        "verbatim test body), or no block at all if you cannot state such a test."
    )
    return "\n".join(lines)


def _fix_request(candidate: BugCandidate, repro_path: str, repro_body: str) -> str:
    """Build the developer task request: the bug, the pinned repro test, and the green-gate bar.

    The request embeds the proven test VERBATIM at its vetted path and tells the developer to ADD it
    to the worktree before fixing, so the orchestrator's whole-suite green-gate (which it appends
    itself) then collects and runs it. Making that gate pass WITH the repro test present is the
    fix's acceptance bar — the developer cannot "pass" by leaving the bug unfixed.
    """
    where_bits = [b for b in (candidate.file, candidate.symbol) if b]
    where = " / ".join(where_bits) if where_bits else (candidate.region or "the codebase")
    return (
        "An autonomous bug-fixer reproduced a real defect and proved it with a focused failing "
        "test. Fix the bug so the FULL green-gate — which now INCLUDES this reproduction test — "
        "passes.\n\n"
        f"BUG LOCATION: {where}\n"
        f"CLAIM (the wrong behaviour to fix): {candidate.claim}\n\n"
        "REPRODUCTION TEST (already proven to FAIL on the current code). You MUST add this file to "
        "the worktree EXACTLY as given, at this path, and you must NOT weaken, rename, or delete "
        f"it:\n\nPATH: {repro_path}\n\n"
        f"```python\n{repro_body}\n```\n\n"
        "STEPS:\n"
        f"1. Create `{repro_path}` with the exact test body above.\n"
        "2. Fix the underlying bug in the source so the behaviour the test asserts is correct.\n"
        "3. Run the authoritative green-gate (given below) and iterate until it passes with NO new "
        "failures versus the pre-existing baseline — the reproduction test MUST pass, and you must "
        "not have removed or weakened any pre-existing test to get there."
    )
