"""Async SQLite database: connection, pragmas, the migration runner, and the write
serialization primitives.

A single shared :class:`aiosqlite.Connection` is used. aiosqlite serializes the
*execution* of individual operations through a dedicated thread, so a single
self-contained statement is safe to run from concurrent coroutines. A multi-statement
logical transaction is NOT: because every coroutine shares one connection — and therefore
one transaction context — one coroutine's ``commit`` would flush another coroutine's
half-written change, and concurrent read-modify-write sequences could interleave. To make
such sequences atomic, every write serializes through a single :class:`asyncio.Lock` and
explicit multi-statement transactions go through :meth:`Database.transaction`. WAL mode
lets readers proceed alongside the single writer.

The connection runs with ``isolation_level=None`` (driver autocommit): the sqlite3 driver
never opens transactions implicitly, so the only transactions that ever exist are the
explicit ``BEGIN``/``COMMIT``/``ROLLBACK`` issued by :meth:`transaction`. That keeps the
atomicity reasoning airtight — there is no hidden, driver-managed transaction that could
interleave with, or be flushed by, ours.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from ..util import now_iso
from .migrations import MIGRATIONS


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        # Serializes every write on the shared connection. One connection means one
        # transaction context, so without this a commit from coroutine A would flush
        # coroutine B's in-flight statements. Held around each single-statement write AND
        # the whole body of transaction(), so no commit can ever land mid-sequence.
        self._write_lock = asyncio.Lock()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected; call connect() first.")
        return self._conn

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None → driver autocommit: only our explicit BEGIN/COMMIT/ROLLBACK
        # (in transaction()) manage transactions, so nothing the driver does implicitly can
        # leave a stray transaction open across the shared connection.
        conn = await aiosqlite.connect(self._path, isolation_level=None)
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
            # Apply the migration body AND its schema_version bump in one explicit
            # transaction so they land all-or-nothing: a failure partway rolls the partial
            # DDL back and leaves the version unadvanced. Otherwise the partial DDL would
            # be committed but the version not bumped, so the next boot replays the same
            # migration and wedges the database (e.g. a duplicate-column error).
            # executescript cannot be used here — it issues an implicit COMMIT first, which
            # would defeat the surrounding BEGIN — so we run each statement individually
            # inside transaction() (BEGIN / COMMIT on success / ROLLBACK on any error).
            async with self.transaction() as tx:
                for stmt in _iter_sql_statements(mig.sql):
                    await tx.execute(stmt)
                await tx.execute(
                    "INSERT INTO schema_version(version, name, applied_at) VALUES (?, ?, ?)",
                    (mig.version, mig.name, now_iso()),
                )

    # --- query helpers (reads do not commit; writes serialize through the lock) ---

    @asynccontextmanager
    async def _write(self) -> AsyncIterator[aiosqlite.Connection]:
        """Hold the write lock for one self-contained statement, then commit.

        Serializes against every other writer (single-statement and :meth:`transaction`
        alike) so a commit can never flush another coroutine's in-flight work on the shared
        connection. Callers run exactly one logical statement inside the ``with`` block.
        """
        async with self._write_lock:
            conn = self.conn
            yield conn
            await conn.commit()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Run a multi-statement read-modify-write atomically on the shared connection.

        Acquires the write lock for the whole body, issues a single ``BEGIN``, yields the
        connection so the caller can run several statements, then ``COMMIT``s once on a
        clean exit or ``ROLLBACK``s every statement on any exception. The lock is always
        released.

        NOT re-entrant: issuing another :meth:`transaction` or any other write from inside
        an open transaction on the same :class:`Database` would deadlock on the lock. The
        composer functions that use this own their transaction end to end — pass them data,
        never an already-open connection.
        """
        async with self._write_lock:
            conn = self.conn
            await conn.execute("BEGIN")
            try:
                yield conn
            except BaseException:
                await conn.execute("ROLLBACK")
                raise
            else:
                await conn.execute("COMMIT")

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        async with self._write() as conn:
            await conn.execute(sql, params)

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


# --- Internal helpers ---


def _iter_sql_statements(script: str) -> list[str]:
    """Split a migration script into its individual SQL statements.

    Migrations are run statement-by-statement (via ``conn.execute``) inside an explicit
    transaction so the whole migration is atomic — ``executescript`` can't be used because
    it issues an implicit COMMIT before running, which would defeat the surrounding BEGIN.
    Splitting is driven by :func:`sqlite3.complete_statement` rather than a naive
    ``split(";")`` so a semicolon inside a string literal does not produce a spurious
    boundary: the buffer is only cut at a ``;`` once it forms a complete statement (an
    unterminated string literal keeps ``complete_statement`` False). A trailing statement
    without a final ``;`` is flushed too.
    """
    statements: list[str] = []
    buffer = ""
    for char in script:
        buffer += char
        if char == ";" and sqlite3.complete_statement(buffer):
            stmt = buffer.strip()
            if stmt:
                statements.append(stmt)
            buffer = ""
    tail = buffer.strip()
    if tail:
        statements.append(tail)
    return statements
