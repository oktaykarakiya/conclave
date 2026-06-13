"""Unit tests for the scale-adaptive planning level router's band matcher.

These exercise ``_matches`` per-field in isolation plus the inclusive char boundaries,
case-insensitive multi-keyword matching, and the advisory file_estimate rule. No config,
schema, or orchestrator wiring is involved — the helper is pure (see level_router spec).
"""

from __future__ import annotations

from conclave.engine.level_router import LevelConditions, _matches


def test_default_condition_matches_any_request() -> None:
    # All-default condition (min_chars=0, no max, no keywords, no file rule) is vacuous.
    assert _matches(LevelConditions(), "") is True
    assert _matches(LevelConditions(), "anything at all") is True


def test_min_chars_boundary_is_inclusive() -> None:
    cond = LevelConditions(min_chars=10)
    assert _matches(cond, "x" * 10) is True  # == min_chars: inclusive
    assert _matches(cond, "x" * 11) is True  # above the floor
    assert _matches(cond, "x" * 9) is False  # one below the floor: rejected


def test_max_chars_boundary_is_inclusive() -> None:
    cond = LevelConditions(max_chars=5)
    assert _matches(cond, "x" * 5) is True  # == max_chars: inclusive
    assert _matches(cond, "x" * 4) is True  # below the ceiling
    assert _matches(cond, "x" * 6) is False  # one above the ceiling: rejected


def test_max_chars_none_means_no_upper_bound() -> None:
    assert _matches(LevelConditions(max_chars=None), "x" * 10_000) is True


def test_single_keyword_is_case_insensitive() -> None:
    # Mixed case on either side must still match.
    assert _matches(LevelConditions(required_keywords=["BUG"]), "fix the bug") is True
    assert _matches(LevelConditions(required_keywords=["bug"]), "Fix the BUG") is True
    assert _matches(LevelConditions(required_keywords=["bug"]), "no defect here") is False


def test_multi_keyword_requires_all_present() -> None:
    cond = LevelConditions(required_keywords=["Fix", "BUG"])
    assert _matches(cond, "fix the bug") is True  # both present (case-insensitive)
    assert _matches(cond, "fix the issue") is False  # 'bug' missing
    assert _matches(cond, "the bug report") is False  # 'fix' missing


def test_empty_keyword_list_is_vacuously_true() -> None:
    assert _matches(LevelConditions(required_keywords=[]), "") is True


def test_file_estimate_none_ignores_file_count_estimate() -> None:
    # Pre-execution norm: no estimate supplied => the advisory field is ignored.
    cond = LevelConditions(file_count_estimate=5)
    assert _matches(cond, "r") is True
    assert _matches(cond, "r", file_estimate=None) is True


def test_file_estimate_enforced_when_supplied() -> None:
    cond = LevelConditions(file_count_estimate=5)
    assert _matches(cond, "r", file_estimate=5) is True  # >= threshold: inclusive
    assert _matches(cond, "r", file_estimate=6) is True  # above threshold
    assert _matches(cond, "r", file_estimate=4) is False  # below threshold: rejected


def test_file_count_estimate_none_never_enforces_file_rule() -> None:
    # No threshold on the condition => the file rule is inert for any estimate.
    cond = LevelConditions(file_count_estimate=None)
    assert _matches(cond, "r", file_estimate=0) is True
    assert _matches(cond, "r", file_estimate=999) is True


def test_all_fields_must_hold_together() -> None:
    # A condition exercising every field only matches when ALL clauses pass at once.
    cond = LevelConditions(
        min_chars=5, max_chars=20, required_keywords=["api"], file_count_estimate=3
    )
    assert _matches(cond, "change the api", file_estimate=3) is True
    assert _matches(cond, "api", file_estimate=3) is False  # too short
    assert _matches(cond, "x" * 21 + " api", file_estimate=3) is False  # too long
    assert _matches(cond, "change the ui", file_estimate=3) is False  # keyword missing
    assert _matches(cond, "change the api", file_estimate=2) is False  # file estimate short
