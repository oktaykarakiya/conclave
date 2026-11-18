"""Usage limits: wait until an engine's limit resets, then resume the interrupted dispatch.

An engine refuses work when a usage limit (Claude Code's five-hour or weekly window, an API rate
limit) or the account balance runs out; the providers report that as ``AgentResult.quota``.
:func:`dispatch_with_quota` is the dispatch path every caller shares (the engine's AgentRunner
and the planning sessions). On such a result it closes the daemon-wide :class:`QuotaGate` for the
engine account's key, waits until the gate reopens, and resumes the interrupted session with
:data:`CONTINUATION_PROMPT` (or re-runs the original prompt when there is no session to resume),
so a caller never sees a quota result. Every other dispatch for the same key waits for the gate
before it starts, so parallel reviewers and other projects do not each hit the refused API. Each
result it hands on (the interrupted run's to the listener, the final one to the caller) carries its
own run's cost, also when the engine reports a resumed session's running total, or restarts that
total because it could not restore the session's.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .base import UNKNOWN_SESSION_RE, AgentResult, OnChunk, Provider, QuotaExhausted
from .profiles import ResolvedProfile
from .router import Engine, resolve_engine

logger = logging.getLogger("conclave.providers.quota")

# A gate reopens this long after the reported reset, so the first request is not refused again.
RESUME_MARGIN = timedelta(seconds=60)
# A reported reset further ahead than this is beyond any usage window (Claude Code's longest is
# weekly), so it is a misread value (e.g. a retry-after in milliseconds read as seconds): it is
# ignored, as an unknown reset, instead of closing the gate until then.
MAX_PLAUSIBLE_RESET = timedelta(days=8)
# A wait sleeps at most this long before it reads the reopen time again (an extended closure, a
# wall-clock jump after a suspend).
_MAX_SLEEP_S = 60.0

CONTINUATION_PROMPT = (
    "You were interrupted by a usage limit. Continue the same task from where you stopped; the "
    "worktree contains your partial work. End with your complete final answer in the format the "
    "task asks for."
)

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def quota_key(provider: Provider, profile: ResolvedProfile) -> tuple[str, str]:
    """``(engine, key)`` of the account a dispatch of *profile* on *provider* draws on.

    Every Claude Code dispatch runs as the router's one ``claude-code`` profile on the host's
    login, so they all share a key. opencode dispatches share one per profile and model provider
    (``opencode/deepseek``): each provider has its own account and limits.
    """
    engine, model = resolve_engine(profile.model, getattr(provider, "default", Engine.opencode))
    if engine == Engine.claude:
        return str(Engine.claude), "claude-code"
    provider_prefix = model.split("/", 1)[0] if model and "/" in model else "default"
    return str(Engine.opencode), f"{profile.name}/{provider_prefix}"


@dataclass(frozen=True)
class QuotaWait:
    """A closed key one dispatch waits for (the details of its ``quota.waiting`` event).

    ``reset_ignored``: the engine reported a reset more than :data:`MAX_PLAUSIBLE_RESET` ahead
    (still ``quota.resets_at``), so the key closed for the retry minutes instead."""

    engine: str
    key: str
    quota: QuotaExhausted
    resumes_at: datetime
    reset_ignored: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "key": self.key,
            "kind": self.quota.kind,
            "window": self.quota.window,
            "resets_at": iso_utc(self.quota.resets_at),
            "reset_ignored": self.reset_ignored,
            "resumes_at": iso_utc(self.resumes_at),
            "message": self.quota.message,
        }


@dataclass
class _Closure:
    engine: str
    reopens_at: datetime
    quota: QuotaExhausted
    reset_ignored: bool = False
    notified: set[str] = field(default_factory=set)  # projects told about this closure


class QuotaGate:
    """Which engine accounts are out of quota, and until when (one per daemon).

    :meth:`close` shuts a key until the reported reset plus :data:`RESUME_MARGIN`, or for
    ``retry_minutes`` when no future reset time is known (none reported, one already past, or one
    more than :data:`MAX_PLAUSIBLE_RESET` ahead, which is ignored); a closure is only ever
    extended, never shortened. :meth:`wait` blocks until a key reopens. The gate also keeps the
    latest rate-limit info each key reported. The clock and sleep are injectable so tests never
    wait.
    """

    def __init__(self, *, clock: Clock = utc_now, sleep: Sleep = asyncio.sleep) -> None:
        self._clock = clock
        self._sleep = sleep
        self._closures: dict[str, _Closure] = {}
        self._rate_limits: dict[str, tuple[str, dict[str, Any]]] = {}

    def now(self) -> datetime:
        return self._clock()

    def close(
        self, key: str, engine: str, quota: QuotaExhausted, *, retry_minutes: int
    ) -> datetime:
        """Close *key* for *quota*; return when it reopens."""
        now = self._clock()
        resets_at = quota.resets_at
        reset_ignored = resets_at is not None and resets_at - now > MAX_PLAUSIBLE_RESET
        if resets_at is not None and resets_at > now and not reset_ignored:
            reopens_at = resets_at + RESUME_MARGIN
        else:
            reopens_at = now + timedelta(minutes=max(retry_minutes, 1))
        current = self._closures.get(key)
        if current is not None and current.reopens_at > now:
            if reopens_at > current.reopens_at:
                current.reopens_at = reopens_at
                current.quota = quota
                current.reset_ignored = reset_ignored
            return current.reopens_at
        self._closures[key] = _Closure(
            engine=engine, reopens_at=reopens_at, quota=quota, reset_ignored=reset_ignored
        )
        return reopens_at

    def pending(self, key: str) -> QuotaWait | None:
        """The wait *key* imposes now, or ``None`` while it is open."""
        closure = self._closures.get(key)
        if closure is None or closure.reopens_at <= self._clock():
            return None
        return QuotaWait(
            closure.engine, key, closure.quota, closure.reopens_at, closure.reset_ignored
        )

    async def wait(self, key: str, cancel_event: asyncio.Event | None = None) -> bool:
        """Sleep until *key* reopens (``True``); ``False`` as soon as *cancel_event* is set."""
        cancelled = (
            asyncio.ensure_future(cancel_event.wait()) if cancel_event is not None else None
        )
        try:
            while (pending := self.pending(key)) is not None:
                remaining = (pending.resumes_at - self._clock()).total_seconds()
                sleeper = asyncio.ensure_future(
                    self._sleep(min(max(remaining, 0.0), _MAX_SLEEP_S))
                )
                try:
                    await asyncio.wait(
                        {sleeper} if cancelled is None else {sleeper, cancelled},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    await _stop(sleeper)
                if cancelled is not None and cancelled.done():
                    return False
            return True
        finally:
            if cancelled is not None:
                await _stop(cancelled)

    def record_rate_limit(self, key: str, engine: str, info: dict[str, Any]) -> None:
        """Keep the latest rate-limit info (status, windows, reset times) *key* reported."""
        self._rate_limits[key] = (engine, dict(info))

    def claim_notification(self, key: str, project_id: str) -> bool:
        """``True`` once per closure of *key* and project: whether to notify about the wait."""
        closure = self._closures.get(key)
        if closure is None or project_id in closure.notified:
            return False
        closure.notified.add(project_id)
        return True

    def snapshot(self) -> list[dict[str, Any]]:
        """Every known key: whether it is closed, until when and why, and its rate-limit info."""
        rows: list[dict[str, Any]] = []
        for key in sorted(self._closures.keys() | self._rate_limits.keys()):
            closure = self._closures.get(key)
            limit = self._rate_limits.get(key)
            wait = self.pending(key)
            if wait is not None:
                row = wait.payload()
            else:
                engine = closure.engine if closure is not None else limit[0] if limit else ""
                row = dict.fromkeys(
                    ("kind", "window", "resets_at", "reset_ignored", "resumes_at", "message")
                )
                row["engine"] = engine
            row.update(
                key=key,
                closed=wait is not None,
                rate_limit=limit[1] if limit is not None else None,
            )
            rows.append(row)
        return rows


async def _stop(future: asyncio.Future[Any]) -> None:
    if not future.done():
        future.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await future


class WaitTracker:
    """How long one task's dispatches waited for a usage limit, measured with the gate's clock.

    Overlapping waits (parallel reviewers waiting for the same reset) are counted once. Each wait
    also counts in the *parent* tracker, if any (a bug-fixer session's, which spans its tasks and
    project-level dispatches), where overlapping waits are counted once too.
    """

    def __init__(self, clock: Clock, *, parent: WaitTracker | None = None) -> None:
        self._clock = clock
        self._parent = parent
        self._active = 0
        self._since: datetime | None = None
        self._total = 0.0

    @contextlib.contextmanager
    def waiting(self) -> Iterator[None]:
        with self._parent.waiting() if self._parent is not None else contextlib.nullcontext():
            if self._active == 0:
                self._since = self._clock()
            self._active += 1
            try:
                yield
            finally:
                self._active -= 1
                if self._active == 0 and self._since is not None:
                    self._total += max((self._clock() - self._since).total_seconds(), 0.0)
                    self._since = None

    def seconds(self) -> float:
        if self._since is None:
            return self._total
        return self._total + max((self._clock() - self._since).total_seconds(), 0.0)


class QuotaListener(Protocol):
    """Told about a dispatch's waits; see ``engine.runner.QuotaEvents``."""

    async def waiting(self, wait: QuotaWait, interrupted: AgentResult | None) -> None:
        """A wait starts: *interrupted* is the run the limit ended, ``None`` when the key was
        already closed before this dispatch ran."""

    async def resumed(
        self, wait: QuotaWait, *, waited_seconds: float, resumed_session: bool
    ) -> None:
        """The key reopened; the dispatch runs again (continuing its session or not)."""


