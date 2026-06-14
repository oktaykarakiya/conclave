"""Typed configuration models for Conclave.

These Pydantic models are the single source of truth for both built-in defaults
(instantiate with no args) and the resolved, validated shape the engine reads.
Overrides are stored sparsely (per-project, per-agent) and deep-merged onto these
defaults by :mod:`conclave.config.resolver`. The models' JSON Schema drives the
web UI's config forms, so the UI is the sole editor — no hand-edited YAML.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Effort(StrEnum):
    """Reasoning-effort tiers, ordered low < medium < high < xhigh < max."""

    low = "low"
    medium = "medium"
    high = "high"
    xhigh = "xhigh"
    max = "max"


class AgentRole(StrEnum):
    """How an agent participates in the pipeline."""

    planning = "planning"  # runs before the developer (planner, pm, architect-as-planner)
    developer = "developer"  # implements changes
    mandatory = "mandatory"  # always reviews (tester, security, reviewer)
    conditional = "conditional"  # reviews only when diff triggers fire
    analyst = "analyst"  # repo intelligence
    hunter = "hunter"  # bug discovery (Phase 2)
    postmortem = "postmortem"  # failure analysis


class ArgMode(StrEnum):
    """How an Engine Profile expresses model/effort to the ``claude`` CLI.

    - ``inherit``: pass nothing → use the host's logged-in default (the unset case).
    - ``flag``: append ``--model``/``--effort`` (native Claude / Anthropic-direct).
    - ``env``: route via ``ANTHROPIC_*`` / ``CLAUDE_CODE_*`` env vars (e.g. DeepSeek).
    """

    inherit = "inherit"
    flag = "flag"
    env = "env"


class Verdict(StrEnum):
    """Possible review outcomes.

    ``decline`` (abstain — "do not auto-fix, edge-case risk") powers the bug-fixer's
    consensus mechanism; ``unknown`` is the post-grounding downgrade target.
    """

    pass_ = "pass"
    fail = "fail"
    block = "block"
    decline = "decline"
    unknown = "unknown"


class DeclineConsensus(StrEnum):
    """Threshold required for the team to refuse (decline) a bug fix."""

    all_mandatory = "all_mandatory"  # every mandatory agent must concur (default)
    majority = "majority"  # a simple majority of voting agents
    any_two = "any_two"  # at least two agents concur


class AgentSettings(BaseModel):
    """Per-agent execution settings (the layer resolved per dispatch)."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model: str | None = Field(
        default=None,
        description="Free-text model name. None defers to the engine profile / system default.",
    )
    effort: Effort | None = Field(default=None, description="Reasoning effort tier, or unset.")
    timeout_minutes: int = Field(default=120, ge=1, le=600)
    max_retries: int = Field(default=3, ge=1, le=50)
    engine_profile: str = Field(
        default="system-default",
        description="Name of the Engine Profile to dispatch under.",
    )


class ExecutionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_branch: str = Field(default="main", description="Integration branch tasks merge into.")
    branch_prefix: str = Field(default="conclave/", description="Per-task branch name prefix.")
    wall_clock_budget_minutes: int = Field(
        default=720, ge=0, description="Hard cap on a single task's retry loop. 0 disables."
    )
    review_rounds_max: int = Field(default=20, ge=1, le=50)
    parallel_reviewers: bool = Field(default=False)
    stop_on_failure: bool = Field(default=False)
    auto_merge: bool = Field(default=True, description="Fast-forward/merge into target on success.")
    baseline_test_command: str | None = Field(
        default=None,
        description="Test command for the baseline snapshot. None => use learned repo command.",
    )
    require_full_green: bool = Field(
        default=True, description="Require zero failing suites (minus governed Quarantine)."
    )


class ExperimentalSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    structured_verdicts: bool = True
    grounding_checks: bool = True
    cross_attempt_memory: bool = True
    cross_attempt_memory_entries: int = Field(default=5, ge=0, le=50)
    persistent_baseline_cache: bool = True
    planner_enabled: bool = True
    # SUPERSEDED by classify_level / planning.level_thresholds (engine/level_router.py):
    # the planner gate is now scale-adaptive (BMad L0-L4), not a single char threshold.
    # Retained (not deleted) to avoid config churn; no longer read by the orchestrator.
    auto_planner_char_threshold: int = Field(default=400, ge=0)
    post_mortem_enabled: bool = True


