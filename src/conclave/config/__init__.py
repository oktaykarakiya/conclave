"""Typed, layered configuration for Conclave."""

from __future__ import annotations

from .models import (
    AgentRole,
    AgentSettings,
    AgentsPolicy,
    ArgMode,
    BugFixerPolicy,
    BugFixerSessionConfig,
    BugFixerSessionOverride,
    ConclaveConfig,
    ConditionalAgent,
    DeclineConsensus,
    Effort,
    ExecutionSettings,
    ExperimentalSettings,
    NotificationSettings,
    PlanningSettings,
    ProtectedSettings,
    Verdict,
)
from .resolver import (
    deep_merge,
    effective_protected,
    load_project_config,
    resolve_agent,
    resolve_bug_fixer_session,
)
from .schema import agent_schema, config_schema, default_config_dict

__all__ = [
    "AgentRole",
    "AgentSettings",
    "AgentsPolicy",
    "ArgMode",
    "BugFixerPolicy",
    "BugFixerSessionConfig",
    "BugFixerSessionOverride",
    "ConclaveConfig",
    "ConditionalAgent",
    "DeclineConsensus",
    "Effort",
    "ExecutionSettings",
    "ExperimentalSettings",
    "NotificationSettings",
    "PlanningSettings",
    "ProtectedSettings",
    "Verdict",
    "agent_schema",
    "config_schema",
    "deep_merge",
    "default_config_dict",
    "effective_protected",
    "load_project_config",
    "resolve_agent",
    "resolve_bug_fixer_session",
]