async def dispatch_with_quota(
    provider: Provider,
    gate: QuotaGate,
    *,
    profile: ResolvedProfile,
    prompt: str,
    timeout_seconds: int,
    retry_minutes: int,
    cwd: Path | None = None,
    on_chunk: OnChunk | None = None,
    cancel_event: asyncio.Event | None = None,
    listener: QuotaListener | None = None,
    waits: WaitTracker | None = None,
) -> AgentResult:
    """Run one dispatch to its real outcome, waiting out every usage limit on the way.

    Waits while the engine account's key is closed (by this dispatch's own limit hit or by any
    other dispatch), then runs. A run the engine refuses for quota closes the key and loops; there
    is no cap on the number of waits. The next run continues the reported session with
    :data:`CONTINUATION_PROMPT`, or sends the original prompt again when there is none, or when
    the engine no longer knows the session. A wait ended by *cancel_event* returns the normal
    cancelled result. Each run gets the full *timeout_seconds*; waiting never happens inside one.

    The cost of every result handed on (to the listener as *interrupted*, and the final one) is
    that run's own: a run that continued a session whose cost the engine reports as a running total
    (``AgentResult.cost_is_session_total``) is charged its total minus the one reported before it,
    or its whole total when that is below the one reported before it (the engine did not restore
    the session's totals, so they restarted from this run).

    Callers pass a *profile* that starts a fresh session (no ``resume_session_id``): continuing a
    session is this function's job. It knows only the totals of the runs it made itself, so a
    session the caller's profile already continued would have its first run charged the whole
    running total of that session's earlier runs.
    """
    engine, key = quota_key(provider, profile)
    session_id: str | None = None  # the interrupted session to continue
    interrupted: AgentResult | None = None
    reported: float | None = None  # the running total the session's last run reported

    # Only what the caller set is passed on, as each caller did before it came through here.
    optional: dict[str, Any] = {}
    if on_chunk is not None:
        optional["on_chunk"] = on_chunk
    if cancel_event is not None:
        optional["cancel_event"] = cancel_event

    async def run(resume: str | None) -> AgentResult:
        result = await provider.run_agent(
            profile=replace(profile, resume_session_id=resume) if resume else profile,
            prompt=CONTINUATION_PROMPT if resume else prompt,
            timeout_seconds=timeout_seconds,
            cwd=cwd,
            **optional,
        )
        if result.rate_limit_info is not None:
            gate.record_rate_limit(key, engine, result.rate_limit_info)
        return result

    while True:
        wait = gate.pending(key)
        if wait is not None:
            started = gate.now()
            with waits.waiting() if waits is not None else contextlib.nullcontext():
                if listener is not None:
                    await _tell(listener.waiting(wait, interrupted), "waiting")
                reopened = await gate.wait(key, cancel_event)
            if not reopened:
                return AgentResult(ok=False, error="cancelled", cancelled=True)
            if listener is not None:
                await _tell(
                    listener.resumed(
                        wait,
                        waited_seconds=(gate.now() - started).total_seconds(),
                        resumed_session=session_id is not None,
                    ),
                    "resumed",
                )
        if cancel_event is not None and cancel_event.is_set():
            return AgentResult(ok=False, error="cancelled", cancelled=True)
        result = await run(session_id)
        continued = session_id is not None
        if continued and _unknown_session(result):
            logger.info("engine no longer has session %s; dispatching afresh", session_id)
            session_id = None
            continued = False
            result = await run(None)
        if not continued:
            reported = None  # a fresh session's running total starts from nothing
        result, reported = _own_cost(result, reported)
        if result.quota is None:
            return result
        gate.close(key, engine, result.quota, retry_minutes=retry_minutes)
        session_id = result.session_id or session_id
        interrupted = result


