"""Unit tests for layered config resolution and minimal-delta storage (pure functions)."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

import conclave
from conclave.config import (
    ConclaveConfig,
    apply_project_overrides,
    config_delta,
    deep_merge,
    load_layered_config,
    load_project_config,
    patch_leaf_paths,
    patched_branch,
    remove_config_paths,
    resolve_agent,
    validate_unset_paths,
    workflow_templates,
)
from conclave.db import Project
from conclave.engine.orchestrator import _resolve_target_branch

SRC = Path(conclave.__file__).resolve().parent
RESOLVER = SRC / "config" / "resolver.py"


def _workflow(template_id: str) -> dict[str, Any]:
    return workflow_templates()[template_id].model_dump(mode="json")


def _project(default_branch: str, config: dict[str, Any] | None = None) -> Project:
    return Project(
        id="p",
        name="p",
        path="/repo",
        default_branch=default_branch,
        config=config or {},
        created_at="2026-11-02T00:00:00+00:00",
    )


# --- resolution order ---------------------------------------------------------


def test_builtin_defaults_apply_when_no_layer_sets_anything() -> None:
    assert load_layered_config() == ConclaveConfig()
    assert load_layered_config({}, {}) == ConclaveConfig()


def test_global_layer_sits_between_builtin_and_project() -> None:
    global_layer = {"execution": {"review_rounds_max": 7, "auto_merge": False}}
    project_layer = {"execution": {"review_rounds_max": 9}}

    cfg = load_layered_config(global_layer, project_layer)

    assert cfg.execution.review_rounds_max == 9  # project wins over global
    assert cfg.execution.auto_merge is False  # global wins over built-in
    assert cfg.execution.wall_clock_budget_minutes == 720  # built-in fills the rest


def test_agent_overrides_merge_key_by_key_across_layers() -> None:
    global_layer = {"agent_overrides": {"tester": {"model": "g/tester", "effort": "max"}}}
    project_layer = {"agent_overrides": {"tester": {"model": "p/tester"}}}

    tester = resolve_agent(load_layered_config(global_layer, project_layer), "tester")

    assert tester.model == "p/tester"
    assert tester.effort == "max"


def test_global_agent_defaults_reach_every_agent() -> None:
    cfg = load_layered_config({"agent_defaults": {"model": "g/default", "effort": "high"}}, {})

    reviewer = resolve_agent(cfg, "reviewer")
    assert (reviewer.model, reviewer.effort) == ("g/default", "high")


def test_a_global_agent_override_beats_the_project_agent_defaults() -> None:
    """Per-agent overrides apply last, so a global agent_overrides entry wins over any layer's
    agent_defaults for that agent; other agents get the project's defaults."""
    cfg = load_layered_config(
        {"agent_overrides": {"reviewer": {"model": "g/reviewer"}}},
        {"agent_defaults": {"model": "p/default"}},
    )

    assert resolve_agent(cfg, "reviewer").model == "g/reviewer"
    assert resolve_agent(cfg, "tester").model == "p/default"


def test_a_workflow_is_replaced_whole_across_layers() -> None:
    fast = _workflow("fast_track")
    sparse_project_workflow = {"nodes": fast["nodes"], "edges": fast["edges"]}

    cfg = load_layered_config(
        {"workflow": _workflow("deep_audit")}, {"workflow": sparse_project_workflow}
    )

    assert cfg.workflow is not None
    # Nothing leaks from the global graph: unset fields take the model defaults.
    assert cfg.workflow.template_id == "standard"
    assert [n.id for n in cfg.workflow.nodes] == [n["id"] for n in fast["nodes"]]


def test_a_partial_level_threshold_keeps_its_sibling_fields() -> None:
    """Stored JSON keys are strings; the base is merged in JSON mode so "2" lines up with 2."""
    cfg = load_project_config({"planning": {"level_thresholds": {"2": {"min_chars": 5}}}})

    level_2 = cfg.planning.level_thresholds[2]
    assert level_2.min_chars == 5
    assert level_2.required_keywords == ["implement", "feature"]


# --- the integration branch -----------------------------------------------------


