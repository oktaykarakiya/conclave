"""Unit tests for the Bug-Fixer region scheduler (``select_hunt_region``).

The scheduler layers ignore-filtering, flat-fallback seeding, and claim-bookkeeping over the
raw coverage picker. These tests pin the ordering contract on a seeded coverage set: never-
examined regions lead, priority breaks ties, ignored regions are excluded, and a claimed region
ages out so the sweep advances.
"""

from __future__ import annotations

from conclave.config import PlanningSettings
from conclave.db import Database
from conclave.db import repositories as repo
from conclave.engine import select_hunt_region

# --- ordering: never-examined first, priority tiebreak ----------------------


async def test_selects_never_examined_first_then_priority_tiebreak(db: Database) -> None:
    """NULL last_examined_at leads; among equals higher priority wins; claims age regions out."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    # One stale-but-examined region, two never-examined at different priorities.
    await repo.upsert_coverage_region(
        db, project_id=p.id, region="examined-old", priority=9, last_examined_at="2026-01-01"
    )
    await repo.upsert_coverage_region(db, project_id=p.id, region="fresh-lo", priority=1)
    await repo.upsert_coverage_region(db, project_id=p.id, region="fresh-hi", priority=5)

    planning = PlanningSettings(priorities=[], ignore_patterns=[])

    # Never-examined sorts first; the higher-priority of the two breaks the tie.
    first = await select_hunt_region(db, p.id, layout_dirs=[], planning=planning)
    assert first is not None and first.region == "fresh-hi"
    # The claim stamps the bookkeeping so the next call advances rather than re-picking.
    assert first.examined_count == 1
    assert first.last_examined_at is not None

    # fresh-hi is now examined ("now"); the other never-examined region is next.
    second = await select_hunt_region(db, p.id, layout_dirs=[], planning=planning)
    assert second is not None and second.region == "fresh-lo"

    # Both fresh regions now carry a "now" stamp that sorts after the 2026-01-01 region.
    third = await select_hunt_region(db, p.id, layout_dirs=[], planning=planning)
    assert third is not None and third.region == "examined-old"


# --- ignore_patterns exclusion ----------------------------------------------


async def test_ignore_patterns_excluded_even_when_top_ranked(db: Database) -> None:
    """An ignored region is skipped at selection even when its priority would otherwise win."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    # The top-priority, never-examined row is under an ignored dir — it must never be picked.
    await repo.upsert_coverage_region(db, project_id=p.id, region="node_modules/pkg", priority=99)
    await repo.upsert_coverage_region(db, project_id=p.id, region="src/app", priority=0)

    planning = PlanningSettings(priorities=[], ignore_patterns=["node_modules", ".venv"])

    picked = await select_hunt_region(db, p.id, layout_dirs=[], planning=planning)
    assert picked is not None and picked.region == "src/app"

    # The ignored region is never claimed — its examination bookkeeping stays untouched.
    rows = {r.region: r for r in await repo.list_coverage_regions(db, p.id)}
    assert rows["node_modules/pkg"].examined_count == 0
    assert rows["node_modules/pkg"].last_examined_at is None


async def test_glob_pattern_excludes_matching_file_region(db: Database) -> None:
    """A glob like ``*.min.js`` excludes a region whose basename matches it."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    await repo.upsert_coverage_region(db, project_id=p.id, region="src/vendor/app.min.js")
    await repo.upsert_coverage_region(db, project_id=p.id, region="src/app.py")

    planning = PlanningSettings(priorities=[], ignore_patterns=["*.min.js"])

    picked = await select_hunt_region(db, p.id, layout_dirs=[], planning=planning)
    assert picked is not None and picked.region == "src/app.py"


async def test_returns_none_when_every_region_is_ignored(db: Database) -> None:
    """No survivor after ignore-filtering yields ``None`` rather than an ignored pick."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    await repo.upsert_coverage_region(db, project_id=p.id, region="node_modules/a")
    await repo.upsert_coverage_region(db, project_id=p.id, region=".venv/lib")

    planning = PlanningSettings(priorities=[], ignore_patterns=["node_modules", ".venv"])
    assert await select_hunt_region(db, p.id, layout_dirs=[], planning=planning) is None


# --- flat-fallback seeding from the repo-knowledge layout -------------------


async def test_seeds_flat_fallback_from_layout_skipping_ignored(db: Database) -> None:
    """With no coverage yet, layout dirs are seeded (ignored skipped); priorities rank seeds."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    planning = PlanningSettings(priorities=["security", "bugs"], ignore_patterns=["node_modules"])
    layout = ["src", "node_modules", "security"]

    # "security" matches priorities[0] → highest seed priority; "node_modules" is never seeded.
    first = await select_hunt_region(db, p.id, layout_dirs=layout, planning=planning)
    assert first is not None and first.region == "security"

    seeded = {r.region for r in await repo.list_coverage_regions(db, p.id)}
    assert seeded == {"src", "security"}  # node_modules excluded at seed time

    # The remaining never-examined region is taken next.
    second = await select_hunt_region(db, p.id, layout_dirs=layout, planning=planning)
    assert second is not None and second.region == "src"


async def test_seeding_is_insert_only_and_preserves_existing(db: Database) -> None:
    """Re-seeding never clobbers a region's real-coverage priority or examination history."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    # Simulate bf-coverage-ingest having recorded a measured priority + a prior examination.
    await repo.upsert_coverage_region(
        db, project_id=p.id, region="src", priority=42, examined_count=3,
        last_examined_at="2026-01-01",
    )

    planning = PlanningSettings(priorities=["security"], ignore_patterns=[])
    # "src" already exists, so seeding must leave its fields intact (no reset to the flat seed).
    picked = await select_hunt_region(db, p.id, layout_dirs=["src"], planning=planning)
    assert picked is not None and picked.region == "src"

    row = {r.region: r for r in await repo.list_coverage_regions(db, p.id)}["src"]
    assert row.priority == 42  # not clobbered to the flat seed (0)
    assert row.examined_count == 4  # bumped by the claim (3 → 4), not reset


async def test_returns_none_when_no_regions_and_no_layout(db: Database) -> None:
    """An empty project with nothing to seed has no region to hunt."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    assert await select_hunt_region(db, p.id, layout_dirs=[], planning=PlanningSettings()) is None
