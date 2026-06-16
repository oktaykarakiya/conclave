"""Reproduction gate — prove a discovered bug with a test that FAILS on the unfixed code.

This is the Bug-Fixer's "prove it first" stage and the counterpart to discovery: discovery finds
a suspected bug and the ``repro`` persona synthesizes a focused failing test
(:func:`conclave.engine.repro.parse_repro_test`); this gate is what makes that test EARN the
``reproduced`` status by running it against the current, still-buggy checkpoint and DEMANDING a
real assertion failure. Nothing advances a candidate toward an auto-fix on the strength of a
synthesized test alone — the test has to actually fail on the code as it stands.

:func:`reproduce_bug` drives one ``discovered`` candidate through the gate:

* RE-VALIDATE the model-derived test path with the *bf-repro-pathguard*
  (:func:`conclave.engine.repro.repro_pathguard`) BEFORE touching the filesystem. The path was
  already vetted when the :class:`ReproTest` was parsed, so this is defense in depth — the same
  "re-resolve inside the sandbox" discipline :func:`conclave.engine.verdict.check_grounding` uses
  on evidence paths. A rejected path short-circuits the whole gate before any worktree is created
  or any byte is written.
* Create a CLEAN, throwaway worktree at the current checkpoint via the shared
  :class:`WorktreeManager`, drop the test into THAT worktree ONLY (never the operator's checkout),
  and run it through :func:`conclave.engine.gate.run_tests`.
* CLASSIFY the :class:`GateResult` strictly:

  - ``failed`` (a real assertion failure) → the bug is REPRODUCED: pin the proven test
    (path + body + a SHA-256 of the body, the *bf-integrity-repro-pin* used later for an
    as-merged re-verification), advance ``discovered → reproduced``, and announce
    ``bug.reproduced``.
  - ``passed`` → the asserted-correct behaviour ALREADY holds on the unfixed code, so the claim
    was a FALSE POSITIVE: advance ``discovered → dismissed_false_positive`` and announce
    ``bug.dismissed``.
  - ``timed_out`` / ``missing_command`` (or a skipped, uncommanded run) → INFRA NOISE, not proof
    either way: mirror ENG-7 by retrying once and, if it persists, abort WITHOUT transitioning the
    candidate. A toolchain failure must never be mistaken for a reproduced (or dismissed) bug.

A repro that asserts a behaviour CHANGE in already-covered code (signalled by the caller, the only
layer with the coverage / existing-test context to judge it) is held back from the auto-
``reproduced`` path: even a genuine failure routes to ``declined_needs_human`` so a human can
confirm it is a real defect rather than a disagreement with intended, already-tested behaviour.

SECRETS HYGIENE: a synthesized test body can quote source that includes a hardcoded secret. The
body is written only into the throwaway worktree and persisted only to this project's SQLite row
(:func:`conclave.db.repositories.set_repro_artifacts`) — both local-only sinks, exactly as in
discovery. The emitted events carry the test PATH and its hash, never the body, so the gate opens
no new exfiltration surface.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..config import ConclaveConfig
from ..db import BugCandidate, BugStatus, Database, Project
from ..db import repositories as repo
from ..events import EventBus, EventType
from .gate import GateResult, run_tests
from .repro import ReproTest, repro_pathguard
from .worktree import WorktreeError, WorktreeManager

# Gate outcomes that are toolchain noise rather than evidence about the bug — neither a
# reproduction nor a dismissal. Mirrors the orchestrator's ENG-7 infra-failure handling.
_INFRA_OUTCOMES = frozenset({"timed_out", "missing_command"})


class ReproOutcome(StrEnum):
    """How one trip through the reproduction gate resolved."""

    reproduced = "reproduced"  # failed on the unfixed code → a real bug, pinned
    dismissed = "dismissed"  # passed on the unfixed code → false positive
    declined = "declined"  # failed, but a covered-behaviour change → needs a human
    infra = "infra"  # timed out / missing command / skipped → no proof either way
    rejected = "rejected"  # bf-repro-pathguard rejected the path → nothing was written
    ineligible = "ineligible"  # candidate was not in `discovered`


class ReproResult(BaseModel):
    """The gate's classification plus the candidate as it stands after the gate ran.

    For :attr:`ReproOutcome.reproduced`/``dismissed``/``declined`` the ``candidate`` is the
    post-transition row (new status, and for ``reproduced`` the pinned artifacts); for
    ``infra``/``rejected``/``ineligible`` no DB change happened and it is the input candidate.
    """

    outcome: ReproOutcome
    candidate: BugCandidate


async def reproduce_bug(
    db: Database,
    bus: EventBus,
    project: Project,
    config: ConclaveConfig,
    candidate: BugCandidate,
    repro: ReproTest,
    *,
    wm: WorktreeManager,
    test_command: str | None,
    timeout_seconds: int = 1800,
    asserts_covered_behavior_change: bool = False,
) -> ReproResult:
    """Prove (or refute) one ``discovered`` candidate with its synthesized repro test.

    Runs ``repro`` in a fresh throwaway worktree at the target branch's current tip — the unfixed
    checkpoint where the bug, if real, still lives — and classifies the gate result per this
    module's contract. ``test_command`` is the command that executes the repro inside the worktree
    (caller-scoped to the repro test, mirroring :func:`run_tests`); a ``None``/skipped command is
    treated as inconclusive infra, never as a pass. ``asserts_covered_behavior_change`` is the
    caller's heightened-scrutiny signal: when set, a genuine failure routes to a human review
    (``declined_needs_human``) instead of auto-advancing to ``reproduced``.

    The throwaway worktree is always cleaned up. The operator's own checkout is never touched.
    """
    # Only a `discovered` candidate is eligible — this gate IS the discovered → {reproduced,
    # dismissed, declined} step. Anything else is a caller error we decline to act on rather than
    # force an illegal transition through the guard.
    if candidate.status is not BugStatus.discovered:
        return ReproResult(outcome=ReproOutcome.ineligible, candidate=candidate)

    # bf-repro-pathguard, defense in depth: the path was vetted when the ReproTest was parsed, but
    # this gate is the WRITER, so it re-validates before touching disk. A rejected path short-
    # circuits BEFORE any worktree is created or any byte is written.
    safe_path = repro_pathguard(repro.path)
    if safe_path is None:
        await _log(bus, project.id, candidate, "pathguard", "repro test path rejected by pathguard")
        return ReproResult(outcome=ReproOutcome.rejected, candidate=candidate)

    base_branch = config.execution.target_branch or project.default_branch
    slug = f"repro-{candidate.id}"
    branch = f"{config.execution.branch_prefix}{slug}"

    try:
        worktree = await wm.create(slug, base_branch, branch)
    except WorktreeError as exc:
        # Could not even stand up the throwaway checkout — infra noise, not evidence either way.
        await _log(bus, project.id, candidate, "worktree", f"repro worktree setup failed: {exc}")
        return ReproResult(outcome=ReproOutcome.infra, candidate=candidate)

    try:
        if not _write_repro(worktree, safe_path, repro.body):
            # The guarded path still resolved outside the worktree (belt-and-suspenders) — refuse
            # rather than write through it.
            await _log(
                bus, project.id, candidate, "write", "repro test path escaped the worktree"
            )
            return ReproResult(outcome=ReproOutcome.rejected, candidate=candidate)
        gate = await _run_with_infra_retry(worktree, test_command, timeout_seconds)
    finally:
        # The worktree is throwaway: always remove it (and its branch), success or failure, so a
        # proven/refuted candidate never strands a dangling checkout.
        await wm.cleanup(slug, branch)

    return await _classify(
        db,
        bus,
        project,
        candidate,
        repro,
        safe_path,
        gate,
        asserts_covered_behavior_change=asserts_covered_behavior_change,
    )


# --- module helpers ---


async def _classify(
    db: Database,
    bus: EventBus,
    project: Project,
    candidate: BugCandidate,
    repro: ReproTest,
    safe_path: str,
    gate: GateResult,
    *,
    asserts_covered_behavior_change: bool,
) -> ReproResult:
    """Map a final :class:`GateResult` to a status transition + event (or to a no-op)."""
    # A skipped run (no command) or an infra outcome is NOT proof in either direction — checked
    # BEFORE `passed`, so an uncommanded/skipped gate can never be mistaken for "passes on the
    # unfixed code" and wrongly dismiss a real bug.
    if gate.skipped or gate.outcome in _INFRA_OUTCOMES:
        await _log(
            bus,
            project.id,
            candidate,
            "gate",
            f"repro gate inconclusive (outcome={gate.outcome}, exit={gate.exit_code})",
        )
        return ReproResult(outcome=ReproOutcome.infra, candidate=candidate)

    if gate.outcome == "failed":
        return await _reproduced_or_declined(
            db, bus, project, candidate, repro, safe_path,
            asserts_covered_behavior_change=asserts_covered_behavior_change,
        )

    # outcome == "passed": the correct behaviour already holds on the unfixed code → false positive.
    reason = "repro test passed on the unfixed checkpoint — claim is a false positive"
    dismissed = await repo.transition_bug_status(
        db, candidate.id, BugStatus.dismissed_false_positive, decline_reason=reason
    )
    await bus.emit(
        type=EventType.bug_dismissed,
        project_id=project.id,
        payload=_event_payload(dismissed, reason=reason),
    )
    return ReproResult(outcome=ReproOutcome.dismissed, candidate=dismissed)


async def _reproduced_or_declined(
    db: Database,
    bus: EventBus,
    project: Project,
    candidate: BugCandidate,
    repro: ReproTest,
    safe_path: str,
    *,
    asserts_covered_behavior_change: bool,
) -> ReproResult:
    """The genuine-failure branch: pin + reproduce, or hold for human review."""
    if asserts_covered_behavior_change:
        # A genuine failure, but the claim asserts a behaviour change in already-covered code with
        # no contradicting existing test: it may be a disagreement with intended behaviour rather
        # than a defect. Route to a human instead of auto-advancing toward a fix.
        reason = (
            "repro failed on the unfixed code but asserts a behaviour change in already-covered "
            "code with no contradicting existing test — needs human review"
        )
        declined = await repo.transition_bug_status(
            db, candidate.id, BugStatus.declined_needs_human, decline_reason=reason
        )
        await bus.emit(
            type=EventType.bug_declined,
            project_id=project.id,
            payload=_event_payload(declined, reason=reason),
        )
        return ReproResult(outcome=ReproOutcome.declined, candidate=declined)

    # Pin the proven test (bf-integrity-repro-pin) THEN advance the status. The artifact write and
    # the guarded transition are deliberately separate calls — see set_repro_artifacts — so each is
    # independently auditable. The hash is taken over the exact body that was written and proven,
    # so a later as-merged re-verification can detect any tampering with the pinned test.
    body = repro.body
    await repo.set_repro_artifacts(
        db, candidate.id, path=safe_path, body=body, hash=_repro_hash(body)
    )
    reproduced = await repo.transition_bug_status(db, candidate.id, BugStatus.reproduced)
    await bus.emit(
        type=EventType.bug_reproduced,
        project_id=project.id,
        payload=_event_payload(reproduced),
    )
    return ReproResult(outcome=ReproOutcome.reproduced, candidate=reproduced)


async def _run_with_infra_retry(
    worktree: Path, command: str | None, timeout_seconds: int
) -> GateResult:
    """Run the repro gate, retrying once on an infra outcome (ENG-7 mirror).

    A single transient toolchain hiccup (a timeout, a momentarily missing command) must not be
    read as "couldn't reproduce", so an infra outcome buys exactly one retry before the caller
    treats it as inconclusive.
    """
    gate = await run_tests(worktree, command, timeout_seconds=timeout_seconds)
    if gate.outcome in _INFRA_OUTCOMES:
        gate = await run_tests(worktree, command, timeout_seconds=timeout_seconds)
    return gate


def _write_repro(worktree: Path, safe_path: str, body: str) -> bool:
    """Write the test body into the worktree at ``safe_path``, or refuse if it escapes.

    ``safe_path`` already passed the bf-repro-pathguard, but the resolved target is re-checked
    against the worktree root (mirroring :func:`check_grounding`) before any write — a path that
    resolves outside the sandbox is refused rather than written through.
    """
    target = (worktree / safe_path).resolve()
    if not target.is_relative_to(worktree.resolve()):
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return True


def _repro_hash(body: str) -> str:
    """SHA-256 of the proven test body — the *bf-integrity-repro-pin*."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _event_payload(candidate: BugCandidate, *, reason: str | None = None) -> dict[str, Any]:
    """Local-only event payload: candidate identity, status, and the pinned test path/hash.

    Deliberately carries the test PATH and HASH but never the body — see this module's SECRETS
    HYGIENE note. ``repro_test_*`` are ``None`` for the dismissed/declined paths (no pin stored).
    """
    payload: dict[str, Any] = {
        "candidate_id": candidate.id,
        "fingerprint": candidate.fingerprint,
        "region": candidate.region,
        "file": candidate.file,
        "symbol": candidate.symbol,
        "status": candidate.status.value,
        "repro_test_path": candidate.repro_test_path,
        "repro_test_hash": candidate.repro_test_hash,
    }
    if reason is not None:
        payload["reason"] = reason
    return payload


async def _log(
    bus: EventBus, project_id: str, candidate: BugCandidate, stage: str, message: str
) -> None:
    """Emit a local ``log`` event for an inconclusive/refused gate (no bug-state change)."""
    await bus.emit(
        type=EventType.log,
        project_id=project_id,
        payload={"stage": f"repro/{stage}", "message": message, "candidate_id": candidate.id},
    )
