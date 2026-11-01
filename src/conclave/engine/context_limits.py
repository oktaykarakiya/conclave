"""Fit recoverable bulk into agent prompts without hiding a cut.

Irreplaceable content (the task request, the plan, reviewer feedback, operator steer) is never
cut. Recoverable bulk (the current diff, the previous attempt's diff, gate/test output and setup
output) can be regenerated in the agent's working directory, so a block longer than its limit
keeps its head and tail, and a marker in place of the omitted middle says how much was omitted,
which setting limited it and how to get the rest.

The limit for a dispatch is ``context.compaction_max_chars`` when compaction resolves on for the
agent and ``context.bulk_max_chars`` otherwise. ``compact_before_start`` resolves with the
precedence workflow node > ``agent_overrides.<agent>`` > ``agent_defaults`` >
``context.compaction_enabled``; ``None`` at a layer defers to the next one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..config import ConclaveConfig, resolve_agent

BulkBlock = Literal["diff", "previous_diff", "gate_output", "setup_output"]
LimitField = Literal["compaction_max_chars", "bulk_max_chars"]

# Share of the budget kept from the start of a block; the rest is kept from its end, where test
# runners print their summary.
_HEAD_SHARE = 0.4
# Room for the two newlines that put the marker on its own line, so a fitted block is never
# longer than its limit plus the marker.
_SEPARATOR_CHARS = 2


@dataclass(frozen=True)
class BulkLimit:
    """The per-block character limit for one dispatch and the setting it comes from."""

    field: LimitField
    limit: int


@dataclass(frozen=True)
class FitResult:
    """A block fitted to its limit; ``kept_chars`` counts the original characters kept."""

    text: str
    original_chars: int
    kept_chars: int

    @property
    def truncated(self) -> bool:
        return self.kept_chars < self.original_chars


def resolve_bulk_limit(
    config: ConclaveConfig, agent: str | None, node_compact: bool | None = None
) -> BulkLimit:
    """Resolve the bulk limit for a dispatch of *agent* (``None`` when no agent reads the block).

    *node_compact* is the workflow node's ``compact_before_start``. The first explicit
    ``True``/``False`` among the node, ``agent_overrides.<agent>`` and ``agent_defaults`` decides;
    when all are ``None`` (or there is no agent), ``context.compaction_enabled`` does.
    """
    compact = node_compact
    if compact is None and agent is not None:
        compact = resolve_agent(config, agent).compact_before_start
        if compact is None:
            # resolve_agent lets an explicit null in agent_overrides.<agent> replace the
            # agent_defaults value; null means "inherit", so defer to agent_defaults.
            compact = config.agent_defaults.compact_before_start
    if compact is None:
        compact = config.context.compaction_enabled
    if compact:
        return BulkLimit("compaction_max_chars", config.context.compaction_max_chars)
    return BulkLimit("bulk_max_chars", config.context.bulk_max_chars)


def fit_block(text: str, *, limit: int, field: LimitField, how_to_get_rest: str) -> FitResult:
    """Fit *text* to *limit* characters, keeping its head and tail around a marker.

    Text at or below the limit is returned unchanged. Above it, about 40% of the budget is kept
    from the start and 60% from the end. Both cuts fall on line boundaries unless that would
    drop more than half of that side's budget. The marker
    ``[... {omitted} of {total} chars omitted (limit: context.{field} = {limit}).
    {how_to_get_rest} ...]`` replaces the middle on a line of its own, so the result is at most
    ``limit + len(marker)`` characters long.
    """
    total = len(text)
    if total <= limit:
        return FitResult(text=text, original_chars=total, kept_chars=total)

    head_budget = int(limit * _HEAD_SHARE)
    tail_budget = max(0, limit - head_budget - _SEPARATOR_CHARS)

    head = text[:head_budget]
    head_end = head.rfind("\n") + 1  # 0 when the head holds no newline
    if head_end > head_budget // 2:
        head = head[:head_end]

    tail_start = total - tail_budget
    if tail_budget and text[tail_start - 1] != "\n":
        line_start = text.find("\n", tail_start) + 1  # 0 when the tail holds no newline
        if 0 < line_start - tail_start < tail_budget // 2:
            tail_start = line_start
    tail = text[tail_start:]

    kept = len(head) + len(tail)
    marker = (
        f"[... {total - kept} of {total} chars omitted "
        f"(limit: context.{field} = {limit}). {how_to_get_rest} ...]"
    )
    parts = [head]
    if head and not head.endswith("\n"):
        parts.append("\n")
    parts.append(marker)
    if tail:
        parts.append("\n" + tail)
    return FitResult(text="".join(parts), original_chars=total, kept_chars=kept)
