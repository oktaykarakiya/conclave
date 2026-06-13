"""Unit tests for repo intelligence + bootstrap seeding."""

from __future__ import annotations

import json
from pathlib import Path

from conclave.bootstrap import seed_global_defaults
from conclave.db import Database
from conclave.db import repositories as repo
from conclave.events import EventBus
from conclave.repo_intel import analyze_repo, manifest_fingerprint, onboard, render_preamble


def test_analyze_node_repo(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "type": "module",
                "scripts": {"test": "jest", "build": "tsc", "lint": "eslint ."},
                "dependencies": {"react": "^18"},
                "devDependencies": {"jest": "^29"},
            }
        )
    )
    (tmp_path / "tsconfig.json").write_text("{}")
    (tmp_path / "src").mkdir()
    knowledge = analyze_repo(tmp_path)
    assert "javascript" in knowledge.languages
    assert "typescript" in knowledge.languages
    assert knowledge.commands["test"] == "npm test"
    assert knowledge.commands["build"] == "npm run build"
    assert "react" in knowledge.frameworks
    assert "jest" in knowledge.frameworks
    assert any("ESM" in c for c in knowledge.conventions)
    assert "src" in knowledge.layout.get("dirs", [])


def test_analyze_rust_repo(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname = 'x'\n")
    knowledge = analyze_repo(tmp_path)
    assert "rust" in knowledge.languages
    assert knowledge.commands["test"] == "cargo test"


def test_fingerprint_changes_with_manifest(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"a": 1}')
    first = manifest_fingerprint(tmp_path)
    (tmp_path / "package.json").write_text('{"a": 2}')
    assert first != manifest_fingerprint(tmp_path)


async def test_onboard_persists_and_skips_when_unchanged(db: Database, tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    project = await repo.create_project(db, name="t", path=str(tmp_path), default_branch="main")
    bus = EventBus(db)

    row1 = await onboard(db, bus, project)
    assert row1.version == 1
    assert "python" in row1.knowledge["languages"]

    project = await repo.get_project(db, project.id)  # type: ignore[assignment]
    assert project is not None
    row2 = await onboard(db, bus, project)
    assert row2.version == 1  # unchanged manifest => no new version

    row3 = await onboard(db, bus, project, force=True)
    assert row3.version == 2


def test_render_preamble() -> None:
    preamble = render_preamble(
        {
            "architecture_summary": "A Python service.",
            "languages": ["python"],
            "commands": {"test": "pytest"},
            "conventions": ["use type hints"],
        }
    )
    assert "A Python service." in preamble
    assert "pytest" in preamble
    assert "use type hints" in preamble


async def test_seed_global_defaults_idempotent_and_preserves_edits(db: Database) -> None:
    await seed_global_defaults(db)
    assert await repo.get_engine_profile(db, "system-default") is not None
    assert await repo.get_agent(db, "developer") is not None
    assert await repo.get_agent(db, "tester") is not None

    # an operator edit must survive re-seeding
    await repo.upsert_agent(db, name="developer", role="developer", persona_md="EDITED PERSONA")
    await seed_global_defaults(db)
    dev = await repo.get_agent(db, "developer")
    assert dev is not None and dev.persona_md == "EDITED PERSONA"
