"""Unit tests for the Bug-Fixer discovery routine (``discover_bug``).

One read-only hunter sweep: pick a region, dispatch the hunter, parse its single candidate,
fingerprint it, dedupe into the ledger, and announce a genuinely new find. These tests pin the
contract end-to-end with a deterministic, LLM-free Provider double: a fresh candidate is stored
``discovered`` and emits exactly one ``bug.discovered``; a repeat fingerprint (including a
cosmetic re-wording of the same claim) is a silent no-op; an empty project or an unparseable
hunter reply discovers nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

from conclave.config import ConclaveConfig
from conclave.db import BugStatus, Database, EventRow
from conclave.db import repositories as repo
from conclave.engine import discover_bug
from conclave.events import EventBus, EventType
from conclave.providers import AgentResult


def _candidate_block(
    *,
    file: str = "src/app.py",
    symbol: str = "f",
    claim: str = "f returns n+1 for empty input",
    severity: str = "high",
) -> str:
    """A well-formed single-candidate hunter reply (the JSON contract ``parse_hunter`` expects)."""
    body = json.dumps({"file": file, "symbol": symbol, "claim": claim, "severity": severity})
    return f"```json\n{body}\n```"


class _HunterProvider:
    """Provider double that returns a scripted hunter reply per dispatch.

    ``texts`` scripts successive replies (the last repeats once exhausted); ``calls`` records how
    many dispatches happened so a test can assert a no-region sweep never reaches the provider.
    """

    def __init__(self, *, text: str | None = None, texts: list[str] | None = None) -> None:
        if texts is not None:
            self._texts = list(texts)
        else:
            self._texts = [text if text is not None else _candidate_block()]
        self.calls = 0

    async def run_agent(
        self, *, profile, prompt, timeout_seconds, cwd=None, on_chunk=None, cancel_event=None
    ) -> AgentResult:
        text = self._texts[min(self.calls, len(self._texts) - 1)]
        self.calls += 1
        return AgentResult(ok=True, text=text, model_reported="fake", cost_usd=0.0)


def _discovered(events: list[EventRow]) -> list[EventRow]:
    return [e for e in events if e.type == EventType.bug_discovered]


# --- happy path: persist + announce -----------------------------------------


async def test_discovers_persists_and_emits(db: Database) -> None:
    """A fresh candidate lands as ``discovered`` and emits exactly one ``bug.discovered``."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    await repo.upsert_coverage_region(db, project_id=p.id, region="src/conclave")
    bus = EventBus(db)
    provider = _HunterProvider()

    found = await discover_bug(db, bus, provider, p, ConclaveConfig())

    assert found is not None
    assert found.status is BugStatus.discovered
    assert found.region == "src/conclave"  # the claimed sweep region rides along on the row
    assert found.file == "src/app.py"
    assert found.symbol == "f"
    assert found.claim == "f returns n+1 for empty input"

    # Exactly one ledger row, and it is the returned candidate.
    rows = await repo.list_bug_candidates(db, p.id)
    assert len(rows) == 1 and rows[0].id == found.id

    # Exactly one bug.discovered event, tagged to the hunter and carrying the candidate identity.
    events = _discovered(await repo.list_events(db, project_id=p.id))
    assert len(events) == 1
    assert events[0].agent == "hunter"
    assert events[0].payload["candidate_id"] == found.id
    assert events[0].payload["fingerprint"] == found.fingerprint
    assert events[0].payload["status"] == BugStatus.discovered.value
    # The claim round-trips into the LOCAL event payload — a permitted local-only sink.
    assert events[0].payload["claim"] == found.claim


# --- coverage-aware selection: the sweep hunts the thinnest-covered region --


async def test_sweep_consumes_coverage_priorities(db: Database, tmp_path: Path) -> None:
    """A sweep ingests the repo's coverage.json and hunts the thinnest-covered region first."""
    p = await repo.create_project(db, name="demo", path=str(tmp_path), default_branch="main")
    report = {
        "files": {
            "src/lo/a.py": {"summary": {"covered_lines": 1, "num_statements": 10}},  # 10%
            "src/hi/b.py": {"summary": {"covered_lines": 9, "num_statements": 10}},  # 90%
        }
    }
    (tmp_path / "coverage.json").write_text(json.dumps(report), encoding="utf-8")
    bus = EventBus(db)
    provider = _HunterProvider()

    found = await discover_bug(db, bus, provider, p, ConclaveConfig())

    # The low-coverage region outranks the high-coverage one and is the region actually swept —
    # proof the ingest is wired in, not dead code.
    assert found is not None and found.region == "src/lo"
    rows = {r.region: r for r in await repo.list_coverage_regions(db, p.id)}
    assert rows["src/lo"].priority == 90 and rows["src/hi"].priority == 10


