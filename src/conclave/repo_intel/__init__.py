"""Autonomous repo intelligence: onboarding analysis + knowledge model."""

from __future__ import annotations

from .analyst import ai_enrich, analyze_repo, onboard
from .knowledge import RepoKnowledge, manifest_fingerprint, render_preamble

__all__ = [
    "RepoKnowledge",
    "ai_enrich",
    "analyze_repo",
    "manifest_fingerprint",
    "onboard",
    "render_preamble",
]
