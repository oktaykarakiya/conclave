"""Integration tests for layered config over REST, MCP and the v13 migration.

Global settings flow to every project at resolution time; a project stores only its delta.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fake_provider import FakeProvider
from httpx import ASGITransport

from conclave.config import (
    GLOBAL_DEFAULTS_PRESET,
    deep_merge,
    load_layered_config,
    load_project_config,
    resolve_agent,
    resolve_project_config,
    workflow_templates,
)
from conclave.db import Database, Project, StoreItemKind
from conclave.db import repositories as repo
from conclave.db.migrations import MIGRATIONS
from conclave.engine.orchestrator import _resolve_target_branch
from conclave.mcp.server import handle_mcp_message
from conclave.runtime import Daemon
from conclave.web import create_app

GLOBAL: dict[str, Any] = {
    "execution": {"review_rounds_max": 7, "baseline_test_command": "pytest -q"},
    "agent_defaults": {"model": "g/default"},
}


@pytest_asyncio.fixture
async def daemon(db: Database, tmp_path: Path) -> AsyncIterator[Daemon]:
    instance = Daemon(db, tmp_path / "home", FakeProvider(), workers_enabled=False)
    try:
        yield instance
    finally:
        await instance.shutdown()


@pytest_asyncio.fixture
async def client(daemon: Daemon) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(daemon, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _mcp(daemon: Daemon, name: str, arguments: dict[str, Any]) -> tuple[bool, Any]:
    response = await handle_mcp_message(
        daemon,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response is not None
    result = response["result"]
    text = result["content"][0]["text"]
    return (True, text) if result["isError"] else (False, json.loads(text))


async def _set_global(db: Database, payload: dict[str, Any]) -> None:
    await repo.upsert_store_item(
        db, kind=StoreItemKind.config_preset, name=GLOBAL_DEFAULTS_PRESET, payload=payload
    )


def _repo_dir(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    (path / ".git").mkdir(parents=True)
    return path


async def _project(
    db: Database,
    tmp_path: Path,
    name: str,
    config: dict[str, Any] | None = None,
    default_branch: str = "main",
) -> Project:
    # Seeds a stored layer directly (a legacy or hand-written row), bypassing minimisation.
    return await repo.create_project(
        db, name=name, path=str(tmp_path / name), default_branch=default_branch, config=config
    )


async def _stored(db: Database, project_id: str) -> dict[str, Any]:
    project = await repo.get_project(db, project_id)
    assert project is not None
    return project.config


async def _raw_rows(db: Database) -> dict[str, tuple[str, str]]:
    rows = await db.fetchall("SELECT id, default_branch, config_json FROM projects")
    return {row["id"]: (row["default_branch"], row["config_json"]) for row in rows}


async def _merge_branch(db: Database, project_id: str) -> str:
    """The branch the orchestrator merges the project's tasks into."""
    project = await repo.get_project(db, project_id)
    assert project is not None
    return _resolve_target_branch(project, await resolve_project_config(db, project))


# --- global settings flow to projects -------------------------------------------


