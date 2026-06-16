"""Bug-Fixer discovery routine — one hunter sweep, from region pick to ledger entry.

Ties together the pieces the prior bf tasks landed into a single read-only sweep: pick a
region (:func:`region_scheduler.select_hunt_region`), dispatch the ``hunter`` persona scoped
to it through the existing :class:`AgentRunner`/:class:`Provider` plumbing, parse its single
candidate (:func:`hunter.parse_hunter_candidate`), fingerprint it, dedupe into the ledger
(:func:`repositories.create_bug_candidate`), and announce a genuinely new find on the
:class:`EventBus` as :attr:`EventType.bug_discovered`.

No worker wiring yet: a caller drives one sweep by awaiting :func:`discover_bug`. It returns
the newly-created :class:`BugCandidate` when this turn discovered something new, or ``None``
when there was nothing to hunt (no region survived selection), the hunter produced no usable
candidate (none/hedged/multiple — the parser refuses to guess), or the find duplicated one
already in the ledger.

SECRETS HYGIENE: the hunter reads source, so a ``claim`` may quote a hardcoded secret. The
ledger row and the ``bug.discovered`` event are both LOCAL-ONLY sinks (the project's SQLite db
and the in-process event bus, which only persists to that same db). This routine deliberately
adds no other destination for that text — it is never logged, returned off-box, or forwarded to
a remote — so discovery opens no new exfiltration surface. The hunter's raw output likewise
never leaves memory: ``AgentRunner`` emits only metadata (ok/cost/model), not the response body.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Any

from ..config import ConclaveConfig
from ..db import BugCandidate, Database, Project
from ..db import repositories as repo
from ..events import EventBus, EventType
from ..providers import Provider
from ..repo_intel.knowledge import render_preamble
from .hunter import parse_hunter_candidate
from .region_scheduler import select_hunt_region
from .runner import AgentRunner


async def discover_bug(
    db: Database,
    bus: EventBus,
    provider: Provider,
    project: Project,
    config: ConclaveConfig,
    *,
    cancel_event: asyncio.Event | None = None,
) -> BugCandidate | None:
    """Run one hunter sweep; record and announce a genuinely new candidate, else ``None``.

    The repo-knowledge layout dirs (loaded from the project's current knowledge row) seed the
    region scheduler's flat fallback, and the rendered knowledge preamble gives the hunter the
    same project context every other dispatch gets. ``cancel_event`` is forwarded to the
    dispatch so a sweep is interruptible once a worker drives it.
    """
    knowledge_row = await repo.current_repo_knowledge(db, project.id)
    knowledge = knowledge_row.knowledge if knowledge_row else {}

    region = await select_hunt_region(
        db, project.id, layout_dirs=_layout_dirs(knowledge), planning=config.planning
    )
    if region is None:
        # Empty project, or every region ignored — nothing to sweep this turn.
        return None

    runner = AgentRunner(db, bus, provider, project.id, config)
    result = await runner.run(
        agent="hunter",
        prompt=_hunt_prompt(region.region),
        worktree=Path(project.path),
        repo_knowledge=render_preamble(knowledge),
        cancel_event=cancel_event,
    )
    if not result.ok or not result.text:
        return None

    candidate = parse_hunter_candidate(result.text)
    if candidate is None:
        # The hunter broke its one-falsifiable-candidate contract; the parser refuses to guess.
        return None

    fingerprint = _fingerprint(candidate.file, candidate.symbol, candidate.claim)

    # Event gate vs. structural dedupe. The pre-check decides whether this find is NEW (and so
    # worth a bug.discovered); create_bug_candidate stays the sole authority that guarantees one
    # row per (project, fingerprint). On a duplicate we still call create_bug_candidate — it is a
    # true no-op that returns the original row untouched — but we stay silent on the bus so a
    # re-sweep of the same region never re-announces a known bug.
    already = await repo.get_bug_candidate_by_fingerprint(db, project.id, fingerprint)
    row = await repo.create_bug_candidate(
        db,
        project_id=project.id,
        fingerprint=fingerprint,
        claim=candidate.claim,
        file=candidate.file,
        symbol=candidate.symbol,
        region=region.region,
        severity=candidate.severity,
    )
    if already is not None:
        return None

    # LOCAL-ONLY sink: the payload (claim included) is persisted to this project's events table
    # and fanned out to in-process subscribers only — see this module's SECRETS HYGIENE note.
    await bus.emit(
        type=EventType.bug_discovered,
        project_id=project.id,
        agent="hunter",
        payload={
            "candidate_id": row.id,
            "fingerprint": row.fingerprint,
            "region": row.region,
            "file": row.file,
            "symbol": row.symbol,
            "severity": row.severity,
            "claim": row.claim,
            "status": row.status.value,
        },
    )
    return row


# --- module helpers ---


def _fingerprint(file: str | None, symbol: str | None, claim: str) -> str:
    """Stable identity for a candidate: file + symbol + normalized claim, SHA-256 hex.

    Only the ``claim`` is normalized (collapse whitespace, casefold) so cosmetic re-wordings of
    the same prose finding collapse to one fingerprint and a re-sweep dedupes rather than piling
    up near-identical rows. ``file``/``symbol`` are case-sensitive identifiers (POSIX paths,
    source symbols), so they are only trimmed, never casefolded. A NUL separator keeps the three
    fields unambiguous so distinct (file, symbol, claim) splits cannot alias to one basis string.
    """
    norm_claim = re.sub(r"\s+", " ", claim).strip().casefold()
    basis = "\x00".join([(file or "").strip(), (symbol or "").strip(), norm_claim])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def _layout_dirs(knowledge: dict[str, Any]) -> list[str]:
    """Extract ``layout.dirs`` from a (possibly malformed) knowledge dict, defensively.

    The knowledge blob is decoded from a JSON column that tolerates corruption, so every level is
    type-checked before use — a bad shape degrades to "no seed dirs" rather than raising into the
    sweep.
    """
    layout = knowledge.get("layout")
    if not isinstance(layout, dict):
        return []
    dirs = layout.get("dirs")
    if not isinstance(dirs, list):
        return []
    return [d for d in dirs if isinstance(d, str)]


def _hunt_prompt(region: str) -> str:
    """The region-scoping task body for the hunter; its persona carries the output contract."""
    return (
        f"REGION: {region}\n\n"
        f"Hunt for exactly one real, latent bug confined to this region. Scan only files under "
        f"`{region}` and ignore everything outside it. End your reply with the single JSON "
        f"candidate block your persona specifies — or no block at all if you cannot state a "
        f"falsifiable claim about wrong behavior."
    )
