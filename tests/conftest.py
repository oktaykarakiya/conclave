"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest_asyncio

from conclave.db import Database


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(tmp_path / "conclave.db")
    await database.connect()
    try:
        yield database
    finally:
        await database.close()