def test_the_branch_column_is_inherited_between_global_and_project() -> None:
    global_layer = {"execution": {"target_branch": "release"}}
    stored = {"execution": {"target_branch": "hotfix"}}

    assert load_layered_config(global_layer).execution.target_branch == "release"
    on_main = load_layered_config(global_layer, None, default_branch="main")
    assert on_main.execution.target_branch == "main"  # the project's branch beats global
    pinned = load_layered_config(global_layer, stored, default_branch="main")
    assert pinned.execution.target_branch == "hotfix"  # its stored target beats both


def test_a_stored_target_branch_survives_only_when_it_names_another_branch() -> None:
    global_layer = {"execution": {"target_branch": "release"}}
    stored = {"execution": {"target_branch": "release"}}

    assert apply_project_overrides(global_layer, stored, default_branch="main")[1] == stored
    assert apply_project_overrides(global_layer, stored, default_branch="release")[1] == {}
    _, attached = apply_project_overrides(
        global_layer, None, {"execution": {"target_branch": "main"}}, default_branch="main"
    )
    assert attached == {}  # attaching on a branch stores nothing for it, whatever global says


@pytest.mark.parametrize(
    ("column", "stored"),
    [
        ("main", {}),
        ("main", {"execution": {"target_branch": "release"}}),
        ("release", {"execution": {"target_branch": "release"}}),
        ("dev", {"execution": {"max_parallel_tasks": 2}}),
    ],
)
def test_the_effective_target_branch_is_where_tasks_merge(
    column: str, stored: dict[str, Any]
) -> None:
    effective = load_layered_config(
        {"execution": {"target_branch": "release"}}, stored, default_branch=column
    )

    assert effective.execution.target_branch == _resolve_target_branch(
        _project(column, stored), effective
    )


def test_only_a_patch_naming_another_branch_moves_the_project() -> None:
    plain = _project("main")
    diverged = _project("main", {"execution": {"target_branch": "release"}})

    assert patched_branch(plain, {"execution": {"target_branch": "main"}}) is None
    assert patched_branch(plain, {"execution": {"target_branch": " dev "}}) == "dev"
    assert patched_branch(plain, {"execution": {"max_parallel_tasks": 2}}) is None
    assert patched_branch(plain, None) is None
    assert patched_branch(diverged, {"execution": {"target_branch": "release"}}) is None
    assert patched_branch(diverged, {"execution": {"target_branch": "main"}}) == "main"


# --- minimal deltas -----------------------------------------------------------

GLOBAL = {
    "execution": {"review_rounds_max": 7},
    "agent_defaults": {"model": "g/default"},
    "agent_overrides": {"tester": {"effort": "max"}},
}


def test_saving_the_full_resolved_document_stores_an_empty_delta() -> None:
    full = load_layered_config(GLOBAL, {}).model_dump(mode="json")

    effective, delta = apply_project_overrides(GLOBAL, {}, full)

    assert delta == {}
    assert effective == load_layered_config(GLOBAL, {})


def test_one_changed_field_is_stored_alone_and_setting_it_back_removes_it() -> None:
    full = load_layered_config(GLOBAL, {}).model_dump(mode="json")
    full["execution"]["max_parallel_tasks"] = 3

    _, delta = apply_project_overrides(GLOBAL, {}, full)
    assert delta == {"execution": {"max_parallel_tasks": 3}}

    _, delta = apply_project_overrides(GLOBAL, delta, {"execution": {"max_parallel_tasks": 1}})
    assert delta == {}


def test_setting_a_global_value_back_drops_the_project_override() -> None:
    stored = {"execution": {"review_rounds_max": 9}}

    _, delta = apply_project_overrides(GLOBAL, stored, {"execution": {"review_rounds_max": 7}})

    assert delta == {}


def test_lists_and_the_workflow_are_compared_whole() -> None:
    priorities = ConclaveConfig().planning.priorities
    reordered = [*priorities[1:], priorities[0]]

    _, delta = apply_project_overrides(
        None, {}, {"planning": {"priorities": reordered}, "workflow": _workflow("fast_track")}
    )

    assert delta == {"planning": {"priorities": reordered}, "workflow": _workflow("fast_track")}
    _, same = apply_project_overrides(
        {"workflow": _workflow("fast_track")}, {}, {"workflow": _workflow("fast_track")}
    )
    assert same == {}


