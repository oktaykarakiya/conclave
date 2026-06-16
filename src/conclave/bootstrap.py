"""Seed global default agent personas on first run.

Idempotent and non-destructive — only inserts what is missing, so operator edits to
personas in the UI are preserved across restarts. Model/provider selection is owned by
opencode, so there is no engine profile to seed.
"""

from __future__ import annotations

from .agents import DEFAULT_PERSONAS
from .db import Database
from .db import repositories as repo


async def seed_global_defaults(db: Database) -> None:
    for name, (role, persona_md) in DEFAULT_PERSONAS.items():
        if await repo.get_agent(db, name) is None:
            await repo.upsert_agent(db, name=name, role=role.value, persona_md=persona_md)
