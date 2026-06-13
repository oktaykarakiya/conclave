"""Unit tests for quarantine integrity (expiry enforcement)."""

from __future__ import annotations

from conclave.db import Database
from conclave.db import repositories as repo
from conclave.verification import quarantine_integrity


async def test_integrity_flags_expired(db: Database) -> None:
    project = await repo.create_project(db, name="t", path="/tmp/t", default_branch="main")
    await repo.add_quarantine(
        db, project_id=project.id, pattern="tests/a.test.js", reason="flaky", until="2999-01-01"
    )
    await repo.add_quarantine(
        db, project_id=project.id, pattern="tests/b.test.js", reason="env", until="2000-01-01"
    )

    integrity = await quarantine_integrity(db, project.id, today="2026-06-13")
    assert integrity["total"] == 2
    assert integrity["active"] == 1
    assert integrity["expired"] == 1
    assert integrity["expired_patterns"] == ["tests/b.test.js"]
    assert integrity["healthy"] is False


async def test_integrity_healthy_when_all_active(db: Database) -> None:
    project = await repo.create_project(db, name="t", path="/tmp/t", default_branch="main")
    await repo.add_quarantine(
        db, project_id=project.id, pattern="x", reason="r", until="2999-01-01"
    )
    integrity = await quarantine_integrity(db, project.id, today="2026-06-13")
    assert integrity["healthy"] is True
    assert integrity["expired"] == 0