def _own_cost(result: AgentResult, reported: float | None) -> tuple[AgentResult, float | None]:
    """*result* charged its own run's cost, and the session's running total after it.

    When the engine reports the session's running total, a run that continued a session whose
    earlier run reported *reported* already includes that cost: its own share is the difference.
    A total below *reported* means the engine did not restore the session's totals (Claude Code
    saves them when its process exits normally, so a run stopped before that leaves none to
    restore): the run's total is then its own cost, charged in full, and the running total
    continues from it. A copy is returned, never the provider's object changed. Any other cost
    (a per-run one, or none) passes through, and *reported* stays the last total known.
    """
    if not result.cost_is_session_total or result.cost_usd is None:
        return result, reported
    total = result.cost_usd
    if reported is not None and total >= reported:
        result = result.model_copy(update={"cost_usd": total - reported})
    return result, total


def _unknown_session(result: AgentResult) -> bool:
    if result.ok or result.quota is not None:
        return False
    return UNKNOWN_SESSION_RE.search(f"{result.error or ''}\n{result.text}") is not None


async def _tell(call: Awaitable[None], what: str) -> None:
    """Await a listener call; its failure is logged, never raised into the dispatch."""
    try:
        await call
    except Exception:
        logger.warning("quota %s listener failed", what, exc_info=True)
