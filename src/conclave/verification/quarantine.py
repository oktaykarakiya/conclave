"""Green-gate integrity from the quarantine table.

The quarantine replaces team-ai's silently-eroding ``accepted_failures``: every entry
carries a mandatory ``until`` date, and expiry is enforced in code — an expired entry
no longer counts as accepted and is surfaced as a health regression. Agents may not
self-add entries (operator-only, via the API); they merely report failures.
"""

from __future__ import annotations

from typing import Any

from ..db import Database
from ..db import repositories as repo
from ..util import today_iso


async def quarantine_integrity(
    db: Database, project_id: str, *, today: str | None = None
) -> dict[str, Any]:
    """Summarize quarantine health: totals, what is still active, and what has expired."""
    as_of = today or today_iso()
    entries = await repo.list_quarantine(db, project_id)
    active = [q for q in entries if q.until >= as_of]
    expired = [q for q in entries if q.until < as_of]
    return {
        "as_of": as_of,
        "total": len(entries),
        "active": len(active),
        "expired": len(expired),
        "expired_patterns": [q.pattern for q in expired],
        "healthy": len(expired) == 0,
    }
