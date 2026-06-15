"""Unit tests for quarantine integrity (expiry enforcement) and apply_quarantine."""

from __future__ import annotations

from conclave.db import Database
from conclave.db import repositories as repo
from conclave.engine.gate import apply_quarantine
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


# ---------------------------------------------------------------------------
# apply_quarantine — async tests for active-vs-expired filtering
# ---------------------------------------------------------------------------


async def test_apply_quarantine_active_injected(db: Database) -> None:
    """Active (non-expired) quarantine patterns are injected into the command."""
    project = await repo.create_project(db, name="t", path="/tmp/t", default_branch="main")
    await repo.add_quarantine(
        db, project_id=project.id, pattern="tests/a.py", reason="flaky", until="2999-01-01"
    )
    result = await apply_quarantine(db, project.id, "pytest")
    assert result is not None
    assert "--deselect " in result
    assert "tests/a.py" in result


async def test_apply_quarantine_expired_excluded(db: Database) -> None:
    """Expired quarantine patterns are NOT injected — integrity preserved."""
    project = await repo.create_project(db, name="t", path="/tmp/t", default_branch="main")
    await repo.add_quarantine(
        db, project_id=project.id, pattern="tests/old.py", reason="stale", until="2000-01-01"
    )
    result = await apply_quarantine(db, project.id, "pytest")
    # Expired pattern should not appear; command returned as-is (no --deselect)
    assert result is not None
    assert "--deselect" not in result


async def test_apply_quarantine_no_active_entries_unchanged(db: Database) -> None:
    """No quarantine entries at all → command returned unchanged."""
    project = await repo.create_project(db, name="t", path="/tmp/t", default_branch="main")
    result = await apply_quarantine(db, project.id, "jest --verbose")
    assert result == "jest --verbose"


async def test_apply_quarantine_none_command_unchanged(db: Database) -> None:
    """None command is returned as None (no test command configured)."""
    project = await repo.create_project(db, name="t", path="/tmp/t", default_branch="main")
    await repo.add_quarantine(
        db, project_id=project.id, pattern="tests/a.py", reason="flaky", until="2999-01-01"
    )
    result = await apply_quarantine(db, project.id, None)
    assert result is None


async def test_apply_quarantine_mixed_active_expired(db: Database) -> None:
    """Only active patterns are injected; expired ones are excluded."""
    project = await repo.create_project(db, name="t", path="/tmp/t", default_branch="main")
    await repo.add_quarantine(
        db, project_id=project.id, pattern="tests/active.py", reason="flaky", until="2999-01-01"
    )
    await repo.add_quarantine(
        db, project_id=project.id, pattern="tests/expired.py", reason="stale", until="2000-01-01"
    )
    result = await apply_quarantine(db, project.id, "pytest -q")
    assert result is not None
    assert "--deselect " in result
    assert "tests/active.py" in result
    assert "tests/expired.py" not in result
