"""Domain row models for the persistence layer.

These are distinct from :mod:`conclave.config` models — they represent stored
records (projects, tasks, events, …). JSON columns are parsed on load via
``from_row``.
"""

from __future__ import annotations

import json
import logging
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# A single corrupt row must never 500 a list endpoint or stall the worker's claim loop,
# so from_row decoding degrades gracefully and records the corruption here instead of
# raising it up the stack.
logger = logging.getLogger("conclave.db.models")


class ProjectMode(StrEnum):
    task_queue = "task_queue"
    autonomous_bug_fixer = "autonomous_bug_fixer"


class TaskState(StrEnum):
    inbox = "inbox"  # created, awaiting approval
    approved = "approved"  # ready to run
    in_progress = "in_progress"  # claimed by a worker
    done = "done"
    failed = "failed"
    cancelled = "cancelled"
    blocked = "blocked"  # parent task failed, cannot proceed


class TaskOrigin(StrEnum):
    operator = "operator"
    bug_fixer = "bug_fixer"


class BugStatus(StrEnum):
    """The 7-state machine the Bug-Fixer controller drives over a candidate.

    The happy path is ``discovered → reproduced → fixing → fixed``; a candidate can
    instead branch off to one of three sinks. ``discovered`` and ``reproduced`` are the
    *actionable* states the controller auto-picks (to reproduce, then to fix); the rest are
    in-progress or terminal. ``declined_needs_human`` is the safe, non-actionable sink a
    corrupt row degrades to in :meth:`BugCandidate.from_row` — it is never auto-picked and
    explicitly routes to a human, mirroring ``TaskState.inbox`` for tasks.
    """

    discovered = "discovered"  # found, awaiting reproduction
    reproduced = "reproduced"  # repro test captured, eligible for an auto-fix attempt
    fixing = "fixing"  # an auto-fix attempt is in flight
    fixed = "fixed"  # terminal: repaired and merged
    dismissed_false_positive = "dismissed_false_positive"  # terminal: not a real bug
    declined_needs_human = "declined_needs_human"  # handed off to a human, never auto-picked
    deferred = "deferred"  # parked for now


class IllegalBugTransition(ValueError):
    """Raised when a bug-candidate status change violates :data:`BUG_STATUS_TRANSITIONS`.

    A ``ValueError`` subclass, so a caller can catch it narrowly or fall back to the broad
    ``ValueError`` net. The Bug-Fixer controller is the sole writer of ``bug_candidates.status``
    and must drive it strictly along the pinned table; an illegal edge is a controller bug, not
    a recoverable runtime condition, so the guard raises rather than silently no-opping the write.
    """


# The pinned 7-state transition table the Bug-Fixer controller drives a candidate along: each
# source state maps to the set of states it may legally advance to, and an empty frozenset marks
# a terminal sink. ``repositories.transition_bug_status`` guards every status write against this
# table, so no caller can shortcut an edge. Reconstructed from the bf-data epic's machine:
#
#   discovered → reproduced → fixing → fixed  (the happy path)
#   fixing → reproduced  (a failed fix attempt falls back to retry)
#   reproduced → declined_needs_human  (fix attempts exhausted → handed to a human)
#   discovered, reproduced → dismissed_false_positive | declined_needs_human | deferred  (sinks)
#   deferred → discovered | reproduced  (un-park back into the actionable pipeline)
#
# fixed, dismissed_false_positive and declined_needs_human are terminal for the controller; a
# human re-opening a declined candidate is a separate operator path, not an automatic edge.
BUG_STATUS_TRANSITIONS: dict[BugStatus, frozenset[BugStatus]] = {
    BugStatus.discovered: frozenset(
        {
            BugStatus.reproduced,
            BugStatus.dismissed_false_positive,
            BugStatus.declined_needs_human,
            BugStatus.deferred,
        }
    ),
    BugStatus.reproduced: frozenset(
        {
            BugStatus.fixing,
            BugStatus.dismissed_false_positive,
            BugStatus.declined_needs_human,
            BugStatus.deferred,
        }
    ),
    BugStatus.fixing: frozenset({BugStatus.fixed, BugStatus.reproduced}),
    BugStatus.fixed: frozenset(),
    BugStatus.dismissed_false_positive: frozenset(),
    BugStatus.declined_needs_human: frozenset(),
    BugStatus.deferred: frozenset({BugStatus.discovered, BugStatus.reproduced}),
}


