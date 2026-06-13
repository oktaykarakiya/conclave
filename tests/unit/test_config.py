"""Unit tests for the typed, layered configuration."""

from __future__ import annotations

from conclave.config import (
    ConclaveConfig,
    Effort,
    config_schema,
    deep_merge,
    effective_protected,
    load_project_config,
    resolve_agent,
)


def test_defaults_load() -> None:
    cfg = ConclaveConfig()
    assert cfg.execution.target_branch == "main"
    assert cfg.execution.wall_clock_budget_minutes == 720
    assert cfg.agents.mandatory == ["tester", "security", "reviewer"]
    assert "architect" in cfg.agents.conditional
    assert cfg.experimental.grounding_checks is True


def test_deep_merge_recurses_dicts_and_replaces_scalars_and_lists() -> None:
    base = {"a": {"b": 1, "c": 2}, "list": [1, 2], "x": 1}
    override = {"a": {"c": 3, "d": 4}, "list": [9], "x": 2}
    merged = deep_merge(base, override)
    assert merged == {"a": {"b": 1, "c": 3, "d": 4}, "list": [9], "x": 2}
    # inputs are not mutated
    assert base["a"] == {"b": 1, "c": 2}


def test_project_overrides_apply() -> None:
    cfg = load_project_config(
        {"execution": {"target_branch": "vibes", "auto_merge": False}}
    )
    assert cfg.execution.target_branch == "vibes"
    assert cfg.execution.auto_merge is False
    # untouched keys keep defaults
    assert cfg.execution.wall_clock_budget_minutes == 720


def test_resolve_agent_layers_defaults_then_overrides() -> None:
    cfg = load_project_config(
        {
            "agent_defaults": {"timeout_minutes": 120, "max_retries": 3},
            "agent_overrides": {
                "tester": {"timeout_minutes": 180, "engine_profile": "deepseek"},
                "developer": {"max_retries": 20},
            },
        }
    )
    tester = resolve_agent(cfg, "tester")
    assert tester.timeout_minutes == 180
    assert tester.engine_profile == "deepseek"
    assert tester.max_retries == 3  # inherited from defaults

    developer = resolve_agent(cfg, "developer")
    assert developer.max_retries == 20
    assert developer.timeout_minutes == 120  # inherited

    # an agent with no overrides gets pure defaults
    reviewer = resolve_agent(cfg, "reviewer")
    assert reviewer.timeout_minutes == 120
    assert reviewer.engine_profile == "system-default"


def test_protected_floor_cannot_be_removed() -> None:
    # Even if the user clears protected.files, the floor (.env*, .git) remains.
    cfg = load_project_config({"protected": {"files": [], "directories": []}})
    files, dirs = effective_protected(cfg)
    assert "*.env" in files
    assert ".env" in files
    assert ".git" in dirs


def test_effort_enum_values() -> None:
    assert [e.value for e in Effort] == ["low", "medium", "high", "xhigh", "max"]


def test_config_schema_is_renderable() -> None:
    schema = config_schema()
    assert schema["type"] == "object"
    assert "execution" in schema["properties"]
    # nested models are referenced/defined for the UI to render
    assert "$defs" in schema
