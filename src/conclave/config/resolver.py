"""Layered configuration resolution.

Three layers compose, later overriding earlier:

1. built-in defaults — :class:`ConclaveConfig` instantiated with no args;
2. per-project overrides — a sparse dict deep-merged onto the defaults;
3. per-agent overrides — ``agent_overrides[name]`` deep-merged onto ``agent_defaults``.

This mirrors team-ai's ``models.overrides.{agent}`` → ``models.default`` semantics,
but fully typed and validated.
"""

from __future__ import annotations

from typing import Any

from .models import (
    AgentSettings,
    BugFixerSessionConfig,
    BugFixerSessionOverride,
    ConclaveConfig,
)

# Safety floor: these are always protected regardless of project config. Users may
# ADD protected paths via config but can never remove these (closes a team-ai footgun).
PROTECTED_FLOOR_FILES = ("*.env", "*.env.*", ".env")
PROTECTED_FLOOR_DIRS = (".git",)


def _unquote_key(k: str) -> str:
    cleaned = k.strip()
    while len(cleaned) >= 2:
        if (cleaned.startswith('"') and cleaned.endswith('"')) or (
            cleaned.startswith("'") and cleaned.endswith("'")
        ):
            cleaned = cleaned[1:-1].strip()
        elif (cleaned.startswith('\\"') and cleaned.endswith('\\"') and len(cleaned) >= 4) or (
            cleaned.startswith("\\'") and cleaned.endswith("\\'") and len(cleaned) >= 4
        ):
            cleaned = cleaned[2:-2].strip()
        else:
            break
    return cleaned or k


def normalize_config_keys(value: Any) -> Any:
    """Recursively strip accidental surrounding quotes from dictionary keys (e.g.
    ``'"architect-as-planner"'`` -> ``'architect-as-planner'``)."""
    if isinstance(value, dict):
        return {
            (_unquote_key(k) if isinstance(k, str) else k): normalize_config_keys(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [normalize_config_keys(item) for item in value]
    return value


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto ``base`` without mutating either.

    Dict values are merged key-by-key; every other type (including lists) is replaced
    wholesale by the override value.
    """
    norm_base = normalize_config_keys(base)
    norm_override = normalize_config_keys(override)
    result: dict[str, Any] = dict(norm_base)
    for key, value in norm_override.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = deep_merge(existing, value)
        else:
            result[key] = value
    return result


def load_project_config(overrides: dict[str, Any] | None = None) -> ConclaveConfig:
    """Build a validated :class:`ConclaveConfig` from built-in defaults + project overrides."""
    base = ConclaveConfig().model_dump(mode="python")
    norm_overrides = normalize_config_keys(overrides) if overrides else None
    merged = deep_merge(base, norm_overrides) if norm_overrides else base
    return ConclaveConfig.model_validate(merged)


def resolve_agent(config: ConclaveConfig, agent: str) -> AgentSettings:
    """Resolve the effective :class:`AgentSettings` for ``agent``.

    ``agent_defaults`` provides the base; any ``agent_overrides[agent]`` keys win.
    """
    base = config.agent_defaults.model_dump(mode="python")
    clean_agent = _unquote_key(agent)
    norm_overrides = normalize_config_keys(config.agent_overrides)
    override = norm_overrides.get(clean_agent) or config.agent_overrides.get(agent, {})
    merged = deep_merge(base, dict(override)) if override else base
    return AgentSettings.model_validate(merged)


def resolve_bug_fixer_session(
    config: ConclaveConfig,
    override: BugFixerSessionOverride | None = None,
) -> BugFixerSessionConfig:
    """Resolve the effective limits for one Bug-Fixer session.

    Mirrors :func:`resolve_agent`'s layered shape: a per-session ``override`` wins,
    falling back to the project's ``bug_fixer`` policy default. ``wall_clock_budget_minutes``
    adds one final fallback — when neither the override nor the policy pins it, the session
    reuses ``execution.wall_clock_budget_minutes`` so it shares the per-task retry-loop cap
    rather than running unbounded.
    """
    policy = config.bug_fixer
    ov = override or BugFixerSessionOverride()
    budget = ov.wall_clock_budget_minutes
    if budget is None:
        budget = policy.wall_clock_budget_minutes
    if budget is None:
        budget = config.execution.wall_clock_budget_minutes
    return BugFixerSessionConfig(
        max_candidates=(
            ov.max_candidates if ov.max_candidates is not None else policy.max_candidates
        ),
        max_attempts=(ov.max_attempts if ov.max_attempts is not None else policy.max_attempts),
        wall_clock_budget_minutes=budget,
    )


def effective_protected(config: ConclaveConfig) -> tuple[list[str], list[str]]:
    """Return ``(file_globs, dir_globs)`` with the safety floor always included."""
    files = list(dict.fromkeys([*PROTECTED_FLOOR_FILES, *config.protected.files]))
    dirs = list(dict.fromkeys([*PROTECTED_FLOOR_DIRS, *config.protected.directories]))
    return files, dirs
