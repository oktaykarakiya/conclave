"""Async SQLite database: connection, pragmas, and the migration runner.

A single shared :class:`aiosqlite.Connection` is used. aiosqlite serializes all
operations on a connection through a dedicated thread, so concurrent coroutines are
safe; WAL mode lets readers proceed alongside the single writer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import aiosqlite

from ..util import now_iso
from .migrations import MIGRATIONS


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected; call connect() first.")
        return self._conn

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        self._conn = conn
        await self._apply_migrations()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _apply_migrations(self) -> None:
        conn = self.conn
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        await conn.commit()
        cur = await conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_version")
        row = await cur.fetchone()
        current = int(row["v"]) if row is not None else 0
        for mig in sorted(MIGRATIONS, key=lambda m: m.version):
            if mig.version <= current:
                continue
            await conn.executescript(mig.sql)
            await conn.execute(
                "INSERT INTO schema_version(version, name, applied_at) VALUES (?, ?, ?)",
                (mig.version, mig.name, now_iso()),
            )
            await conn.commit()

    # --- query helpers (reads do not commit; writes commit) ---

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        await self.conn.execute(sql, params)
        await self.conn.commit()

    async def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> Any | None:
        cur = await self.conn.execute(sql, params)
        return await cur.fetchone()

    async def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[Any]:
        cur = await self.conn.execute(sql, params)
        return list(await cur.fetchall())

    async def fetchval(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        row = await self.fetchone(sql, params)
        if row is None:
            return None
        return row[0]
