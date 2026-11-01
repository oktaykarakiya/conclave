"""Deterministic, zero-LLM context compaction for tasks and multi-agent planning sessions."""

from __future__ import annotations

import json
import re
from typing import Any

from ..db import CheckpointStatus, Database
from ..db import repositories as repo
from ..util import new_id, now_iso

_SALIENT_LINE_RE = re.compile(
    r"("
    r"^\s*[-*•]\s+|"
    r"^\s*\d+\.\s+|"
    r"^\s*#+ |"
    r"```|"
    r'"verdict"\s*:|'
    r'"file"\s*:|'
    r'"action"\s*:|'
    r"\b(?:PASS|FAIL|BLOCK|DECLINE|ERROR|WARNING)\b|"
    r"\b[\w./\\-]+\.(?:py|ts|tsx|js|jsx|json|sql|sh|md|toml|yaml|yml)\b|"
    r"\b(?:pytest|mypy|ruff|npm|npx|git|cargo)\b"
    r")",
    re.IGNORECASE,
)


def compact_prompt_section(label: str, text: str, max_chars: int = 3200) -> str:
    """Deterministically compact *text* to at most *max_chars* while preserving high-signal lines.

    If ``len(text) <= max_chars``, returns *text* unchanged. Otherwise preserves the leading
    header/summary, salient middle lines (JSON/code blocks, file paths, commands, verdicts,
    bullet points), and the trailing tail with a ``[... compacted N chars -> M chars ...]`` marker.
    """
    orig_len = len(text)
    if orig_len <= max_chars:
        return text

    marker_reserve = 96
    budget = max(80, max_chars - marker_reserve)
    head_budget = max(30, int(budget * 0.40))
    mid_budget = max(20, int(budget * 0.25))
    tail_budget = max(30, budget - head_budget - mid_budget)

    head = text[:head_budget].rstrip()
    tail = text[-tail_budget:].lstrip()
    middle_region = text[head_budget : max(head_budget, orig_len - tail_budget)]

    salient_lines: list[str] = []
    salient_chars = 0
    for raw_line in middle_region.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if _SALIENT_LINE_RE.search(stripped):
            line_clipped = stripped[:180]
            if salient_chars + len(line_clipped) + 1 > mid_budget:
                break
            salient_lines.append(line_clipped)
            salient_chars += len(line_clipped) + 1

    salient_block = "\n".join(salient_lines)
    prefix = f"{head}\n"
    if salient_block:
        prefix += f"{salient_block}\n"
    suffix = f"\n{tail}"

    # Compute exact final character count including the marker itself.
    # Two-pass calculation ensures M in "[... compacted N chars -> M chars ...]" is exact.
    base_len = len(prefix) + len(suffix)
    approx_total = base_len + len(f"[... compacted {orig_len} chars -> {base_len + 40} chars ...]")
    marker = f"[... compacted {orig_len} chars -> {approx_total} chars ...]"
    final_text = f"{prefix}{marker}{suffix}"
    exact_len = len(final_text)
    if exact_len != approx_total:
        marker = f"[... compacted {orig_len} chars -> {exact_len} chars ...]"
        final_text = f"{prefix}{marker}{suffix}"

    return final_text


def _compact_plan_dict(plan: dict[str, Any], max_str_chars: int = 400) -> dict[str, Any]:
    """Recursively compact verbose string fields inside a task plan dictionary."""
    compacted: dict[str, Any] = {}
    for key, val in plan.items():
        if isinstance(val, str) and len(val) > max_str_chars:
            compacted[key] = compact_prompt_section(key, val, max_chars=max_str_chars)
        elif isinstance(val, list):
            new_list: list[Any] = []
            for item in val[:25]:
                if isinstance(item, str) and len(item) > max_str_chars:
                    new_list.append(compact_prompt_section(key, item, max_chars=max_str_chars))
                elif isinstance(item, dict):
                    new_list.append(_compact_plan_dict(item, max_str_chars=max_str_chars))
                else:
                    new_list.append(item)
            compacted[key] = new_list
        elif isinstance(val, dict):
            compacted[key] = _compact_plan_dict(val, max_str_chars=max_str_chars)
        else:
            compacted[key] = val
    return compacted


