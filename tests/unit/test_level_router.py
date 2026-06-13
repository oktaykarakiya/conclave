"""Unit tests for the scale-adaptive planning level router.

The first group exercises ``_matches`` per-field in isolation: the inclusive char
boundaries, case-insensitive multi-keyword matching, and the advisory file_estimate rule.
The second group exercises ``classify_level`` end to end against a default
``PlanningSettings()`` — the acceptance battery, the 400/500 seam, the planner flags and
min/max clamps, plus the R1 robustness case proving a stored (shadowed) legacy
``level_thresholds[1]`` cannot reintroduce a misclassification. Both are pure and
LLM-free (see the level_router spec).
"""

from __future__ import annotations

from conclave.config.models import PlanningSettings
from conclave.engine.level_router import LevelConditions, _matches, classify_level


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


# --- classify_level: acceptance battery on a default PlanningSettings() ------


def test_trivial_request_gets_no_planning() -> None:
    # <=50 chars sits at L0's fast-path ceiling: no planner (0), not lifted to L1.
    assert classify_level("x" * 50, PlanningSettings()) == 0


def test_short_no_keyword_request_is_level_1() -> None:
    # 200 chars with neither keyword: L2 misses on keywords, so light planning (L1).
    assert classify_level("x" * 200, PlanningSettings()) == 1


def test_medium_request_with_both_keywords_is_level_2() -> None:
    request = "implement feature " + "x" * 282
    assert len(request) == 300  # in the [51,499] band carrying BOTH keywords
    assert classify_level(request, PlanningSettings()) == 2


def test_medium_request_with_one_keyword_is_level_1() -> None:
    # Only one of the two required keywords present => L2 misses, falls back to L1.
    request = "implement " + "x" * 290
    assert len(request) == 300
    assert classify_level(request, PlanningSettings()) == 1


def test_large_request_is_level_3() -> None:
    assert classify_level("x" * 600, PlanningSettings()) == 3


def test_huge_request_is_capped_at_level_4() -> None:
    # >=1000 chars matches L4 (max_chars None); the default max_level=4 keeps it there.
    assert classify_level("x" * 1000, PlanningSettings()) == 4


# --- classify_level: the 400/500 seam ---------------------------------------


def test_seam_450_no_keyword_is_level_1_not_0() -> None:
    # Inside the clean L1 band [51,499]; a non-trivial request is never dropped to 0.
    assert classify_level("x" * 450, PlanningSettings()) == 1


def test_seam_500_no_keyword_is_level_3() -> None:
    # 500 is the inclusive floor of L3 [500,999].
    assert classify_level("x" * 500, PlanningSettings()) == 3


# --- classify_level: planner flags ------------------------------------------


def test_planner_disabled_forces_level_0() -> None:
    assert classify_level("x" * 600, PlanningSettings(), planner_enabled=False) == 0


def test_use_planner_false_forces_level_0() -> None:
    assert classify_level("x" * 600, PlanningSettings(), use_planner=False) == 0


def test_use_planner_true_lifts_trivial_to_level_1() -> None:
    # Explicit opt-in bypasses the L0 fast-path; the floor becomes 1, never 0.
    assert classify_level("x" * 30, PlanningSettings(), use_planner=True) == 1


# --- classify_level: min_level / max_level clamps ---------------------------


def test_min_level_floors_result() -> None:
    # min_level=2 disables the L0 fast-path and floors the gap-filler at 2.
    assert classify_level("x" * 200, PlanningSettings(min_level=2)) == 2


def test_max_level_caps_result() -> None:
    # max_level=0 caps a would-be-L3 request down into [0,0].
    assert classify_level("x" * 600, PlanningSettings(max_level=0)) == 0


# --- classify_level: R1 robustness (shadowed legacy threshold) --------------


def test_r1_shadowed_legacy_threshold_still_classifies_correctly() -> None:
    # Simulate a project saved before the retile: level_thresholds[1] persisted as the
    # legacy [0,400] band (a deep_merge result that SHADOWS the default), with the rest of
    # the tiling intact. classify_level must STILL be correct — proving config shadowing
    # cannot reintroduce either the trivial-fast-path or the band-hole defect.
    thresholds = dict(PlanningSettings().level_thresholds)
    thresholds[1] = LevelConditions(min_chars=0, max_chars=400)
    planning = PlanningSettings(level_thresholds=thresholds)

    assert classify_level("x" * 50, planning) == 0  # fast-path keyed on L0, not shadowed
    assert classify_level("x" * 450, planning) == 1  # 401-499 band-hole => gap-filler L1
    assert classify_level("x" * 500, planning) == 3  # L3 band unaffected
