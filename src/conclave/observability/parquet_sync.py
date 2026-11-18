"""Record agent interaction logs in SQLite, and export them on request to an archive file.

The archive has the 23-column ``INTERACTION_LOGS`` schema used by ``sync_interaction_logs.py``,
so Conclave dispatches and OpenCode/Claude sessions share one analytical schema. It is
ZSTD-compressed Apache Parquet (``INTERACTION_LOGS.parquet``) when the optional ``pyarrow`` is
installed (``pip install -e ".[observability]"``), else JSON Lines (``INTERACTION_LOGS.jsonl``).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from ..db import Database, InteractionLog
from ..db import repositories as repo
from ..providers import AgentResult
from ..util import new_id

_ARCHIVE_STEM = "INTERACTION_LOGS"
# Raw rows read from the database, then converted and written in a worker thread, per step.
_EXPORT_PAGE_ROWS = 500


def _load_pyarrow() -> tuple[Any, Any] | None:
    """Dynamically load ``pyarrow`` and ``pyarrow.parquet`` without static stub coupling."""
    try:
        pa = importlib.import_module("pyarrow")
        pq = importlib.import_module("pyarrow.parquet")
        return pa, pq
    except ImportError:
        return None


def _default_home() -> Path:
    """``$CONCLAVE_HOME``, else ``~/.local/share/conclave``: the daemon's home."""
    override = os.environ.get("CONCLAVE_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".local" / "share" / "conclave"


def _parquet_schema(pa: Any) -> Any:
    return pa.schema(
        [
            ("session_id", pa.string()),
            ("title", pa.string()),
            ("directory", pa.string()),
            ("path", pa.string()),
            ("slug", pa.string()),
            ("model", pa.string()),
            ("agent", pa.string()),
            ("cost", pa.float64()),
            ("tokens_input", pa.int64()),
            ("tokens_output", pa.int64()),
            ("tokens_reasoning", pa.int64()),
            ("tokens_cache_read", pa.int64()),
            ("tokens_cache_write", pa.int64()),
            ("time_created", pa.int64()),
            ("time_updated", pa.int64()),
            ("created_at_str", pa.string()),
            ("updated_at_str", pa.string()),
            ("duration", pa.string()),
            ("tool_calls_count", pa.int64()),
            ("message_count", pa.int64()),
            ("user_prompt", pa.string()),
            ("tool_breakdown", pa.string()),
            ("messages", pa.string()),
        ]
    )


def _archive_record(row: InteractionLog) -> dict[str, Any]:
    """One archive row: the 23 columns of the ``INTERACTION_LOGS`` schema."""
    return {
        "session_id": str(row.session_id),
        "title": str(row.title),
        "directory": str(row.directory),
        "path": str(row.path or row.directory),
        "slug": str(row.slug),
        "model": str(row.model),
        "agent": str(row.agent),
        "cost": float(row.cost),
        "tokens_input": int(row.tokens_input),
        "tokens_output": int(row.tokens_output),
        "tokens_reasoning": int(row.tokens_reasoning),
        "tokens_cache_read": int(row.tokens_cache_read),
        "tokens_cache_write": int(row.tokens_cache_write),
        "time_created": int(row.time_created),
        "time_updated": int(row.time_updated),
        "created_at_str": str(row.created_at_str),
        "updated_at_str": str(row.updated_at_str),
        "duration": f"{row.duration:.1f}s",
        "tool_calls_count": int(row.tool_calls_count),
        "message_count": int(row.message_count),
        "user_prompt": str(row.user_prompt),
        "tool_breakdown": json.dumps(row.tool_breakdown, ensure_ascii=False),
        "messages": json.dumps(row.messages, ensure_ascii=False),
    }


class _ArchiveWriter:
    """Writes one archive into a hidden temporary file beside it, then renames that over it.

    Blocking file work: it is built and every method runs in a worker thread, never on the
    event loop.
    """

    def __init__(self, target_path: Path | None, home: Path | None) -> None:
        pyarrow = _load_pyarrow()
        self.format = "jsonl" if pyarrow is None else "parquet"
        base = target_path if target_path is not None else (home or _default_home()) / _ARCHIVE_STEM
        # The suffix always names the content: never JSON Lines under a .parquet name.
        self.path = base.with_suffix(f".{self.format}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.rows = 0
        # Unique, so two exports never write into one file; hidden, as it is not the archive.
        self._tmp = self.path.with_name(f".{self.path.name}.{new_id()}.tmp")
        self._jsonl: IO[str] | None = None
        self._parquet: Any = None
        if pyarrow is None:
            self._jsonl = open(self._tmp, "w", encoding="utf-8", newline="\n")
        else:
            pa, pq = pyarrow
            self._pa = pa
            self._schema = _parquet_schema(pa)
            self._parquet = pq.ParquetWriter(str(self._tmp), self._schema, compression="ZSTD")

    def write(self, raw_rows: list[Any]) -> None:
        """Convert a page of raw ``interaction_logs`` rows and append it to the file."""
        records = [_archive_record(InteractionLog.from_row(row)) for row in raw_rows]
        if self._jsonl is not None:
            self._jsonl.writelines(json.dumps(rec, ensure_ascii=False) + "\n" for rec in records)
        else:
            self._parquet.write_table(self._pa.Table.from_pylist(records, schema=self._schema))
        self.rows += len(records)

    def finish(self) -> int:
        """Close the file and rename it over the archive; return the archive's size in bytes."""
        if self._jsonl is not None:
            self._jsonl.close()
        else:
            if self.rows == 0:  # still a Parquet file with the schema
                self._parquet.write_table(self._schema.empty_table())
            self._parquet.close()
        os.replace(self._tmp, self.path)
        return self.path.stat().st_size

    def discard(self) -> None:
        """Close and delete the temporary file; the archive in place, if any, stays as it was."""
        with contextlib.suppress(Exception):
            (self._jsonl if self._jsonl is not None else self._parquet).close()
        self._tmp.unlink(missing_ok=True)


async def export_interaction_logs_to_parquet(
    db: Database, target_path: Path | None = None, *, home: Path | None = None
) -> dict[str, Any]:
    """Write every ``interaction_logs`` row into the archive file, replacing it atomically, and
    return ``{ok, path, rows, format, size_bytes}``.

    The format is ``parquet`` when ``pyarrow`` is importable, else ``jsonl``, and the file's
    suffix is always ``.<format>``: *target_path* with that suffix, else
    ``INTERACTION_LOGS.<format>`` in *home* (the daemon's home, ``$CONCLAVE_HOME`` when not
    given). Rows are read in pages, without a cap, and converted and written in a worker
    thread, never on the event loop. The name predates the JSON Lines format; the REST and MCP
    callers keep it.
    """
    writer = await asyncio.to_thread(_ArchiveWriter, target_path, home)
    try:
        after: int | None = None
        while page := await repo.interaction_log_rows_after(db, after, _EXPORT_PAGE_ROWS):
            await asyncio.to_thread(writer.write, page)
            after = int(page[-1]["row_key"])
        size = await asyncio.to_thread(writer.finish)
    except Exception:
        # Not on cancellation: a write may still run in its thread then, and closing the file
        # under it is unsafe, so that hidden temporary file stays behind.
        await asyncio.to_thread(writer.discard)
        raise
    return {
        "ok": True,
        "path": str(writer.path),
        "rows": writer.rows,
        "format": writer.format,
        "size_bytes": size,
    }


async def log_agent_interaction(
    db: Database,
    *,
    session_id: str | None = None,
    title: str,
    directory: str,
    model: str,
    agent: str,
    prompt: str,
    result: AgentResult,
    duration_seconds: float,
    project_id: str | None = None,
    task_id: str | None = None,
    node_id: str | None = None,
    source: str = "conclave",
) -> InteractionLog:
    """Record a completed agent dispatch into ``interaction_logs``.

    Only the row: the archive file is written on request
    (:func:`export_interaction_logs_to_parquet`), never per dispatch.
    """
    sid = session_id or result.session_id or new_id()
    now_ms = int(time.time() * 1000)
    dur_sec = max(0.0, float(duration_seconds))
    time_created = now_ms - int(dur_sec * 1000)
    time_updated = now_ms

    created_str = datetime.fromtimestamp(time_created / 1000.0, tz=UTC).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    updated_str = datetime.fromtimestamp(time_updated / 1000.0, tz=UTC).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    effective_model = result.model_reported or model or "opencode/default"
    assistant_text = result.text or (f"[Error: {result.error}]" if result.error else "")

    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "time": created_str,
            "text": prompt,
            "tools": [],
        },
        {
            "role": "assistant",
            "time": updated_str,
            "model": effective_model,
            "tokens": f"{result.input_tokens or 0} in / {result.output_tokens or 0} out",
            "text": assistant_text,
            "tools": [
                {"name": k, "count": v, "input": ""}
                for k, v in result.tool_breakdown.items()
            ],
        },
    ]

    tool_calls_count = result.tool_calls_count or sum(result.tool_breakdown.values())
    entry = await repo.record_interaction_log(
        db,
        session_id=sid,
        title=title,
        directory=directory,
        path=directory,
        slug=f"{agent}-{sid[:8]}",
        model=effective_model,
        agent=agent,
        cost=float(result.cost_usd or 0.0),
        tokens_input=int(result.input_tokens or 0),
        tokens_output=int(result.output_tokens or 0),
        tokens_reasoning=int(result.reasoning_tokens or 0),
        tokens_cache_read=int(result.cache_read_tokens or 0),
        tokens_cache_write=int(result.cache_creation_tokens or 0),
        time_created=time_created,
        time_updated=time_updated,
        created_at_str=created_str,
        updated_at_str=updated_str,
        duration=dur_sec,
        tool_calls_count=int(tool_calls_count),
        message_count=len(messages),
        user_prompt=prompt[:2000],
        tool_breakdown=dict(result.tool_breakdown),
        messages=messages,
        project_id=project_id,
        task_id=task_id,
        node_id=node_id,
        source=source,
    )
    return entry