async def compact_task_context(db: Database, task_id: str) -> dict[str, Any]:
    """Record a compacted summary of a task's context and its estimated size savings.

    Writes only a ``compaction`` checkpoint (summary and statistics). The task's request and
    plan are never rewritten, so agents always receive them whole.
    """
    task = await repo.get_task(db, task_id)
    if task is None:
        raise ValueError(f"task not found: {task_id}")

    checkpoints = await repo.list_task_checkpoints(db, task_id)
    verdicts = await repo.list_verdicts(db, task_id)

    plan_before = json.dumps(task.plan, ensure_ascii=False) if task.plan is not None else ""
    cp_before = json.dumps([cp.artifact for cp in checkpoints], ensure_ascii=False)
    verdicts_before = json.dumps(
        [{"agent": v.agent, "verdict": v.verdict, "reason": v.reason} for v in verdicts],
        ensure_ascii=False,
    )
    combined_before = "\n".join(
        part for part in (task.request, plan_before, cp_before, verdicts_before) if part
    )
    chars_before = len(combined_before)

    compacted_plan = (
        _compact_plan_dict(task.plan, max_str_chars=300) if task.plan is not None else None
    )
    plan_after = (
        json.dumps(compacted_plan, ensure_ascii=False) if compacted_plan is not None else ""
    )
    compacted_summary = compact_prompt_section(
        "TASK CONTEXT",
        combined_before,
        max_chars=min(2400, max(200, int(chars_before * 0.6))),
    )
    chars_after = min(chars_before, len(compacted_summary) + len(plan_after) // 2)
    tokens_saved_est = max(0, (chars_before - chars_after) // 4)

    await repo.save_task_checkpoint(
        db,
        task_id=task_id,
        node_id="compaction",
        attempt=0,
        status=CheckpointStatus.completed,
        artifact={
            "chars_before": chars_before,
            "chars_after": chars_after,
            "tokens_before_est": chars_before // 4,
            "tokens_after_est": chars_after // 4,
            "tokens_saved_est": tokens_saved_est,
            "summary": compacted_summary[:1000],
        },
    )

    return {
        "task_id": task_id,
        "chars_before": chars_before,
        "chars_after": chars_after,
        "tokens_saved_est": tokens_saved_est,
        "compacted": True,
    }


async def compact_planning_session_context(db: Database, session_id: str) -> dict[str, Any]:
    """Summarize earlier planning turns into a single ``[Compacted Session Digest]`` message."""
    session = await repo.get_planning_session(db, session_id)
    if session is None:
        raise ValueError(f"planning session not found: {session_id}")

    messages = await repo.list_planning_messages(db, session_id)
    task_nodes = await repo.list_planning_task_nodes(db, session_id)
    chars_before = sum(len(m.content) for m in messages)

    if len(messages) <= 4:
        return {
            "session_id": session_id,
            "messages_before": len(messages),
            "messages_after": len(messages),
            "chars_before": chars_before,
            "chars_after": chars_before,
            "tokens_saved_est": 0,
            "compacted": False,
        }

    earlier = messages[:-3]
    recent = messages[-3:]

    digest_parts: list[str] = ["[Compacted Session Digest]"]
    for msg in earlier:
        snippet = compact_prompt_section(
            f"{msg.agent}-t{msg.turn_number}", msg.content.strip(), max_chars=240
        )
        digest_parts.append(f"- Turn {msg.turn_number} ({msg.agent}): {snippet}")

    if task_nodes:
        digest_parts.append(
            f"Active task nodes ({len(task_nodes)}): "
            + ", ".join(n.title for n in task_nodes[:15])
        )

    digest_text = compact_prompt_section(
        "SESSION DIGEST", "\n".join(digest_parts), max_chars=1600
    )
    if not digest_text.startswith("[Compacted Session Digest]"):
        digest_text = "[Compacted Session Digest]\n" + digest_text

    digest_id = new_id()
    digest_turn = earlier[0].turn_number
    earlier_ids = [m.id for m in earlier]
    placeholders = ", ".join("?" for _ in earlier_ids)

    async with db.transaction() as conn:
        await conn.execute(
            f"DELETE FROM planning_messages WHERE id IN ({placeholders})",
            tuple(earlier_ids),
        )
        await conn.execute(
            "INSERT INTO planning_messages("
            "id, session_id, agent, role, content, turn_number, "
            "parent_id, metadata_json, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                digest_id,
                session_id,
                "planner",
                "agent",
                digest_text,
                digest_turn,
                None,
                json.dumps({"compacted": True, "turns_merged": len(earlier)}),
                now_iso(),
            ),
        )

    chars_after = len(digest_text) + sum(len(m.content) for m in recent)
    tokens_saved_est = max(0, (chars_before - chars_after) // 4)
    return {
        "session_id": session_id,
        "messages_before": len(messages),
        "messages_after": 1 + len(recent),
        "chars_before": chars_before,
        "chars_after": chars_after,
        "tokens_saved_est": tokens_saved_est,
        "compacted": True,
    }


compact_planning_session = compact_planning_session_context
