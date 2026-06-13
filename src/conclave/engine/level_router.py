"""Scale-adaptive planning level router (BMad L0-L4).

This module owns the pure decision logic that maps an incoming task request to a
planning level. This task implements only the band-matching helper ``_matches``;
``classify_level`` is wired in later tasks (2/4) and the canonical spec below is the
single source of truth they must implement against.

``LevelConditions`` is defined locally (not in :mod:`conclave.config.models`) because
this task forbids config/schema changes; tasks 2/4 will nest it into
``PlanningSettings.level_thresholds``.

CANONICAL CLASSIFICATION SPEC (single source of truth for tasks 2 and 4 - state it
verbatim in the module docstring). classify_level computes, in order:
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

from pydantic import BaseModel, ConfigDict, Field


class LevelConditions(BaseModel):
    """Match rule for a single planning level's band.

    Defined here rather than in :mod:`conclave.config.models` because this task forbids
    config/schema changes; tasks 2/4 nest it into ``PlanningSettings.level_thresholds``.
    """

    model_config = ConfigDict(extra="forbid")

    min_chars: int = Field(default=0, ge=0, description="Inclusive lower bound on len(request).")
    max_chars: int | None = Field(
        default=None, description="Inclusive upper bound on len(request); None => unbounded."
    )
    required_keywords: list[str] = Field(
        default_factory=list,
        description="All must appear case-insensitively in the request for a match.",
    )
    file_count_estimate: int | None = Field(
        default=None,
        description="Advisory: enforced only when a file estimate is supplied at call time.",
    )


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
