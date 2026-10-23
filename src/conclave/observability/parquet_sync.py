"""Synchronize Conclave agent interaction logs to SQLite and ZSTD-compressed Apache Parquet.

Matches the 23-column ``INTERACTION_LOGS.parquet`` schema used by ``sync_interaction_logs.py``
so Conclave dispatches and OpenCode/Claude sessions share one unified analytical schema.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..db import Database, InteractionLog
from ..db import repositories as repo
from ..providers import AgentResult
from ..util import new_id

logger = logging.getLogger("conclave.observability.parquet_sync")


def _load_pyarrow() -> tuple[Any, Any] | None:
    """Dynamically load ``pyarrow`` and ``pyarrow.parquet`` without static stub coupling."""
    try:
        pa = importlib.import_module("pyarrow")
        pq = importlib.import_module("pyarrow.parquet")
        return pa, pq
    except ImportError:
        return None


def default_parquet_paths() -> list[Path]:
    """Return the default target paths for ``INTERACTION_LOGS.parquet``."""
    home_dir = Path(
        os.environ.get("CONCLAVE_HOME", str(Path.home() / ".local" / "share" / "conclave"))
    )
    paths: list[Path] = [home_dir / "INTERACTION_LOGS.parquet"]
    if (
        os.environ.get("CONCLAVE_PARQUET_SYNC") != "0"
        and "PYTEST_CURRENT_TEST" not in os.environ
    ):
        cwd = Path.cwd()
        if cwd.is_dir() and (cwd / "pyproject.toml").is_file():
            cwd_target = cwd / "INTERACTION_LOGS.parquet"
            if cwd_target not in paths:
                paths.append(cwd_target)
    return paths


async def export_interaction_logs_to_parquet(
    db: Database, target_path: Path | None = None
) -> dict[str, Any]:
    """Export all ``interaction_logs`` rows into the 23-column ZSTD Parquet archive."""
    logs = await repo.list_interaction_logs(db, limit=10_000)

    if target_path is not None:
        targets = [target_path]
    elif "PYTEST_CURRENT_TEST" in os.environ:
        targets = [db._path.parent / "INTERACTION_LOGS.parquet"]
    else:
        targets = default_parquet_paths()

    out_path = targets[0]
    records: list[dict[str, Any]] = [
        {
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
        for row in logs
    ]

    loaded = _load_pyarrow()
    if loaded is None:
        payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"
        for dest in targets:
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = dest.with_name(f"{dest.name}.tmp")
            tmp_file.write_text(payload, encoding="utf-8")
            os.replace(tmp_file, dest)
        return {
            "ok": True,
            "path": str(out_path),
            "rows": len(logs),
            "size_bytes": out_path.stat().st_size if out_path.is_file() else 0,
            "format": "jsonl-fallback",
        }

    pa, pq = loaded
    schema = pa.schema(
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

    table = pa.Table.from_pylist(records, schema=schema)
    for dest in targets:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = dest.with_name(f"{dest.name}.tmp")
        pq.write_table(table, str(tmp_file), compression="ZSTD")
        os.replace(tmp_file, dest)

    return {
        "ok": True,
        "path": str(out_path),
        "rows": len(logs),
        "size_bytes": out_path.stat().st_size,
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
    """Record a completed agent dispatch into ``interaction_logs`` and sync Parquet."""
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

    if "PYTEST_CURRENT_TEST" not in os.environ:
        try:
            await export_interaction_logs_to_parquet(db)
        except Exception:
            logger.debug("background parquet export failed", exc_info=True)

    return entry