# --- dedupe: a repeat fingerprint is a silent no-op -------------------------


async def test_duplicate_fingerprint_is_noop(db: Database) -> None:
    """A second sweep yielding the same fingerprint stores nothing new and stays silent."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    await repo.upsert_coverage_region(db, project_id=p.id, region="src/conclave")
    bus = EventBus(db)
    provider = _HunterProvider()  # identical candidate every dispatch → identical fingerprint

    first = await discover_bug(db, bus, provider, p, ConclaveConfig())
    assert first is not None

    second = await discover_bug(db, bus, provider, p, ConclaveConfig())
    assert second is None  # duplicate fingerprint → no-op

    # Still exactly one row and exactly one announcement, even though both sweeps dispatched.
    assert len(await repo.list_bug_candidates(db, p.id)) == 1
    assert len(_discovered(await repo.list_events(db, project_id=p.id))) == 1
    assert provider.calls == 2


async def test_claim_normalization_dedupes_cosmetic_rewordings(db: Database) -> None:
    """A claim differing only in whitespace/case collapses to the same fingerprint."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    await repo.upsert_coverage_region(db, project_id=p.id, region="src/a")
    await repo.upsert_coverage_region(db, project_id=p.id, region="src/b")
    bus = EventBus(db)
    provider = _HunterProvider(
        texts=[
            _candidate_block(claim="f returns n+1 for empty input"),
            _candidate_block(claim="F  returns   N+1 for empty INPUT"),
        ]
    )

    first = await discover_bug(db, bus, provider, p, ConclaveConfig())
    second = await discover_bug(db, bus, provider, p, ConclaveConfig())

    assert first is not None
    assert second is None  # same finding, only reworded → deduped
    assert len(await repo.list_bug_candidates(db, p.id)) == 1


async def test_distinct_fingerprints_each_discovered(db: Database) -> None:
    """Two genuinely different findings each persist and each announce."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    await repo.upsert_coverage_region(db, project_id=p.id, region="src/a")
    await repo.upsert_coverage_region(db, project_id=p.id, region="src/b")
    bus = EventBus(db)
    provider = _HunterProvider(
        texts=[
            _candidate_block(claim="f returns n+1 for empty input"),
            _candidate_block(claim="g drops the last element"),
        ]
    )

    first = await discover_bug(db, bus, provider, p, ConclaveConfig())
    second = await discover_bug(db, bus, provider, p, ConclaveConfig())

    assert first is not None and second is not None
    assert first.fingerprint != second.fingerprint
    assert len(await repo.list_bug_candidates(db, p.id)) == 2
    assert len(_discovered(await repo.list_events(db, project_id=p.id))) == 2


# --- nothing to discover -----------------------------------------------------


async def test_no_region_is_noop(db: Database) -> None:
    """An empty project with nothing to seed never dispatches the hunter and discovers nothing."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    bus = EventBus(db)
    provider = _HunterProvider()

    assert await discover_bug(db, bus, provider, p, ConclaveConfig()) is None
    assert provider.calls == 0  # no region ⇒ no hunter dispatch
    assert await repo.list_bug_candidates(db, p.id) == []
    assert _discovered(await repo.list_events(db, project_id=p.id)) == []


async def test_unparseable_output_is_noop(db: Database) -> None:
    """A hunter reply that breaks the one-candidate contract stores nothing and stays silent."""
    p = await repo.create_project(db, name="demo", path="/tmp/demo", default_branch="main")
    await repo.upsert_coverage_region(db, project_id=p.id, region="src/conclave")
    bus = EventBus(db)
    provider = _HunterProvider(text="I scanned the region but found nothing actionable.")

    assert await discover_bug(db, bus, provider, p, ConclaveConfig()) is None
    assert await repo.list_bug_candidates(db, p.id) == []
    assert _discovered(await repo.list_events(db, project_id=p.id)) == []
