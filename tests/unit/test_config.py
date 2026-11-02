"""Unit tests for the typed, layered configuration."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from conclave.config import (
    AgentSettings,
    BugFixerSessionOverride,
    ConclaveConfig,
    Effort,
    config_schema,
    deep_merge,
    effective_protected,
    load_project_config,
    resolve_agent,
    resolve_bug_fixer_session,
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
        {"execution": {"target_branch": "vibes", "auto_merge": True}}
    )
    assert cfg.execution.target_branch == "vibes"
    assert cfg.execution.auto_merge is True
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


def test_bug_fixer_policy_defaults() -> None:
    cfg = ConclaveConfig()
    assert cfg.bug_fixer.max_candidates == 10
    assert cfg.bug_fixer.max_attempts == 3
    # The policy leaves wall-clock unset so resolution reuses the execution cap.
    assert cfg.bug_fixer.wall_clock_budget_minutes is None
    # Safety-first: an auto-merging agent polls the reviewers before auto-fixing by default.
    assert cfg.bug_fixer.require_decline_consensus is True


def test_resolve_bug_fixer_session_uses_policy_defaults() -> None:
    cfg = ConclaveConfig()
    session = resolve_bug_fixer_session(cfg)
    assert session.max_candidates == 10
    assert session.max_attempts == 3
    # Neither override nor policy pins a budget, so it falls back to the execution cap.
    assert session.wall_clock_budget_minutes == cfg.execution.wall_clock_budget_minutes == 720


def test_resolve_bug_fixer_session_override_wins_over_policy() -> None:
    cfg = load_project_config(
        {"bug_fixer": {"max_candidates": 5, "max_attempts": 2, "wall_clock_budget_minutes": 90}}
    )
    override = BugFixerSessionOverride(max_candidates=1, wall_clock_budget_minutes=15)
    session = resolve_bug_fixer_session(cfg, override)
    # Fields present on the start-request payload take precedence...
    assert session.max_candidates == 1
    assert session.wall_clock_budget_minutes == 15
    # ...and omitted fields fall back to the project policy default.
    assert session.max_attempts == 2


def test_resolve_bug_fixer_session_policy_budget_overrides_execution_fallback() -> None:
    # A policy-pinned budget wins over the execution cap; the execution setting is untouched.
    cfg = load_project_config({"bug_fixer": {"wall_clock_budget_minutes": 60}})
    session = resolve_bug_fixer_session(cfg)
    assert session.wall_clock_budget_minutes == 60
    assert cfg.execution.wall_clock_budget_minutes == 720


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


# --- quality-first defaults ----------------------------------------------------------


def test_quality_first_defaults() -> None:
    cfg = ConclaveConfig()
    assert cfg.execution.auto_merge is False  # a green task waits on its branch
    assert cfg.experimental.auto_planner_char_threshold == 0  # every task is planned
    assert cfg.agent_defaults.effort is None  # auto: highest sensible per engine
    assert cfg.system.hot_reload_watch is False
    assert cfg.ui.model_choices == []


def test_quality_first_settings_are_in_the_config_schema() -> None:
    schema = config_schema()
    assert {"system", "ui"} <= schema["properties"].keys()
    defs = schema["$defs"]
    assert defs["ExecutionSettings"]["properties"]["auto_merge"]["default"] is False
    threshold = defs["ExperimentalSettings"]["properties"]["auto_planner_char_threshold"]
    assert (threshold["default"], threshold["minimum"]) == (0, 0)
    watch = defs["SystemSettings"]["properties"]["hot_reload_watch"]
    assert (watch["type"], watch["default"]) == ("boolean", False)
    choices = defs["UiSettings"]["properties"]["model_choices"]
    assert (choices["type"], choices["items"]["type"], choices["maxItems"]) == (
        "array", "string", 200
    )


def test_new_sections_validate_their_values() -> None:
    cfg = load_project_config(
        {
            "system": {"hot_reload_watch": True},
            "ui": {"model_choices": [" deepseek/deepseek-flash ", "claude-code/opus"]},
        }
    )
    assert cfg.system.hot_reload_watch is True
    assert cfg.ui.model_choices == ["deepseek/deepseek-flash", "claude-code/opus"]
    for bad in (
        {"system": {"hot_reload_watch": "maybe"}},
        {"system": {"watch": True}},
        {"ui": {"model_choices": "claude-code/opus"}},
        {"ui": {"model_choices": ["  "]}},
        {"ui": {"model_choices": ["m" * 201]}},
    ):
        with pytest.raises(ValidationError):
            load_project_config(bad)


def test_builtin_config_presets_keep_auto_merge_on() -> None:
    from conclave.bootstrap import _BUILT_IN_CONFIG_PRESETS

    assert len(_BUILT_IN_CONFIG_PRESETS) == 4
    for name, (_, _, payload) in _BUILT_IN_CONFIG_PRESETS.items():
        assert payload["execution"]["auto_merge"] is True, name
        assert load_project_config(payload).execution.auto_merge is True, name


# --- context (prompt bulk limits) --------------------------------------------


def test_context_defaults() -> None:
    cfg = ConclaveConfig()
    assert cfg.context.model_dump() == {
        "compaction_enabled": False,
        "compaction_max_chars": 60_000,
        "bulk_max_chars": 200_000,
    }
    # None = inherit context.compaction_enabled; stored true/false stay valid.
    assert AgentSettings().compact_before_start is None
    assert AgentSettings(compact_before_start=True).compact_before_start is True


@pytest.mark.parametrize("field", ["compaction_max_chars", "bulk_max_chars"])
@pytest.mark.parametrize("value", [999, 2_000_001])
def test_context_char_limits_are_bounded(field: str, value: int) -> None:
    with pytest.raises(ValidationError) as excinfo:
        load_project_config({"context": {field: value}})
    assert excinfo.value.errors()[0]["loc"] == ("context", field)
    assert load_project_config({"context": {field: 1_000}}).context.model_dump()[field] == 1_000


def test_gate_integrity_defaults() -> None:
    cfg = ConclaveConfig()
    assert cfg.execution.allow_no_test_command is False
    assert cfg.execution.test_integrity == "block"


def test_gate_integrity_fields_are_in_the_config_schema() -> None:
    props = config_schema()["$defs"]["ExecutionSettings"]["properties"]
    assert props["allow_no_test_command"]["type"] == "boolean"
    assert props["allow_no_test_command"]["default"] is False
    assert props["test_integrity"]["enum"] == ["block", "warn", "off"]
    assert props["test_integrity"]["default"] == "block"


def test_test_integrity_rejects_unknown_modes() -> None:
    warn = load_project_config({"execution": {"test_integrity": "warn"}})
    assert warn.execution.test_integrity == "warn"
    with pytest.raises(ValidationError) as excinfo:
        load_project_config({"execution": {"test_integrity": "strict"}})
    assert excinfo.value.errors()[0]["loc"] == ("execution", "test_integrity")


def test_context_is_in_the_config_schema() -> None:
    schema = config_schema()
    assert schema["properties"]["context"] == {"$ref": "#/$defs/ContextSettings"}
    props = schema["$defs"]["ContextSettings"]["properties"]
    for field in ("compaction_max_chars", "bulk_max_chars"):
        assert (props[field]["minimum"], props[field]["maximum"]) == (1_000, 2_000_000)
    assert props["compaction_enabled"]["default"] is False
