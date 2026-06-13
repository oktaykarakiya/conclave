"""Scale-adaptive planning level router (BMad L0-L4).

This module owns the pure decision logic that maps an incoming task request to a
planning level. It exposes the band-matching helper ``_matches`` and the top-level
``classify_level``, which implements the canonical spec below.

``LevelConditions`` and ``PlanningSettings`` live in :mod:`conclave.config.models`
(``PlanningSettings.level_thresholds`` nests the per-level bands). They are imported
and re-exported here so callers and tests can reach ``LevelConditions`` from either.

CANONICAL CLASSIFICATION SPEC (the single source of truth ``classify_level`` implements).
classify_level computes, in order:
1. If planner_enabled is False OR use_planner is False => return 0.
2. TRIVIAL FAST-PATH keyed on L0's OWN ceiling (NOT on L1's band): if min_level == 0
   AND level_thresholds.get(0) exists AND its max_chars is not None AND
   len(request) <= level_thresholds[0].max_chars AND use_planner is not True => return 0
   (no planning). L0's default [0,50] ceiling has never changed, so this is immune to
   stored-config drift in level_thresholds[1] (see R1 below).
3. Otherwise scan levels from max_level DOWN to min_level and pick the HIGHEST level
   whose level_thresholds[level] fully matches via _matches.
4. GAP-FILLER: if NO level matched (a non-trivial request falling in a hole between
   bands, e.g. 401-499 under a stale L1=[0,400]), return max(min_level, 1) - the
   lightest planning level - NEVER 0. A non-trivial request is thus never silently
   dropped to no-planning.
5. If use_planner is True, return max(result, 1) so explicit opt-in never yields 0.
6. Always clamp into [min_level, max_level].

R1 ROBUSTNESS (why steps 2 & 4 are framed this way): the config UI round-trips the FULL
resolved config (web/api.py:160 getConfig => model_dump; patch_config persists verbatim)
and deep_merge (config/resolver.py:25-38) lets the STORED value win, so any
already-configured project has the legacy level_thresholds[1]={min_chars:0,max_chars:400}
persisted and would SHADOW any default retile. By keying L0 on L0's own ceiling (step 2)
and filling band-holes with L1 (step 4), classify_level is correct REGARDLESS of the
stored level_thresholds[1] values. file_count_estimate is unused at classify time
(file_estimate is None pre-execution). The whole scheme supersedes
experimental.auto_planner_char_threshold; trivial (<= L0 ceiling) requests get NO
planning, preserving today's skip for short requests.
"""

from __future__ import annotations

from conclave.config.models import LevelConditions, PlanningSettings

# ``LevelConditions`` is imported for ``_matches``'s signature and re-exported so the
# existing ``from conclave.engine.level_router import LevelConditions`` keeps resolving;
# ``PlanningSettings`` carries the per-level ``level_thresholds`` bands classify_level reads.


def classify_level(
    request: str,
    planning: PlanningSettings,
    *,
    use_planner: bool | None = None,
    planner_enabled: bool = True,
) -> int:
    """Map ``request`` to a BMad planning level in ``[min_level, max_level]``.

    Implements the canonical spec (see the module docstring) exactly, in order:
    1. ``planner_enabled`` False OR ``use_planner`` False => 0 (no planning).
    2. Trivial fast-path keyed on L0's OWN ceiling: ``min_level == 0`` AND
       ``level_thresholds[0]`` exists with a non-None ``max_chars`` AND
       ``len(request) <= max_chars`` AND ``use_planner`` is not True => 0. Keying on L0
       (never on L1) makes this immune to stored drift in ``level_thresholds[1]``.
    3. Otherwise pick the HIGHEST level matching via ``_matches``, scanning ``max_level``
       down to ``min_level``.
    4. Gap-filler: if nothing matched, ``max(min_level, 1)`` — a non-trivial request is
       never silently dropped to no-planning (NEVER 0).
    5. If ``use_planner`` is True, ``max(result, 1)`` so explicit opt-in never yields 0.
    6. Always clamp into ``[min_level, max_level]``.
    """
    min_level = planning.min_level
    max_level = planning.max_level
    thresholds = planning.level_thresholds

    # Step 1: planner globally off, or an explicit per-task opt-out, means no planning.
    if not planner_enabled or use_planner is False:
        return 0

    # Step 2: trivial fast-path keyed on L0's own ceiling (immune to stored L1 drift).
    l0 = thresholds.get(0)
    if (
        min_level == 0
        and l0 is not None
        and l0.max_chars is not None
        and len(request) <= l0.max_chars
        and use_planner is not True
    ):
        return 0

    # Step 3: highest fully-matching level, scanning max_level DOWN to min_level.
    matched: int | None = None
    for level in range(max_level, min_level - 1, -1):
        conditions = thresholds.get(level)
        if conditions is not None and _matches(conditions, request):
            matched = level
            break

    # Step 4: gap-filler — a non-trivial request that fell in a band-hole still plans.
    result = matched if matched is not None else max(min_level, 1)

    # Step 5: explicit opt-in never yields 0.
    if use_planner is True:
        result = max(result, 1)

    # Step 6: clamp into [min_level, max_level].
    return max(min_level, min(result, max_level))


# --- module helpers ---------------------------------------------------------


def _matches(
    conditions: LevelConditions, request: str, file_estimate: int | None = None
) -> bool:
    """True iff ``request`` satisfies every clause of ``conditions``.

    Char bounds are inclusive on both ends; ``max_chars`` None means no upper bound.
    Every ``required_keywords`` entry must appear case-insensitively. The file rule is
    ADVISORY: it is enforced only when a ``file_estimate`` is actually supplied at call
    time AND the condition carries a ``file_count_estimate`` — pre-execution the estimate
    is None and the field is ignored entirely (see the module docstring's R1 note).
    """
    if len(request) < conditions.min_chars:
        return False
    if conditions.max_chars is not None and len(request) > conditions.max_chars:
        return False
    lowered = request.lower()
    if any(keyword.lower() not in lowered for keyword in conditions.required_keywords):
        return False
    if (
        file_estimate is not None
        and conditions.file_count_estimate is not None
        and file_estimate < conditions.file_count_estimate
    ):
        return False
    return True
