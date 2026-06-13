"""Small shared utilities."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string (sorts lexicographically by time)."""
    return datetime.now(UTC).isoformat()


def today_iso() -> str:
    """Current UTC date as ``YYYY-MM-DD``."""
    return datetime.now(UTC).date().isoformat()


def new_id() -> str:
    """A fresh opaque identifier."""
    return uuid.uuid4().hex