_DEFAULT_IGNORE_PATTERNS = [
    "node_modules",
    "vendor",
    "dist",
    "build",
    "*.min.js",
    "*.lock",
    ".git",
    ".venv",
]


class LevelConditions(BaseModel):
    """Match rule for a single planning level's request-length band.

    Nested per level into :attr:`PlanningSettings.level_thresholds` and evaluated by
    :func:`conclave.engine.level_router._matches`. Char bounds are inclusive on both
    ends; an empty ``required_keywords`` is vacuously satisfied and ``file_count_estimate``
    is advisory (enforced only when a file estimate is supplied at call time).
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


class L2Settings(BaseModel):
    """Field gates for the enhanced one-shot (BMad L2) planning path.

    When a task classifies to level 2, these flags DEMAND the named fields in the plan
    JSON: the planner prompt is augmented to require them up-front, and any the planner
    still omits trigger a clearly-labeled corrective note folded into the developer's
    plan preamble (never a task failure). Both default True so the "enhanced" intent
    holds out of the box; the real planner persona already emits both, so the happy path
    adds no note. Both False collapses L2 to a plain L1 one-shot. See
    :meth:`conclave.engine.orchestrator.Orchestrator._maybe_plan`.
    """

    model_config = ConfigDict(extra="forbid")

    require_acceptance_criteria: bool = Field(
        default=True,
        description="Demand non-empty acceptance_criteria in the L2 plan; note it if omitted.",
    )
    require_risk_assessment: bool = Field(
        default=True,
        description="Demand non-empty risks in the L2 plan; note it if omitted.",
    )


class L3Settings(BaseModel):
    """Stage gates for the three-agent sequential (BMad L3) planning path.

    When a task classifies to level 3, each flag controls whether that stage's
    planning persona is dispatched. The stages run in strict order — PM →
    Architect-as-Planner → Planner — and prior-section artifacts are passed into
    the planner's prompt. An intermediate None is skipped; a final (planner) None
    degrades to an L1-style empty preamble. See
    :meth:`conclave.engine.orchestrator.Orchestrator._run_l3`.
    """

    model_config = ConfigDict(extra="forbid")

    produce_prd: bool = Field(
        default=True,
        description="Dispatch 'pm' persona for a PRD-lite section.",
    )
    produce_arch_note: bool = Field(
        default=True,
        description="Dispatch 'architect-as-planner' persona for an architecture note.",
    )
    decompose_into_stories: bool = Field(
        default=True,
        description="Dispatch 'planner' persona for concrete story/plan JSON (final stage).",
    )


class L4Settings(BaseModel):
    """Settings for the epic-decomposition (BMad L4) planning path.

    When ``auto_create_children`` is on and the task classifies to level 4, the
    orchestrator dispatches a decomposer persona (via :meth:`_run_l4`) to split the
    epic into child tasks.  ``max_child_tasks`` caps the row output.  Both defaults
    keep the L4 path active out of the box; set ``auto_create_children`` to False
    to degrade L4 epics to a direct L1 execution instead.
    """

    model_config = ConfigDict(extra="forbid")

    auto_create_children: bool = Field(
        default=True,
        description="When True, a level-4 epic decomposes into child tasks.",
    )
    max_child_tasks: int = Field(
        default=12,
        ge=1,
        le=100,
        description="Maximum number of child tasks to create from an epic decomposition.",
    )


class PlanningSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    priorities: list[str] = Field(
        default_factory=lambda: [
            "security",
            "bugs",
            "reliability",
            "performance",
            "maintainability",
            "test_coverage",
        ]
    )
    ignore_patterns: list[str] = Field(default_factory=lambda: list(_DEFAULT_IGNORE_PATTERNS))
    min_level: int = Field(default=0, ge=0, le=4, description="Floor on scale-adaptive planning.")
    max_level: int = Field(default=4, ge=0, le=4, description="Cap on scale-adaptive planning.")
    # Default is a clean, gap-free tiling of request length onto BMad L0-L4:
    # [0,50]=L0 (trivial, no planning), [51,499]=L1 (light) and L2 when BOTH 'implement'
    # and 'feature' appear, [500,999]=L3, [1000,inf]=L4. The router (level_router.py) keys
    # its trivial fast-path on L0's own ceiling and fills band-holes with L1, so this tiling
    # stays correct even if a stored override shadows an individual level (see R1 there).
    level_thresholds: dict[int, LevelConditions] = Field(
        default_factory=lambda: {
            0: LevelConditions(min_chars=0, max_chars=50),
            1: LevelConditions(min_chars=51, max_chars=499),
            2: LevelConditions(
                min_chars=51, max_chars=499, required_keywords=["implement", "feature"]
            ),
            3: LevelConditions(min_chars=500, max_chars=999),
            4: LevelConditions(min_chars=1000, max_chars=None),
        },
        description="Per-level (BMad L0-L4) request-length match bands.",
    )
    l2_settings: L2Settings = Field(
        default_factory=L2Settings,
        description="Field gates for the enhanced one-shot (BMad L2) planning path.",
    )
    l3_settings: L3Settings = Field(
        default_factory=L3Settings,
        description="Stage gates for the three-agent sequential (BMad L3) planning path.",
    )
    l4_settings: L4Settings = Field(
        default_factory=L4Settings,
        description="Settings for the epic-decomposition (BMad L4) planning path.",
    )


class ConditionalAgent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    triggers: list[str] = Field(default_factory=list)


_DEFAULT_CONDITIONAL: dict[str, list[str]] = {
    "architect": [
        "new_files", "api_change", "db_schema", "new_dependency", "complexity_medium_plus",
    ],
    "legal": ["new_dependency", "user_data_change", "auth_change", "third_party_api"],
    "risk": [
        "complexity_medium_plus", "db_change", "auth_change", "payment_change",
        "infra_change", "files_gt_5",
    ],
    "performance": ["db_change", "api_change", "loop_operation", "frontend_bundle"],
    "devops": ["dockerfile", "env_var", "new_service", "deploy_config", "migration"],
}


class AgentsPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mandatory: list[str] = Field(default_factory=lambda: ["tester", "security", "reviewer"])
    conditional: dict[str, ConditionalAgent] = Field(
        default_factory=lambda: {
            name: ConditionalAgent(triggers=list(triggers))
            for name, triggers in _DEFAULT_CONDITIONAL.items()
        }
    )
    max_invocations_per_task: int = Field(default=20, ge=1, le=100)
    decline_consensus: DeclineConsensus = Field(
        default=DeclineConsensus.all_mandatory,
        description="Threshold for the team to refuse (decline) a bug fix.",
    )


class ProtectedSettings(BaseModel):
    """Paths agents must never modify. ``.env*`` and ``.git`` are an enforced floor
    (see :func:`conclave.config.resolver.effective_protected`); users may add, not remove."""

    model_config = ConfigDict(extra="forbid")

    files: list[str] = Field(
        default_factory=lambda: [
            "*.env",
            "*.env.*",
            "docker-compose.yml",
            "docker-compose.*.yml",
            "*.lock",
        ]
    )
    directories: list[str] = Field(default_factory=lambda: [".git"])


class ConclaveConfig(BaseModel):
    """The fully-resolved project configuration shape.

    ``ConclaveConfig()`` (no args) is the built-in default layer. Per-project overrides
    are deep-merged onto it; ``agent_overrides[name]`` are deep-merged onto ``agent_defaults``.
    """

    model_config = ConfigDict(extra="forbid")

    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    experimental: ExperimentalSettings = Field(default_factory=ExperimentalSettings)
    planning: PlanningSettings = Field(default_factory=PlanningSettings)
    agents: AgentsPolicy = Field(default_factory=AgentsPolicy)
    protected: ProtectedSettings = Field(default_factory=ProtectedSettings)
    agent_defaults: AgentSettings = Field(default_factory=AgentSettings)
    agent_overrides: dict[str, dict[str, object]] = Field(default_factory=dict)
