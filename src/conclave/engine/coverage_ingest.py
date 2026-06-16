"""Bug-Fixer coverage ingestion — turn a target repo's coverage report into hunt priorities.

This is the routine the region scheduler's docstring anticipates as ``bf-coverage-ingest``: it is
what makes the sweep genuinely *coverage-aware* instead of leaning forever on the flat-priority
seeding. It reads the ``coverage.json`` that ``coverage json`` / ``pytest --cov-report=json``
leaves at the repo root, aggregates coverage per directory, and upserts one
:func:`~conclave.db.repositories.upsert_coverage_region` row per region with a priority derived
from how *thinly* that region is covered — a less-exercised region has more unobserved behavior
for a latent bug to hide in, so the hunter sweeps it first.

GRACEFUL FALLBACK: when no report is present (or it is unreadable / not valid JSON / the wrong
shape) nothing is written and the scheduler keeps its flat seeding, so selection degrades to
least-recently-examined rather than erroring the sweep. The parsing is therefore pure and
defensive — every level of the JSON is type-checked, matching the row-model / knowledge-blob
style elsewhere — and a malformed report collapses to "no priorities" instead of raising.

Only ``priority`` is ever written here. ``examined_count`` and ``last_examined_at`` are the
scheduler's bookkeeping; leaving them untouched (the upsert overwrites only the fields it is
handed) lets a re-ingest re-rank regions from a fresh report without rewinding how far the sweep
has already progressed. Reading a local JSON file invokes no engine CLI, so ingestion stays
deterministic and LLM-free for tests.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

from ..db import Database
from ..db import repositories as repo

# The canonical artifact ``coverage json`` (and ``pytest --cov-report=json``) writes to the CWD,
# which for a green-gate / CI run is the repo root we are handed.
_REPORT_NAME = "coverage.json"


async def ingest_coverage(db: Database, project_id: str, *, repo_path: Path) -> int:
    """Parse ``<repo_path>/coverage.json`` and upsert one hunt priority per region.

    Returns the number of regions written. A missing or corrupt report writes nothing and returns
    ``0`` — the graceful fallback that leaves selection on the scheduler's flat-priority seeding
    (least-recently-examined ordering).
    """
    priorities = _region_priorities(_load_report(repo_path))
    for region, priority in priorities.items():
        # Priority-only: never pass examined_count / last_examined_at, so the upsert preserves the
        # scheduler's bookkeeping and a re-ingest re-ranks without rewinding the sweep's progress.
        await repo.upsert_coverage_region(
            db, project_id=project_id, region=region, priority=priority
        )
    return len(priorities)


# --- module helpers ---


def _load_report(repo_path: Path) -> Any:
    """The decoded ``coverage.json``, or ``None`` when absent / unreadable / not valid JSON."""
    try:
        raw = (repo_path / _REPORT_NAME).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


def _region_priorities(report: Any) -> dict[str, int]:
    """Map each region (a covered file's parent dir) to a priority from its aggregate coverage.

    Coverage is summed over every file directly under a directory *before* ranking, so a region's
    priority reflects its whole body of code rather than any single file. Every level of the
    (possibly malformed) report is type-checked: a file whose entry is the wrong shape, or whose
    counts are missing/non-numeric, is skipped rather than aborting the whole ingest.
    """
    files = report.get("files") if isinstance(report, dict) else None
    if not isinstance(files, dict):
        return {}

    covered: dict[str, int] = {}
    statements: dict[str, int] = {}
    for path, entry in files.items():
        if not isinstance(path, str) or not isinstance(entry, dict):
            continue
        summary = entry.get("summary")
        if not isinstance(summary, dict):
            continue
        n_statements = _non_negative_int(summary.get("num_statements"))
        n_covered = _non_negative_int(summary.get("covered_lines"))
        if n_statements is None or n_covered is None:
            continue
        region = _region_of(path)
        statements[region] = statements.get(region, 0) + n_statements
        covered[region] = covered.get(region, 0) + n_covered

    return {
        region: _priority_from_coverage(covered[region], total)
        for region, total in statements.items()
    }


def _region_of(path: str) -> str:
    """The hunt region for a covered file: its immediate parent directory (POSIX, root → ``.``).

    The immediate parent keeps a region focused on one directory of source — the granularity the
    hunter sweeps — rather than collapsing a whole tree to its top dir. ``PurePosixPath`` folds a
    leading ``./`` and stays platform-independent so the mapping is deterministic in tests.
    """
    return str(PurePosixPath(path).parent)


def _priority_from_coverage(covered: int, statements: int) -> int:
    """Hunt priority from a region's coverage: ``round(100 - percent)``, clamped to ``>= 0``.

    Lower coverage ⇒ higher priority, on the same 0-100 scale the field already uses. A region
    with no statements (an all-empty ``__init__`` package) has nothing left uncovered, so it
    scores 100% → priority 0 and sinks below any region carrying real, unexercised code.
    """
    if statements <= 0:
        return 0
    percent = 100.0 * covered / statements
    priority = round(100.0 - percent)
    return priority if priority > 0 else 0


def _non_negative_int(value: Any) -> int | None:
    """A non-negative ``int`` from a JSON number, or ``None`` for a missing / invalid one.

    ``bool`` is rejected explicitly (it is an ``int`` subclass in Python) so a stray ``true`` in
    the report cannot masquerade as a line count.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None
