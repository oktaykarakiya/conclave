"""Bug-Fixer region scheduler — picks the next repo region for the hunter to sweep.

The hunter examines one region per turn; this helper decides which. It layers three concerns
that the raw SQL picker (:func:`conclave.db.repositories.select_next_region`) cannot express on
its own:

* **Flat-fallback seeding** — before any real coverage exists, the repo-knowledge layout dirs
  are materialized as coverage rows so the sweep has ground to stand on. Seeding is insert-only,
  so it never clobbers the real-coverage priorities that ``bf-coverage-ingest`` writes once it
  has run.
* **Ignore filtering** — ``PlanningSettings.ignore_patterns`` (node_modules, .venv, …) are
  dropped at selection time, so even a region a coverage ingest recorded by mistake is skipped.
* **Ranking** — seed priorities come from ``PlanningSettings.priorities``, so the sweep scheduler
  and the planner keep sharing one ranking.

Selection order is least-recently-examined first, highest priority breaking ties (a NULL
``last_examined_at`` — never examined — sorts first). After a region is chosen it is *claimed*:
``examined_count`` is bumped and ``last_examined_at`` stamped, so the next sweep advances rather
than re-picking the same region.
"""

from __future__ import annotations

from collections.abc import Sequence
from fnmatch import fnmatchcase

from ..config import PlanningSettings
from ..db import CoverageRegion, Database
from ..db import repositories as repo
from ..util import now_iso


async def select_hunt_region(
    db: Database,
    project_id: str,
    *,
    layout_dirs: Sequence[str],
    planning: PlanningSettings,
) -> CoverageRegion | None:
    """Pick and claim the next region for the hunter, or ``None`` when none remain.

    ``layout_dirs`` are the repo-knowledge layout directories (``RepoKnowledge.layout['dirs']``)
    used to seed the flat fallback. The returned :class:`CoverageRegion` is the claimed row, with
    its bumped ``examined_count`` and freshly stamped ``last_examined_at`` already persisted.
    """
    await _seed_missing_regions(db, project_id, layout_dirs, planning)

    # select_next_region applies the canonical order; only when its top pick is an ignored region
    # (one a coverage ingest may have recorded directly) do we walk the full list for the first
    # survivor — the two agree whenever the top pick is not ignored.
    candidate = await repo.select_next_region(db, project_id)
    if candidate is None:
        return None
    if _is_ignored(candidate.region, planning.ignore_patterns):
        candidate = next(
            (
                region
                for region in await repo.list_coverage_regions(db, project_id)
                if not _is_ignored(region.region, planning.ignore_patterns)
            ),
            None,
        )
        if candidate is None:
            return None

    return await repo.upsert_coverage_region(
        db,
        project_id=project_id,
        region=candidate.region,
        examined_count=candidate.examined_count + 1,
        last_examined_at=now_iso(),
    )


# --- module helpers ---


async def _seed_missing_regions(
    db: Database,
    project_id: str,
    layout_dirs: Sequence[str],
    planning: PlanningSettings,
) -> None:
    """Insert a coverage row for each non-ignored layout dir not already tracked.

    Insert-only: a region already in the table keeps its (possibly real-coverage) priority and
    examination history untouched, so re-running the scheduler each sweep never resets progress.
    """
    existing = {region.region for region in await repo.list_coverage_regions(db, project_id)}
    for region in layout_dirs:
        if region in existing or _is_ignored(region, planning.ignore_patterns):
            continue
        await repo.upsert_coverage_region(
            db,
            project_id=project_id,
            region=region,
            priority=_seed_priority(region, planning.priorities),
        )


def _is_ignored(region: str, patterns: Sequence[str]) -> bool:
    """True when the region — or any of its path segments — matches an ignore glob.

    Segment-wise matching lets a bare name like ``node_modules`` exclude ``node_modules/pkg`` and
    a glob like ``*.min.js`` exclude ``src/vendor/app.min.js``. ``fnmatchcase`` keeps the decision
    platform-independent (so the sweep is deterministic in tests) rather than folding case through
    the host filesystem's rules the way ``fnmatch`` does.
    """
    segments = [seg for seg in region.split("/") if seg]
    return any(
        fnmatchcase(region, pattern) or any(fnmatchcase(seg, pattern) for seg in segments)
        for pattern in patterns
    )


def _seed_priority(region: str, priorities: Sequence[str]) -> int:
    """Seed priority for a fresh region: share the planner's category ranking.

    A region with a path segment literally named after a priority category (earlier in the list
    ⇒ higher) is swept sooner; with no match the region lands on the flat floor (0) — the fallback
    the scheduler leans on until a real-coverage ingest writes a measured priority.
    """
    segments = {seg.lower() for seg in region.split("/") if seg}
    for rank, category in enumerate(priorities):
        if category.lower() in segments:
            return len(priorities) - rank
    return 0
