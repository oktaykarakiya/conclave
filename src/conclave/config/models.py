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
    repro = "repro"  # reproduction gate: prove a candidate with a failing test (Phase 2)
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
    setup_command: str | None = Field(
        default=None,
        description=(
            "Provisioning command run ONCE per worktree before any agent, e.g. to build a "
            "virtualenv and install deps. Agents and the green-gate then share this environment, "
            "so reviewer checks match the gate. None => no provisioning."
        ),
    )
    baseline_test_command: str | None = Field(
        default=None,
        description="Test command for the baseline snapshot. None => use learned repo command.",
    )
    require_full_green: bool = Field(
        default=True, description="Require zero failing suites (minus governed Quarantine)."
    )
    setup_timeout_seconds: int = Field(
        default=900,
        ge=1,
        description="Timeout in seconds for the setup_command provisioning step.",
    )
    retention_events_max: int = Field(
        default=10_000,
        ge=100,
        description=(
            "Maximum number of recent events to retain per project before GC prunes older rows. "
            "A DELETE subquery keeps the highest-id rows; when the count is below this cap the "
            "DELETE is cheap (no rows match)."
        ),
    )


class ExperimentalSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    structured_verdicts: bool = True
    grounding_checks: bool = True
    cross_attempt_memory: bool = True
    cross_attempt_memory_entries: int = Field(default=5, ge=0, le=50)
    persistent_baseline_cache: bool = True
    planner_enabled: bool = True
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
    """Heuristic thresholds that classify a task into a planning level (0-4)."""

    model_config = ConfigDict(extra="forbid")

    min_chars: int = Field(default=0, ge=0, description="Minimum request length in characters.")
    max_chars: int | None = Field(
        default=None, description="Maximum request length (None = unbounded)."
    )
    required_keywords: list[str] = Field(
        default_factory=list, description="Keywords that must appear in the request."
    )
    file_count_estimate: int | None = Field(
        default=None, description="Estimated files touched (None = not used for classification)."
    )


class L2Settings(BaseModel):
    """Enhanced one-shot planning: acceptance criteria + risk assessment."""

    model_config = ConfigDict(extra="forbid")

    require_acceptance_criteria: bool = Field(default=True)
    require_risk_assessment: bool = Field(default=True)


class L3Settings(BaseModel):
    """Multi-stage planning: PRD-lite → architecture note → stories."""

    model_config = ConfigDict(extra="forbid")

    produce_prd: bool = Field(default=True)
    produce_arch_note: bool = Field(default=True)
    decompose_into_stories: bool = Field(default=True)


class L4Settings(BaseModel):
    """Epic-level planning: decompose into child tasks re-queued in the task system."""

    model_config = ConfigDict(extra="forbid")

    auto_create_children: bool = Field(default=True)
    max_child_tasks: int = Field(default=20, ge=1, le=100)


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
    level_thresholds: dict[int, LevelConditions] = Field(
        default_factory=lambda: {
            0: LevelConditions(min_chars=0, max_chars=50),
            1: LevelConditions(min_chars=0, max_chars=400),
            2: LevelConditions(min_chars=200, required_keywords=["implement", "feature"]),
            3: LevelConditions(min_chars=500, file_count_estimate=5),
            4: LevelConditions(min_chars=1000, file_count_estimate=10),
        },
        description="Heuristic thresholds mapping level → classification conditions.",
    )
    l2_settings: L2Settings = Field(default_factory=L2Settings)
    l3_settings: L3Settings = Field(default_factory=L3Settings)
    l4_settings: L4Settings = Field(default_factory=L4Settings)


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


class BugFixerPolicy(BaseModel):
    """Default session limits for an autonomous Bug-Fixer run.

    This is the single config source the bf-api start endpoint and the bf-controller's
    run-controls both read, so an operator's per-session override and the controller's
    loop bounds resolve from one place rather than drifting apart. Region priorities are
    deliberately NOT here: they stay on :attr:`PlanningSettings.priorities` so the sweep
    scheduler and the planner keep sharing one ranking.

    ``wall_clock_budget_minutes`` defaults to ``None`` and *reuses*
    ``execution.wall_clock_budget_minutes`` (the per-task retry-loop cap) at resolution
    time, so a session inherits the same hard wall as ordinary tasks unless an operator
    narrows it for that run.
    """

    model_config = ConfigDict(extra="forbid")

    max_candidates: int = Field(
        default=10,
        ge=1,
        le=1000,
        description="Cap on bug candidates the controller pursues before the session ends.",
    )
    max_attempts: int = Field(
        default=3,
        ge=1,
        le=50,
        description="Auto-fix attempts per candidate before it declines to a human.",
    )
    wall_clock_budget_minutes: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Hard wall-clock cap on one session. None reuses "
            "execution.wall_clock_budget_minutes so a session shares the per-task cap."
        ),
    )


class BugFixerSessionOverride(BaseModel):
    """Per-session overrides carried on a Bug-Fixer start request.

    Every field is optional: a value present here wins over the project's
    :class:`BugFixerPolicy` default; an omitted (``None``) field falls back to it. This
    is the start-request payload bf-api validates before handing the controller a
    resolved :class:`BugFixerSessionConfig`. Bounds mirror the policy so an override can
    never widen a limit past what the policy itself permits.
    """

    model_config = ConfigDict(extra="forbid")

    max_candidates: int | None = Field(default=None, ge=1, le=1000)
    max_attempts: int | None = Field(default=None, ge=1, le=50)
    wall_clock_budget_minutes: int | None = Field(default=None, ge=0)


class BugFixerSessionConfig(BaseModel):
    """The fully-resolved limits a single Bug-Fixer session runs under.

    Produced by :func:`conclave.config.resolver.resolve_bug_fixer_session` from the
    project policy, the execution wall-clock default, and an optional per-session
    override. Every field is concrete — the wall-clock fallback is already applied — so
    the controller reads it without re-deriving precedence on each loop tick.
    """

    model_config = ConfigDict(extra="forbid")

    max_candidates: int = Field(ge=1, le=1000)
    max_attempts: int = Field(ge=1, le=50)
    wall_clock_budget_minutes: int = Field(ge=0)


class NotificationSettings(BaseModel):
    """Outbound notifications for terminal task events (done/failed).

    Inert by default: with no ``webhook_url`` set, no sink is built and nothing fires.
    When a URL is configured, a small JSON payload is POSTed best-effort on each terminal
    task — a delivery failure never affects task processing.
    """

    model_config = ConfigDict(extra="forbid")

    webhook_url: str | None = Field(
        default=None,
        description=(
            "If set, POST a small JSON payload to this URL when a task reaches a terminal "
            "state (done/failed). None disables notifications entirely."
        ),
    )
    timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=120,
        description="Per-request timeout for the webhook POST.",
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
    bug_fixer: BugFixerPolicy = Field(default_factory=BugFixerPolicy)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    protected: ProtectedSettings = Field(default_factory=ProtectedSettings)
    agent_defaults: AgentSettings = Field(default_factory=AgentSettings)
    agent_overrides: dict[str, dict[str, object]] = Field(default_factory=dict)
