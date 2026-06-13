"""Typed, layered configuration for Conclave."""

from __future__ import annotations

from .models import (
    AgentRole,
    AgentSettings,
    AgentsPolicy,
    ArgMode,
    ConclaveConfig,
    ConditionalAgent,
    DeclineConsensus,
    Effort,
    ExecutionSettings,
    ExperimentalSettings,
    PlanningSettings,
    ProtectedSettings,
    Verdict,
)
from .resolver import (
    deep_merge,
    effective_protected,
    load_project_config,
    resolve_agent,
)
from .schema import agent_schema, config_schema, default_config_dict

__all__ = [
    "AgentRole",
    "AgentSettings",
    "AgentsPolicy",
    "ArgMode",
    "ConclaveConfig",
    "ConditionalAgent",
    "DeclineConsensus",
    "Effort",
    "ExecutionSettings",
    "ExperimentalSettings",
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
]