def test_a_new_map_entry_is_kept_whole() -> None:
    effective = load_layered_config(None, {"agent_overrides": {"pm": {"model": "a/b"}}})
    inherited = ConclaveConfig().model_dump(mode="json")

    delta = config_delta(effective.model_dump(mode="json"), inherited)

    assert delta == {"agent_overrides": {"pm": {"model": "a/b"}}}


def test_null_is_a_value_not_a_delete_marker() -> None:
    global_layer = {"execution": {"setup_command": "make venv"}}

    effective, delta = apply_project_overrides(
        global_layer, {}, {"execution": {"setup_command": None}}
    )

    assert effective.execution.setup_command is None
    assert delta == {"execution": {"setup_command": None}}


def test_resetting_the_workflow_over_a_global_one_is_stored_as_null() -> None:
    effective, delta = apply_project_overrides(
        {"workflow": _workflow("deep_audit")}, {}, {"workflow": None}
    )

    assert effective.workflow is None
    assert delta == {"workflow": None}


# --- unset --------------------------------------------------------------------


def test_unset_removes_a_project_only_entry_and_prunes_emptied_parents() -> None:
    stored = {"agent_overrides": {"reviewer": {"model": "p/reviewer"}}}

    _, delta = apply_project_overrides(GLOBAL, stored, unset=["agent_overrides.reviewer"])

    assert delta == {}


def test_unset_removes_a_single_field_so_it_inherits_again() -> None:
    stored = {
        "agent_overrides": {"tester": {"model": "p/tester", "effort": "low"}},
        "execution": {"review_rounds_max": 9},
    }

    effective, delta = apply_project_overrides(
        GLOBAL,
        stored,
        unset=["agent_overrides.tester.effort", "execution.review_rounds_max"],
    )

    assert delta == {"agent_overrides": {"tester": {"model": "p/tester"}}}
    assert resolve_agent(effective, "tester").effort == "max"  # inherited from global
    assert effective.execution.review_rounds_max == 7


def test_unset_runs_before_the_patch() -> None:
    stored = {"execution": {"setup_command": "old"}}

    _, delta = apply_project_overrides(
        None, stored, {"execution": {"setup_command": "new"}}, unset=["execution.setup_command"]
    )

    assert delta == {"execution": {"setup_command": "new"}}


def test_unset_of_a_missing_path_is_a_no_op() -> None:
    stored = {"execution": {"max_parallel_tasks": 2}}

    assert remove_config_paths(stored, ["execution.setup_command", "agent_overrides.x"]) == stored
    assert stored == {"execution": {"max_parallel_tasks": 2}}  # the input is not mutated


@pytest.mark.parametrize("path", ["", "a..b", "bogus.x", "workflow.nodes", "execution."])
def test_invalid_unset_paths_are_rejected(path: str) -> None:
    with pytest.raises(ValueError, match="invalid unset path"):
        validate_unset_paths([path])


def test_unset_paths_strip_stray_quotes_from_keys() -> None:
    assert validate_unset_paths(['agent_overrides."architect-as-planner"']) == [
        "agent_overrides.architect-as-planner"
    ]


def test_patch_leaf_paths() -> None:
    patch = {
        "execution": {"target_branch": "x", "nothing": {}},
        "agents": {"mandatory": ["tester"]},
        "workflow": _workflow("fast_track"),
    }

    assert patch_leaf_paths(patch) == ["execution.target_branch", "agents.mandatory", "workflow"]


# --- normalising legacy stored configs ------------------------------------------

NORMALISE_GLOBAL: dict[str, Any] = {
    "execution": {"target_branch": "develop", "review_rounds_max": 6},
    "experimental": {"auto_planner_char_threshold": 0},
    "agent_defaults": {"model": "g/default", "effort": "high"},
    "agent_overrides": {"reviewer": {"model": "g/reviewer"}},
}


def _frozen_full_document() -> dict[str, Any]:
    """What attach (global copied in) followed by a full-document Settings save stored."""
    attached = deep_merge(NORMALISE_GLOBAL, {"execution": {"max_parallel_tasks": 2}})
    return load_project_config(attached).model_dump(mode="json")