def _loads(value: Any, fallback: Any) -> Any:
    """Decode a JSON column, tolerating corruption.

    A malformed ``*_json`` cell falls back to its caller-supplied default (and is logged)
    rather than raising JSONDecodeError out of ``from_row`` — one bad row must not take
    down the whole task/project list endpoint or crash the worker.
    """
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        logger.warning("corrupt JSON column, falling back to %r (raw=%r)", fallback, value)
        return fallback


def _enum[E: StrEnum](enum_cls: type[E], value: Any, default: E) -> E:
    """Parse an enum column, tolerating unknown values.

    The mirror of :func:`_loads` for enum columns: an unrecognised stored string (newer
    schema, or genuine corruption) falls back to ``default`` instead of raising ValueError.
    ``default`` is each field's already-declared safe value — notably ``TaskState.inbox``,
    a non-claimable state, so a corrupt task can never be picked up and run.
    """
    try:
        return enum_cls(value)
    except ValueError:
        logger.warning(
            "unknown %s value %r, falling back to %s", enum_cls.__name__, value, default.value
        )
        return default


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    path: str
    default_branch: str
    mode: ProjectMode = ProjectMode.task_queue
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> Project:
        return cls(
            id=row["id"],
            name=row["name"],
            path=row["path"],
            default_branch=row["default_branch"],
            mode=_enum(ProjectMode, row["mode"], ProjectMode.task_queue),
            config=_loads(row["config_json"], {}),
            created_at=row["created_at"],
        )


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    title: str = ""
    request: str
    level: int | None = None
    state: TaskState = TaskState.inbox
    use_planner: bool | None = None
    plan: dict[str, Any] | None = None
    branch: str | None = None
    result_summary: str | None = None
    origin: TaskOrigin = TaskOrigin.operator
    parent_task_id: str | None = None
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row: Any) -> Task:
        up = row["use_planner"]
        ptid = row["parent_task_id"] if "parent_task_id" in row.keys() else None
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            request=row["request"],
            level=row["level"],
            state=_enum(TaskState, row["state"], TaskState.inbox),
            use_planner=None if up is None else bool(up),
            plan=_loads(row["plan_json"], None),
            branch=row["branch"],
            result_summary=row["result_summary"],
            origin=_enum(TaskOrigin, row["origin"], TaskOrigin.operator),
            parent_task_id=ptid,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class EventRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    project_id: str | None = None
    task_id: str | None = None
    planning_session_id: str | None = None
    agent: str | None = None
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    ts: str

    @classmethod
    def from_row(cls, row: Any) -> EventRow:
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            planning_session_id=(
                row["planning_session_id"] if "planning_session_id" in row.keys() else None
            ),
            agent=row["agent"],
            type=row["type"],
            payload=_loads(row["payload_json"], {}),
            ts=row["ts"],
        )


class VerdictRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    task_id: str
    attempt: int
    agent: str
    verdict: str
    reason: str = ""
    source: str = "none"
    grounded_count: int = 0
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> VerdictRow:
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            attempt=row["attempt"],
            agent=row["agent"],
            verdict=row["verdict"],
            reason=row["reason"],
            source=row["source"],
            grounded_count=row["grounded_count"],
            evidence=_loads(row["evidence_json"], []),
            created_at=row["created_at"],
        )


class UsageRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str | None = None
    task_id: str | None = None
    agent: str
    model_reported: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    ts: str

    @classmethod
    def from_row(cls, row: Any) -> UsageRow:
        keys = row.keys()
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            task_id=row["task_id"],
            agent=row["agent"],
            model_reported=row["model_reported"],
            cost_usd=row["cost_usd"],
            num_turns=row["num_turns"],
            input_tokens=row["input_tokens"] if "input_tokens" in keys else None,
            output_tokens=row["output_tokens"] if "output_tokens" in keys else None,
            cache_read_tokens=row["cache_read_tokens"] if "cache_read_tokens" in keys else None,
            cache_creation_tokens=(
                row["cache_creation_tokens"] if "cache_creation_tokens" in keys else None
            ),
            ts=row["ts"],
        )


