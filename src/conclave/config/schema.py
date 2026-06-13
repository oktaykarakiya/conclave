"""JSON Schema export so the web UI can auto-render config forms.

The frontend fetches these schemas and renders typed fields (with defaults, ranges,
enums, and descriptions) without a bespoke form per setting — keeping the UI the
sole config editor.
"""

from __future__ import annotations

from typing import Any

from .models import AgentSettings, ConclaveConfig


def config_schema() -> dict[str, Any]:
    """JSON Schema for the full project configuration."""
    return ConclaveConfig.model_json_schema()


def agent_schema() -> dict[str, Any]:
    """JSON Schema for per-agent settings."""
    return AgentSettings.model_json_schema()


def default_config_dict() -> dict[str, Any]:
    """The built-in default configuration as a plain dict (for seeding new projects)."""
    return ConclaveConfig().model_dump(mode="json")