LEGACY_SHAPES: dict[str, dict[str, Any]] = {
    "frozen_full_document": _frozen_full_document(),
    "partial_delta": {
        "execution": {"target_branch": "develop", "max_parallel_tasks": 4},
        "experimental": {"auto_planner_char_threshold": 0},
    },
    "agent_overrides": {
        "agent_defaults": {"model": "g/default", "timeout_minutes": 30},
        "agent_overrides": {
            "reviewer": {"model": "g/reviewer", "effort": "max"},
            '"architect-as-planner"': {"model": "p/planner"},
        },
    },
    "custom_workflow": {"workflow": _workflow("tdd_loop"), "agent_defaults": {"effort": "high"}},
}


@pytest.mark.parametrize("column", ["main", "develop"])
@pytest.mark.parametrize("shape", sorted(LEGACY_SHAPES))
def test_normalisation_preserves_the_effective_config_and_is_idempotent(
    shape: str, column: str
) -> None:
    before = LEGACY_SHAPES[shape]

    _, delta = apply_project_overrides(NORMALISE_GLOBAL, before, default_branch=column)

    after = load_layered_config(NORMALISE_GLOBAL, delta, default_branch=column)
    assert after == load_layered_config(NORMALISE_GLOBAL, before, default_branch=column)
    second = apply_project_overrides(NORMALISE_GLOBAL, delta, default_branch=column)[1]
    assert second == delta  # a second pass agrees


@pytest.mark.parametrize("column", ["main", "develop"])
@pytest.mark.parametrize("shape", sorted(LEGACY_SHAPES))
def test_normalisation_keeps_the_branch_tasks_merge_into(shape: str, column: str) -> None:
    """Old attach copied the global target_branch into the project, and the orchestrator merges
    into a stored target before the default_branch column; the delta must keep that branch."""
    before = LEGACY_SHAPES[shape]
    legacy_branch = _resolve_target_branch(_project(column, before), load_project_config(before))

    _, delta = apply_project_overrides(NORMALISE_GLOBAL, before, default_branch=column)

    after = load_layered_config(NORMALISE_GLOBAL, delta, default_branch=column)
    assert _resolve_target_branch(_project(column, delta), after) == legacy_branch
    assert after.execution.target_branch == legacy_branch  # and the effective config shows it


def test_normalising_a_frozen_document_keeps_its_legacy_config_and_shrinks_it() -> None:
    before = LEGACY_SHAPES["frozen_full_document"]

    _, on_main = apply_project_overrides(NORMALISE_GLOBAL, before, default_branch="main")
    _, on_develop = apply_project_overrides(NORMALISE_GLOBAL, before, default_branch="develop")

    for column, delta in (("main", on_main), ("develop", on_develop)):
        assert load_layered_config(
            NORMALISE_GLOBAL, delta, default_branch=column
        ) == load_project_config(before)
    # The copied global target names another branch than "main", so only there it stays.
    assert on_main == {"execution": {"max_parallel_tasks": 2, "target_branch": "develop"}}
    assert on_develop == {"execution": {"max_parallel_tasks": 2}}


# --- guards -------------------------------------------------------------------


def _source_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)


def test_no_direct_project_config_resolution_outside_the_resolver() -> None:
    """Every runtime, REST and MCP site resolves through resolve_project_config (layered)."""
    pattern = re.compile(r"load_project_config\(\s*[A-Za-z_.]*\.config")
    offenders = [
        f"{path.relative_to(SRC)}:{lineno}"
        for path in _source_files()
        if path != RESOLVER
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == []


def test_no_project_config_writes_bypass_the_resolver() -> None:
    """Stored project configs are only written as minimal deltas, by the resolver."""
    offenders: list[str] = []
    for path in _source_files():
        if path == RESOLVER:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            callee = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else ""
            )
            writes_config = callee == "update_project_config" or (
                callee in ("create_project", "update_project")
                and any(kw.arg == "config" for kw in node.keywords)
            )
            if writes_config:
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    # repositories.py defines the raw writers; nothing else may call them with a config.
    assert offenders == []