class EngineProfileRow(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    id: str
    project_id: str | None = None
    name: str
    arg_mode: str = "inherit"
    base_url: str | None = None
    model: str | None = None
    subagent_model: str | None = None
    effort: str | None = None
    auth_secret_id: str | None = None
    extra_env: dict[str, str] = Field(default_factory=dict)
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> EngineProfileRow:
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            arg_mode=row["arg_mode"],
            base_url=row["base_url"],
            model=row["model"],
            subagent_model=row["subagent_model"],
            effort=row["effort"],
            auth_secret_id=row["auth_secret_id"],
            extra_env=_loads(row["extra_env_json"], {}),
            created_at=row["created_at"],
        )


class AgentPersona(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str | None = None
    name: str
    role: str
    persona_md: str
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> AgentPersona:
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            name=row["name"],
            role=row["role"],
            persona_md=row["persona_md"],
            created_at=row["created_at"],
        )


class Baseline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    sha: str
    output: str
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> Baseline:
        return cls(
            project_id=row["project_id"],
            sha=row["sha"],
            output=row["output"],
            created_at=row["created_at"],
        )


class QuarantineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    pattern: str
    reason: str
    until: str  # YYYY-MM-DD
    created_by: str = "operator"
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> QuarantineEntry:
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            pattern=row["pattern"],
            reason=row["reason"],
            until=row["until"],
            created_by=row["created_by"],
            created_at=row["created_at"],
        )


class RepoKnowledgeRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    version: int
    sha: str | None = None
    manifest_fingerprint: str | None = None
    ai_enriched: bool = False
    knowledge: dict[str, Any] = Field(default_factory=dict)
    created_at: str

    @classmethod
    def from_row(cls, row: Any) -> RepoKnowledgeRow:
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            version=row["version"],
            sha=row["sha"],
            manifest_fingerprint=row["manifest_fingerprint"],
            ai_enriched=bool(row["ai_enriched"]) if "ai_enriched" in row.keys() else False,
            knowledge=_loads(row["knowledge_json"], {}),
            created_at=row["created_at"],
        )


class BugCandidate(BaseModel):
    """A row in the Bug-Fixer ledger — one suspected bug tracked through :class:`BugStatus`.

    The reproduction artifact (``region`` + ``repro_test_*``), the auto-fix retry count
    (``attempts``), the human-handoff note (``decline_reason``) and the serialized reviewer
    ``consensus`` ride alongside the state so the controller can resume a candidate without
    re-deriving its context.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    fingerprint: str
    file: str | None = None
    symbol: str | None = None
    region: str | None = None
    claim: str
    severity: str | None = None
    status: BugStatus = BugStatus.discovered
    repro_test_path: str | None = None
    repro_test_body: str | None = None
    repro_test_hash: str | None = None
    attempts: int = 0
    decline_reason: str | None = None
    consensus: dict[str, Any] = Field(default_factory=dict)
    task_id: str | None = None
    notes: str | None = None
    discovered_at: str
    last_examined_at: str
    fixed_at: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> BugCandidate:
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            fingerprint=row["fingerprint"],
            file=row["file"],
            symbol=row["symbol"],
            region=row["region"],
            claim=row["claim"],
            severity=row["severity"],
            # A corrupt/unknown status degrades to declined_needs_human, NOT the 'discovered'
            # field default: 'discovered' is auto-picked for reproduction (and onward to a fix),
            # so a corrupt candidate that fell back there would be silently fixed. The safe sink
            # is the non-actionable, human-routed state — the BugStatus mirror of TaskState.inbox.
            status=_enum(BugStatus, row["status"], BugStatus.declined_needs_human),
            repro_test_path=row["repro_test_path"],
            repro_test_body=row["repro_test_body"],
            repro_test_hash=row["repro_test_hash"],
            attempts=row["attempts"],
            decline_reason=row["decline_reason"],
            consensus=_loads(row["consensus_json"], {}),
            task_id=row["task_id"],
            notes=row["notes"],
            discovered_at=row["discovered_at"],
            last_examined_at=row["last_examined_at"],
            fixed_at=row["fixed_at"],
        )


class CoverageRegion(BaseModel):
    """A region of the repo the Bug-Fixer has swept, with the scheduler's ranking fields.

    ``priority`` ranks which region to examine next and ``examined_count`` ages out regions
    already swept many times, so the scheduler can balance breadth against revisiting hot spots.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    region: str
    last_examined_at: str | None = None
    priority: int = 0
    examined_count: int = 0

    @classmethod
    def from_row(cls, row: Any) -> CoverageRegion:
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            region=row["region"],
            last_examined_at=row["last_examined_at"],
            priority=row["priority"],
            examined_count=row["examined_count"],
        )
