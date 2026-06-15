"""Verification governance: green-gate integrity (expiry-enforced quarantine)."""

from __future__ import annotations

from ..engine.gate import inject_quarantine_exclusions
from .quarantine import quarantine_integrity

__all__ = ["inject_quarantine_exclusions", "quarantine_integrity"]