async def test_a_global_change_reaches_projects_without_touching_their_rows(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    await _set_global(db, GLOBAL)
    plain = await _project(db, tmp_path, "plain")
    pinned = await _project(db, tmp_path, "pinned", {"execution": {"review_rounds_max": 3}})
    rows_before = await _raw_rows(db)

    res = await client.patch(
        "/api/config/global",
        json={"config": {"execution": {"review_rounds_max": 11, "auto_merge": True}}},
    )
    assert res.status_code == 200, res.text
    assert res.json()["updated_projects"] == []

    assert await _raw_rows(db) == rows_before  # byte-identical project rows
    plain_cfg = (await client.get(f"/api/projects/{plain.id}/config")).json()
    assert plain_cfg["execution"]["review_rounds_max"] == 11
    assert plain_cfg["execution"]["auto_merge"] is True
    pinned_cfg = await resolve_project_config(db, pinned)
    assert pinned_cfg.execution.review_rounds_max == 3  # the project override still wins
    assert pinned_cfg.execution.auto_merge is True


async def test_a_project_without_overrides_is_builtin_plus_global(
    db: Database, tmp_path: Path
) -> None:
    """Everything comes from built-in + global except the integration branch, which is the
    project's own default_branch: the orchestrator merges its tasks there."""
    global_layer = deep_merge(GLOBAL, {"execution": {"target_branch": "trunk"}})
    await _set_global(db, global_layer)
    project = await _project(db, tmp_path, "empty", default_branch="dev")

    effective = (await resolve_project_config(db, project)).model_dump(mode="json")
    expected = load_layered_config(global_layer, None).model_dump(mode="json")
    assert effective["execution"].pop("target_branch") == "dev"
    assert expected["execution"].pop("target_branch") == "trunk"
    assert effective == expected
    assert await _merge_branch(db, project.id) == "dev"


# --- attach -------------------------------------------------------------------


async def test_attach_stores_no_copy_of_the_global_settings(
    client: httpx.AsyncClient, daemon: Daemon, db: Database, tmp_path: Path
) -> None:
    await _set_global(db, deep_merge(GLOBAL, {"execution": {"target_branch": "trunk"}}))

    created = await client.post(
        "/api/projects", json={"name": "rest", "path": str(_repo_dir(tmp_path, "rest"))}
    )
    assert created.status_code == 200, created.text
    assert created.json()["config"] == {}  # the branch lives in default_branch, the rest inherits
    assert created.json()["default_branch"] == "main"

    is_error, attached = await _mcp(
        daemon,
        "conclave_attach_project",
        {"name": "mcp", "path": str(_repo_dir(tmp_path, "mcp")), "default_branch": "release"},
    )
    assert is_error is False
    assert attached["config"] == {}
    assert attached["default_branch"] == "release"

    for project_id, branch in ((created.json()["id"], "main"), (attached["id"], "release")):
        doc = (await client.get(f"/api/projects/{project_id}/config")).json()
        assert doc["execution"]["target_branch"] == branch  # not the global "trunk"
        assert doc["execution"]["review_rounds_max"] == 7  # the rest comes from global
        assert await _merge_branch(db, project_id) == branch


async def test_attach_stores_only_the_preset_values_that_differ(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    await _set_global(db, GLOBAL)
    await repo.upsert_store_item(
        db,
        kind=StoreItemKind.config_preset,
        name="team",
        payload={
            "execution": {"max_parallel_tasks": 1, "review_rounds_max": 7, "auto_merge": True}
        },
    )

    created = await client.post(
        "/api/projects",
        json={
            "name": "preset",
            "path": str(_repo_dir(tmp_path, "preset")),
            "config_preset": "team",
            "workflow_template": "fast_track",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["config"] == {
        "execution": {"auto_merge": True},
        "workflow": workflow_templates()["fast_track"].model_dump(mode="json"),
    }


# --- project writes store minimal deltas -------------------------------------


async def test_saving_the_full_document_stores_only_what_changed(
    client: httpx.AsyncClient, db: Database, tmp_path: Path
) -> None:
    await _set_global(db, GLOBAL)
    pid = (
        await client.post(
            "/api/projects", json={"name": "doc", "path": str(_repo_dir(tmp_path, "doc"))}
        )
    ).json()["id"]
    doc = (await client.get(f"/api/projects/{pid}/config")).json()

    for method in ("PATCH", "PUT"):
        res = await client.request(method, f"/api/projects/{pid}/config", json={"config": doc})
        assert res.status_code == 200, res.text
        assert await _stored(db, pid) == {}

    doc["execution"]["max_parallel_tasks"] = 3
    assert (await client.patch(f"/api/projects/{pid}/config", json={"config": doc})).is_success
    assert await _stored(db, pid) == {"execution": {"max_parallel_tasks": 3}}

    doc["execution"]["max_parallel_tasks"] = 1  # back to the inherited value
    assert (await client.put(f"/api/projects/{pid}/config", json={"config": doc})).is_success
    assert await _stored(db, pid) == {}


async def test_saves_never_move_the_branch_tasks_merge_into(
    client: httpx.AsyncClient, daemon: Daemon, db: Database, tmp_path: Path
) -> None:
    """A project attached before layering kept the global target_branch copied into it, while
    its default_branch column says "main"; it merges into the stored target. Showing the
    effective config, sending it back whole, or an unrelated write must not change that."""
    await _set_global(db, {"execution": {"target_branch": "release"}})
    legacy = await _project(
        db, tmp_path, "legacy", {"execution": {"target_branch": "release", "max_parallel_tasks": 2}}
    )
    plain = await _project(db, tmp_path, "plain", {"execution": {"target_branch": "main"}})
    expected = {legacy.id: "release", plain.id: "main"}
    assert {pid: await _merge_branch(db, pid) for pid in expected} == expected

    for pid, branch in expected.items():
        doc = (await client.get(f"/api/projects/{pid}/config")).json()
        assert doc["execution"]["target_branch"] == branch  # where it merges, not the global
        res = await client.patch(f"/api/projects/{pid}/config", json={"config": doc})
        assert res.status_code == 200, res.text
        res = await client.put(f"/api/projects/{pid}/config", json={"config": doc})
        assert res.status_code == 200, res.text
        is_error, resolved = await _mcp(
            daemon, "conclave_patch_config", {"project_id": pid, "config": doc}
        )
        assert is_error is False
        assert resolved["execution"]["target_branch"] == branch
        is_error, _ = await _mcp(
            daemon,
            "conclave_configure_agent",
            {"project_id": pid, "agent": "reviewer", "model": "p/reviewer"},
        )
        assert is_error is False

    assert {pid: await _merge_branch(db, pid) for pid in expected} == expected
    rows = await _raw_rows(db)
    assert (rows[legacy.id][0], rows[plain.id][0]) == ("main", "main")  # columns untouched
    reviewer = {"agent_overrides": {"reviewer": {"model": "p/reviewer"}}}
    assert await _stored(db, legacy.id) == {
        "execution": {"target_branch": "release", "max_parallel_tasks": 2},
        **reviewer,
    }
    assert await _stored(db, plain.id) == reviewer  # equal to its column, so not stored

    # Moving the branch on purpose moves default_branch with it; nothing else stores it.
    res = await client.patch(
        f"/api/projects/{legacy.id}/config",
        json={"config": {"execution": {"target_branch": " dev "}}},
    )
    assert res.status_code == 200, res.text
    is_error, _ = await _mcp(
        daemon,
        "conclave_patch_config",
        {"project_id": plain.id, "config": {"execution": {"target_branch": "next"}}},
    )
    assert is_error is False
    rows = await _raw_rows(db)
    assert (rows[legacy.id][0], rows[plain.id][0]) == ("dev", "next")
    assert await _stored(db, legacy.id) == {"execution": {"max_parallel_tasks": 2}, **reviewer}
    assert await _stored(db, plain.id) == reviewer
    assert (await _merge_branch(db, legacy.id), await _merge_branch(db, plain.id)) == (
        "dev",
        "next",
    )


async def test_unset_removes_overrides_over_rest_and_mcp(
    client: httpx.AsyncClient, daemon: Daemon, db: Database, tmp_path: Path
) -> None:
    await _set_global(db, GLOBAL)
    project = await _project(
        db,
        tmp_path,
        "unset",
        {
            "agent_overrides": {
                "reviewer": {"model": "p/reviewer", "effort": "max"},
                "tester": {"model": "p/tester"},
            },
            "execution": {"setup_command": "make venv"},
        },
    )

    res = await client.patch(
        f"/api/projects/{project.id}/config",
        json={"unset": ["agent_overrides.reviewer.effort", "agent_overrides.tester"]},
    )
    assert res.status_code == 200, res.text
    assert await _stored(db, project.id) == {
        "agent_overrides": {"reviewer": {"model": "p/reviewer"}},
        "execution": {"setup_command": "make venv"},
    }

    is_error, resolved = await _mcp(
        daemon,
        "conclave_patch_config",
        {
            "project_id": project.id,
            "unset": ["agent_overrides.reviewer", "execution.setup_command"],
        },
    )
    assert is_error is False
    assert await _stored(db, project.id) == {}
    assert resolved["execution"]["setup_command"] is None
    assert resolved["agent_overrides"] == {}


async def test_bad_config_writes_are_rejected_and_write_nothing(
    client: httpx.AsyncClient, daemon: Daemon, db: Database, tmp_path: Path
) -> None:
    await repo.upsert_store_item(
        db,
        kind=StoreItemKind.config_preset,
        name="broken",
        payload={"execution": {"max_parallel_tasks": 99}},
    )
    project = await _project(db, tmp_path, "guarded", {"execution": {"max_parallel_tasks": 2}})
    rows_before = await _raw_rows(db)
    bad_workflow = {"nodes": [], "edges": [{"id": "e", "source": "a", "target": "b"}]}

    for body in (
        {"config": {"execution": {"max_parallel_tasks": 99}}},
        {"config": {"bogus": 1}},
        {"config": {"workflow": bad_workflow}},
        {"unset": ["bogus.key"]},
        {"unset": ["workflow.nodes"]},
        {"unset": ["execution..setup_command"]},
        {"unset": [f"execution.k{i}" for i in range(101)]},
    ):
        res = await client.patch(f"/api/projects/{project.id}/config", json=body)
        assert res.status_code == 422, (body, res.text)
        res = await client.patch("/api/config/global", json=body)
        assert res.status_code == 422, (body, res.text)

    install = await client.post(
        f"/api/projects/{project.id}/store/install",
        json={"kind": "config_preset", "name": "broken"},
    )
    assert install.status_code == 422, install.text
    attach = await client.post(
        "/api/projects",
        json={"name": "x", "path": str(_repo_dir(tmp_path, "x")), "config_preset": "broken"},
    )
    assert attach.status_code == 422, attach.text

    for args in (
        {"project_id": project.id, "unset": ["bogus.x"]},
        {"project_id": project.id, "unset": "execution.setup_command"},
        {"project_id": project.id, "config": {"execution": {"max_parallel_tasks": 99}}},
        {"project_id": "global", "unset": ["workflow.nodes"]},
    ):
        is_error, _ = await _mcp(daemon, "conclave_patch_config", args)
        assert is_error is True, args

    assert await _raw_rows(db) == rows_before
    global_item = await repo.get_store_item_by_name(
        db, StoreItemKind.config_preset, GLOBAL_DEFAULTS_PRESET
    )
    assert global_item is None


# --- apply_to_all_projects ----------------------------------------------------


async def test_global_patches_leave_project_rows_alone_by_default(
    client: httpx.AsyncClient, daemon: Daemon, db: Database, tmp_path: Path
) -> None:
    await _project(db, tmp_path, "a", {"execution": {"review_rounds_max": 3}})
    await _project(db, tmp_path, "b", {"execution": {"auto_merge": True}})
    rows_before = await _raw_rows(db)

    res = await client.patch(
        "/api/config/global", json={"config": {"execution": {"review_rounds_max": 9}}}
    )
    assert res.status_code == 200, res.text
    is_error, _ = await _mcp(
        daemon, "conclave_patch_config", {"review_rounds_max": 10, "target_branch": "release"}
    )
    assert is_error is False

    assert await _raw_rows(db) == rows_before


async def test_apply_to_all_projects_clears_the_patched_keys_from_every_project(
    client: httpx.AsyncClient, daemon: Daemon, db: Database, tmp_path: Path
) -> None:
    a = await repo.create_project(
        db,
        name="a",
        path=str(tmp_path / "a"),
        default_branch="dev",
        config={
            "execution": {"review_rounds_max": 3, "target_branch": "dev"},
            "agent_defaults": {"model": "p/a"},
        },
    )
    b = await _project(db, tmp_path, "b", {"execution": {"auto_merge": True}})

    res = await client.patch(
        "/api/config/global",
        json={
            "config": {"execution": {"review_rounds_max": 9, "target_branch": "release"}},
            "apply_to_all_projects": True,
        },
    )
    assert res.status_code == 200, res.text
    assert sorted(res.json()["updated_projects"]) == sorted([a.id, b.id])

    a_row = await repo.get_project(db, a.id)
    b_row = await repo.get_project(db, b.id)
    assert a_row is not None and b_row is not None
    assert a_row.config == {"agent_defaults": {"model": "p/a"}}
    assert b_row.config == {"execution": {"auto_merge": True}}
    assert (a_row.default_branch, b_row.default_branch) == ("release", "release")
    effective = await resolve_project_config(db, a_row)
    assert effective.execution.review_rounds_max == 9
    assert effective.execution.target_branch == "release"

    is_error, _ = await _mcp(
        daemon,
        "conclave_patch_config",
        {"config": {"agent_defaults": {"model": "g/default"}}, "apply_to_all_projects": True},
    )
    assert is_error is False
    assert await _stored(db, a.id) == {}
    assert await _stored(db, b.id) == {"execution": {"auto_merge": True}}


async def test_global_configure_agent_clears_the_field_in_every_project(
    daemon: Daemon, db: Database, tmp_path: Path
) -> None:
    a = await _project(
        db,
        tmp_path,
        "a",
        {
            "agent_defaults": {"model": "p/a", "effort": "low"},
            "agent_overrides": {"reviewer": {"model": "p/reviewer-a"}},
        },
    )
    b = await _project(
        db,
        tmp_path,
        "b",
        {"agent_overrides": {"reviewer": {"model": "p/reviewer-b", "effort": "max"}}},
    )

    is_error, _ = await _mcp(
        daemon, "conclave_configure_agent", {"agent": "reviewer", "model": "g/reviewer"}
    )
    assert is_error is False
    assert await _stored(db, a.id) == {"agent_defaults": {"model": "p/a", "effort": "low"}}
    assert await _stored(db, b.id) == {"agent_overrides": {"reviewer": {"effort": "max"}}}
    b_effective = await resolve_project_config(db, await repo.get_project(db, b.id))
    assert resolve_agent(b_effective, "reviewer").model == "g/reviewer"
    assert resolve_agent(b_effective, "reviewer").effort == "max"

    is_error, _ = await _mcp(daemon, "conclave_configure_agent", {"model": "g/default"})
    assert is_error is False
    assert await _stored(db, a.id) == {"agent_defaults": {"effort": "low"}}


# --- inspecting layers --------------------------------------------------------


async def test_layers_are_inspectable_over_rest_and_mcp(
    client: httpx.AsyncClient, daemon: Daemon, db: Database, tmp_path: Path
) -> None:
    await _set_global(db, GLOBAL)
    project = await _project(db, tmp_path, "layers", {"execution": {"max_parallel_tasks": 2}})

    effective = (await client.get(f"/api/projects/{project.id}/config")).json()
    assert "effective" not in effective and effective["execution"]["max_parallel_tasks"] == 2
    expected = {
        "effective": effective,
        "global": GLOBAL,
        "project": {"execution": {"max_parallel_tasks": 2}},
    }
    layered = await client.get(f"/api/projects/{project.id}/config", params={"layers": "true"})
    assert layered.json() == expected
    bad = await client.get(f"/api/projects/{project.id}/config", params={"layers": "maybe"})
    assert bad.status_code == 422

    is_error, via_mcp = await _mcp(
        daemon, "conclave_get_config", {"project_id": project.id, "include_layers": True}
    )
    assert is_error is False and via_mcp == expected
    _, plain = await _mcp(daemon, "conclave_get_config", {"project_id": project.id})
    assert plain == effective
    _, global_layers = await _mcp(daemon, "conclave_get_config", {"include_layers": True})
    assert global_layers == {
        "effective": load_layered_config(GLOBAL).model_dump(mode="json"),
        "global": GLOBAL,
    }


# --- v13: normalising existing project configs --------------------------------

LEGACY_GLOBAL: dict[str, Any] = {
    "execution": {"target_branch": "develop", "review_rounds_max": 6},
    "experimental": {"auto_planner_char_threshold": 250},
    "agent_defaults": {"model": "g/default", "effort": "high"},
    "agent_overrides": {"reviewer": {"model": "g/reviewer"}},
}
# attach copied the global settings in, then a Settings save froze the full document
_FROZEN = load_project_config(
    deep_merge(LEGACY_GLOBAL, {"execution": {"max_parallel_tasks": 2}})
).model_dump(mode="json")
LEGACY_ROWS: dict[str, dict[str, Any]] = {
    "frozen": _FROZEN,
    "frozen_on_develop": _FROZEN,
    "partial": {
        "execution": {"target_branch": "develop", "max_parallel_tasks": 4},
        "experimental": {"auto_planner_char_threshold": 250},
    },
    "agents": {
        "agent_defaults": {"model": "g/default", "timeout_minutes": 30},
        "agent_overrides": {"reviewer": {"model": "g/reviewer", "effort": "max"}},
    },
    "workflow": {
        "workflow": workflow_templates()["tdd_loop"].model_dump(mode="json"),
        "agent_defaults": {"effort": "high"},
    },
    "minimal": {"execution": {"max_parallel_tasks": 3}},
}
# The default_branch column of each row ("main" unless listed).
LEGACY_BRANCHES: dict[str, str] = {"frozen_on_develop": "develop"}
EXPECTED_DELTAS: dict[str, dict[str, Any]] = {
    # The copied global target names another branch than the column: tasks merge there, so
    # it stays; on the "develop" column it is what the project inherits anyway.
    "frozen": {"execution": {"max_parallel_tasks": 2, "target_branch": "develop"}},
    "frozen_on_develop": {"execution": {"max_parallel_tasks": 2}},
    "partial": {"execution": {"target_branch": "develop", "max_parallel_tasks": 4}},
    "agents": {
        "agent_defaults": {"timeout_minutes": 30},
        "agent_overrides": {"reviewer": {"effort": "max"}},
    },
    "workflow": {"workflow": workflow_templates()["tdd_loop"].model_dump(mode="json")},
    "minimal": {"execution": {"max_parallel_tasks": 3}},
}


async def _connect(path: Path) -> Database:
    database = Database(path)
    await database.connect()
    return database


async def _seed_legacy_db(path: Path, tmp_path: Path) -> tuple[dict[str, str], list[str]]:
    """A database at v12 holding legacy rows: ``({shape: project id}, [rows left as they are])``."""
    database = await _connect(path)
    try:
        await _set_global(database, LEGACY_GLOBAL)
        ids = {
            name: (
                await _project(
                    database, tmp_path, name, cfg, LEGACY_BRANCHES.get(name, "main")
                )
            ).id
            for name, cfg in LEGACY_ROWS.items()
        }
        invalid = await _project(database, tmp_path, "invalid", {"execution": {"bogus": 1}})
        corrupt = await _project(database, tmp_path, "corrupt")
        await database.execute(
            "UPDATE projects SET config_json = '{not json' WHERE id = ?", (corrupt.id,)
        )
    finally:
        await database.close()
    return ids, [invalid.id, corrupt.id]


async def test_migration_13_rewrites_stored_configs_to_minimal_deltas(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "legacy.db"
    monkeypatch.setattr("conclave.db.database.MIGRATIONS", MIGRATIONS[:12])
    ids, left_alone = await _seed_legacy_db(path, tmp_path)
    effective_before = {
        name: load_layered_config(
            LEGACY_GLOBAL, cfg, default_branch=LEGACY_BRANCHES.get(name, "main")
        )
        for name, cfg in LEGACY_ROWS.items()
    }

    # A runner that predates data steps (an in-place hot reload keeps the old Database method)
    # applies v13's SQL and records the version but never calls run().
    monkeypatch.setattr(
        "conclave.db.database.MIGRATIONS", [*MIGRATIONS[:12], replace(MIGRATIONS[12], run=None)]
    )
    database = await _connect(path)
    try:
        rows_before = await _raw_rows(database)
        assert await database.fetchval("SELECT MAX(version) FROM schema_version") == 13
        assert await database.fetchval("SELECT version FROM pending_data_migrations") == 13
        legacy_branches: dict[str, str] = {}
        for name, pid in ids.items():  # where the pre-layering runtime merged each project
            legacy = await repo.get_project(database, pid)
            assert legacy is not None
            legacy_branches[name] = _resolve_target_branch(
                legacy, load_project_config(legacy.config)
            )
    finally:
        await database.close()

    caplog.set_level(logging.INFO, logger="conclave.db.migrations")
    monkeypatch.setattr("conclave.db.database.MIGRATIONS", MIGRATIONS)
    database = await _connect(path)  # the next start runs the pending step
    try:
        applied = await database.fetchall("SELECT version, name FROM schema_version")
        assert (13, "normalise_project_config_deltas") in {(r[0], r[1]) for r in applied}
        assert await database.fetchval("SELECT COUNT(*) FROM pending_data_migrations") == 0
        for name, pid in ids.items():
            project = await repo.get_project(database, pid)
            assert project is not None
            assert project.config == EXPECTED_DELTAS[name], name
            effective = await resolve_project_config(database, project)
            assert effective == effective_before[name], name
            assert await _merge_branch(database, pid) == legacy_branches[name], name
            assert effective.execution.target_branch == legacy_branches[name], name
        frozen = await repo.get_project(database, ids["frozen"])
        assert frozen is not None
        assert await resolve_project_config(database, frozen) == load_project_config(
            LEGACY_ROWS["frozen"]
        )  # the legacy built-in + stored config too
        rows = await _raw_rows(database)
        for pid in left_alone:  # corrupt or invalid: left as it is
            assert rows[pid] == rows_before[pid]
        assert "5 of 8 rewritten" in caplog.text
    finally:
        await database.close()

    caplog.clear()
    database = await _connect(path)  # applied once: a restart runs nothing
    try:
        assert await _raw_rows(database) == rows
        assert "rewritten" not in caplog.text
        await database.execute("INSERT INTO pending_data_migrations(version) VALUES (13)")
    finally:
        await database.close()
    database = await _connect(path)  # and running the step again changes nothing
    try:
        assert await _raw_rows(database) == rows
        assert "0 of 8 rewritten" in caplog.text
    finally:
        await database.close()


async def test_a_fresh_database_runs_migration_13_once(db: Database) -> None:
    applied = await db.fetchall("SELECT version, name FROM schema_version WHERE version = 13")
    assert [(r[0], r[1]) for r in applied] == [(13, "normalise_project_config_deltas")]
    assert await db.fetchval("SELECT COUNT(*) FROM pending_data_migrations") == 0
