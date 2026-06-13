"""Seed global defaults on first run: the system-default engine profile + personas.

Idempotent and non-destructive — only inserts what is missing, so operator edits to
personas/profiles in the UI are preserved across restarts.
"""

from __future__ import annotations

from .agents import DEFAULT_PERSONAS
from .db import Database
from .db import repositories as repo


async def seed_global_defaults(db: Database) -> None:
    if await repo.get_engine_profile(db, "system-default") is None:
        await repo.upsert_engine_profile(db, name="system-default", arg_mode="inherit")
    for name, (role, persona_md) in DEFAULT_PERSONAS.items():
        if await repo.get_agent(db, name) is None:
            await repo.upsert_agent(db, name=name, role=role.value, persona_md=persona_md)
