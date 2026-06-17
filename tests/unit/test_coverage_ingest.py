"""Unit tests for Bug-Fixer coverage ingestion (``ingest_coverage``).

These pin the two halves of the contract the region scheduler leans on: a real ``coverage.json``
is parsed into per-region priorities where a thinly-covered region outranks a well-covered one,
and an absent/corrupt report is a graceful no-op so selection degrades to least-recently-examined
rather than erroring. A final case proves a re-ingest re-ranks priority without rewinding the
scheduler's examination bookkeeping.
"""

from __future__ import annotations

import json
from pathlib import Path

from conclave.config import PlanningSettings
from conclave.db import Database
from conclave.db import repositories as repo
from conclave.engine import ingest_coverage, select_hunt_region


def _write_report(repo_path: Path, files: dict[str, tuple[int, int]]) -> None:
    """Write a minimal ``coverage.json`` — ``{file_path: (covered_lines, num_statements)}``.

    Only the two counts the parser actually reads are emitted, mirroring the real ``coverage json``
    schema (a ``files`` map of per-file ``summary`` blocks) without its incidental fields.
    """
    report = {
        "files": {
            path: {"summary": {"covered_lines": cov, "num_statements": stmts}}
            for path, (cov, stmts) in files.items()
        }
    }
    (repo_path / "coverage.json").write_text(json.dumps(report), encoding="utf-8")


# --- parsing: low coverage -> higher hunt priority --------------------------


async def test_low_coverage_gets_higher_priority(db: Database, tmp_path: Path) -> None:
    """A sample report becomes region rows; the thinly-covered region outranks the well-covered."""
    p = await repo.create_project(db, name="demo", path=str(tmp_path), default_branch="main")
    # src/lo aggregates to 2/10 (20%) across two files; src/hi is 9/10 (90%).
    _write_report(
        tmp_path,
        {
            "src/lo/a.py": (1, 5),
            "src/lo/b.py": (1, 5),
            "src/hi/c.py": (9, 10),
        },
    )

    written = await ingest_coverage(db, p.id, repo_path=tmp_path)
    assert written == 2

    rows = {r.region: r for r in await repo.list_coverage_regions(db, p.id)}
    assert set(rows) == {"src/lo", "src/hi"}
    # Coverage is aggregated per directory, then ranked by inverse coverage.
    assert rows["src/lo"].priority == 80  # round(100 - 20)
    assert rows["src/hi"].priority == 10  # round(100 - 90)
    assert rows["src/lo"].priority > rows["src/hi"].priority  # low coverage ⇒ hunted first
    # Ingest writes only priority; the scheduler's bookkeeping starts clean.
    assert rows["src/lo"].examined_count == 0
    assert rows["src/lo"].last_examined_at is None

    # End-to-end: the scheduler now picks the low-coverage region first (both never-examined).
    planning = PlanningSettings(priorities=[], ignore_patterns=[])
    picked = await select_hunt_region(db, p.id, layout_dirs=[], planning=planning)
    assert picked is not None and picked.region == "src/lo"


async def test_empty_region_scores_zero_priority(db: Database, tmp_path: Path) -> None:
    """A region with no statements (all-empty files) scores priority 0, below any covered region."""
    p = await repo.create_project(db, name="demo", path=str(tmp_path), default_branch="main")
    _write_report(tmp_path, {"src/empty/__init__.py": (0, 0), "src/real/a.py": (1, 10)})

    await ingest_coverage(db, p.id, repo_path=tmp_path)

    rows = {r.region: r for r in await repo.list_coverage_regions(db, p.id)}
    assert rows["src/empty"].priority == 0  # nothing to cover ⇒ nothing to hunt
    assert rows["src/real"].priority == 90  # round(100 - 10)


# --- graceful no-coverage fallback ------------------------------------------


async def test_no_report_is_graceful_noop(db: Database, tmp_path: Path) -> None:
    """No coverage.json → nothing written; selection degrades to flat seeding (least-recently)."""
    p = await repo.create_project(db, name="demo", path=str(tmp_path), default_branch="main")

    written = await ingest_coverage(db, p.id, repo_path=tmp_path)
    assert written == 0
    assert await repo.list_coverage_regions(db, p.id) == []

    # Fallback: with no ingested priorities, the scheduler's flat seeding (priority 0) takes over
    # and selection is driven purely by least-recently-examined ordering.
    planning = PlanningSettings(priorities=[], ignore_patterns=[])
    picked = await select_hunt_region(db, p.id, layout_dirs=["src"], planning=planning)
    assert picked is not None and picked.region == "src"
    assert picked.priority == 0  # flat seed, not a coverage-derived rank


async def test_corrupt_report_is_graceful_noop(db: Database, tmp_path: Path) -> None:
    """A malformed coverage.json degrades to no priorities rather than raising into the sweep."""
    p = await repo.create_project(db, name="demo", path=str(tmp_path), default_branch="main")
    (tmp_path / "coverage.json").write_text("{not valid json", encoding="utf-8")

    assert await ingest_coverage(db, p.id, repo_path=tmp_path) == 0
    assert await repo.list_coverage_regions(db, p.id) == []


async def test_wrong_shape_report_is_graceful_noop(db: Database, tmp_path: Path) -> None:
    """Valid JSON of the wrong shape (no ``files`` map) is ignored, not crashed on."""
    p = await repo.create_project(db, name="demo", path=str(tmp_path), default_branch="main")
    (tmp_path / "coverage.json").write_text(json.dumps({"totals": {}}), encoding="utf-8")

    assert await ingest_coverage(db, p.id, repo_path=tmp_path) == 0
    assert await repo.list_coverage_regions(db, p.id) == []


# --- re-ingest re-ranks without rewinding the sweep -------------------------


async def test_reingest_reranks_without_rewinding_examination(
    db: Database, tmp_path: Path
) -> None:
    """A re-ingest overwrites priority but leaves examined_count / last_examined_at intact."""
    p = await repo.create_project(db, name="demo", path=str(tmp_path), default_branch="main")
    # The region has already been swept (examination bookkeeping set by the scheduler).
    await repo.upsert_coverage_region(
        db,
        project_id=p.id,
        region="src/lo",
        priority=10,
        examined_count=3,
        last_examined_at="2026-01-01",
    )
    _write_report(tmp_path, {"src/lo/a.py": (1, 10)})  # 10% covered → priority 90

    await ingest_coverage(db, p.id, repo_path=tmp_path)

    row = {r.region: r for r in await repo.list_coverage_regions(db, p.id)}["src/lo"]
    assert row.priority == 90  # re-ranked from the fresh report
    assert row.examined_count == 3  # not rewound
    assert row.last_examined_at == "2026-01-01"
