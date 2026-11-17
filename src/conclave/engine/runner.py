"""Agent runner: turns a (persona, task) into a provider dispatch with events + usage.

opencode owns model/provider selection and auth through its own config, so a dispatch
no longer resolves an Engine Profile from the database — it uses a fixed ``opencode``
profile carrying only the per-agent model/effort overrides. The runner assembles the
full prompt (persona + repo knowledge + project rules + task), invokes the provider through
the usage-limit wait (:func:`~conclave.providers.dispatch_with_quota`), records usage, and
emits dispatch/result events.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from ..config import ArgMode, ConclaveConfig, Effort, resolve_agent
from ..db import Database
from ..db import repositories as repo
from ..events import EventBus, EventType, build_notification_sink
from ..observability.parquet_sync import log_agent_interaction
from ..providers import (
    AgentResult,
    EffortChoice,
    Provider,
    QuotaGate,
    QuotaWait,
    ResolvedProfile,
    WaitTracker,
    dispatch_with_quota,
    resolve_dispatch_effort,
)

logger = logging.getLogger("conclave.engine.runner")

# Live token streaming is flushed to the bus per newline OR once the buffer crosses this
# size, whichever comes first. Bounding the flush keeps a chatty agent from emitting one
# event per tiny provider chunk (which would flood the bus and every subscriber's queue)
# while still delivering near-real-time output to the Live tab.
_STREAM_FLUSH_BYTES = 1024

# Webhook deliveries of quota.waiting run beside the wait (a slow webhook must not delay a
# cancel); the set keeps each task referenced until it finishes.
_NOTIFICATIONS: set[asyncio.Task[None]] = set()


def effort_clamped_payload(
    choice: EffortChoice, model: str | None, agent: str, *, node_id: str
) -> dict[str, object]:
    """Payload of the ``log`` event a dispatch emits when its explicit effort was clamped."""
    return {
        "stage": "effort_clamped",
        "requested": choice.requested,
        "used": choice.used,
        "model": model,
        "agent": agent,
        "node_id": node_id,
        # The Live tab renders log rows as "[stage] message".
        "message": (
            f"{agent}: {model or 'the default model'} does not support effort "
            f"{choice.requested}; using {choice.used or 'no effort flag'}"
        ),
    }


def assemble_prompt(
    system: str,
    knowledge: str,
    rules: str,
    task_prompt: str,
    skills: list[str] | None = None,
) -> str:
    parts = [f"SYSTEM CONTEXT:\n{system}"]
    if skills:
        skill_lines = "\n".join(f"- {s.strip()}" for s in skills if s and s.strip())
        if skill_lines:
            parts.append(f"SKILLS & CAPABILITIES:\n{skill_lines}")
    if knowledge:
        parts.append(f"REPO KNOWLEDGE:\n{knowledge}")
    if rules:
        parts.append(f"PROJECT RULES:\n{rules}")
    parts.append(f"TASK:\n{task_prompt}")
    return "\n\n".join(parts) + "\n"


class _OutputStreamer:
    """Buffers provider stdout chunks and emits bounded ``agent_output`` events.

    Providers call :meth:`feed` once per raw read (which may be tiny). We coalesce those
    chunks and flush a single :class:`EventType.agent_output` event per complete line, or
    whenever the pending buffer crosses :data:`_STREAM_FLUSH_BYTES`, so the Live tab sees
    near-real-time output without one bus event per micro-chunk. :meth:`flush` drains any
    trailing partial line once the dispatch finishes.

    Streaming is strictly best-effort: any failure while emitting is swallowed and logged so
    a misbehaving subscriber or bus hiccup can never break the agent dispatch it mirrors.
    """

    def __init__(
        self,
        bus: EventBus,
        project_id: str,
        task_id: str | None,
        agent: str,
        node_id: str | None = None,
    ) -> None:
        self._bus = bus
        self._project_id = project_id
        self._task_id = task_id
        self._agent = agent
        self._node_id = node_id
        self._buffer = ""

    async def feed(self, text: str) -> None:
        self._buffer += text
        # Flush every complete line eagerly so output streams as it arrives.
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            await self._emit(line + "\n")
        # A long line with no newline must not buffer unbounded — flush at the size cap.
        if len(self._buffer) >= _STREAM_FLUSH_BYTES:
            await self._emit(self._buffer)
            self._buffer = ""

    async def flush(self) -> None:
        """Emit any trailing partial line left after the dispatch completes."""
        if self._buffer:
            await self._emit(self._buffer)
            self._buffer = ""

    async def _emit(self, text: str) -> None:
        try:
            payload: dict[str, object] = {"text": text}
            if self._node_id:
                payload["node_id"] = self._node_id
            await self._bus.emit(
                type=EventType.agent_output,
                project_id=self._project_id,
                task_id=self._task_id,
                agent=self._agent,
                payload=payload,
            )
        except Exception:  # best-effort: never let a live-output emit break the dispatch
            logger.warning(
                "agent_output stream emit failed for agent %s", self._agent, exc_info=True
            )


class QuotaEvents:
    """Reports one dispatch's usage-limit waits (a :class:`~conclave.providers.QuotaListener`).

    Emits ``quota.waiting`` and ``quota.resumed`` on the task, or the planning session (neither:
    a project-level dispatch such as the bug fixer's), POSTs ``quota.waiting`` to the configured
    webhook once per closure and project, and, with *record_usage*, adds a usage row for each run
    a limit interrupted (the tokens it used are spent all the same).
    """

    def __init__(
        self,
        db: Database,
        bus: EventBus,
        gate: QuotaGate,
        config: ConclaveConfig,
        *,
        project_id: str,
        agent: str,
        node_id: str,
        task_id: str | None = None,
        planning_session_id: str | None = None,
        record_usage: bool = False,
    ) -> None:
        self._db = db
        self._bus = bus
        self._gate = gate
        self._config = config
        self._project_id = project_id
        self._agent = agent
        self._node_id = node_id
        self._task_id = task_id
        self._planning_session_id = planning_session_id
        self._record_usage = record_usage

    async def waiting(self, wait: QuotaWait, interrupted: AgentResult | None) -> None:
        if interrupted is not None and self._record_usage:
            await repo.add_usage(
                self._db,
                agent=self._agent,
                project_id=self._project_id,
                task_id=self._task_id,
                model_reported=interrupted.model_reported,
                cost_usd=interrupted.cost_usd,
                num_turns=interrupted.num_turns,
                input_tokens=interrupted.input_tokens,
                output_tokens=interrupted.output_tokens,
                cache_read_tokens=interrupted.cache_read_tokens,
                cache_creation_tokens=interrupted.cache_creation_tokens,
            )
        payload = {**wait.payload(), "agent": self._agent, "node_id": self._node_id}
        await self._emit(EventType.quota_waiting, payload)
        if not self._gate.claim_notification(wait.key, self._project_id):
            return
        sink = build_notification_sink(self._config)
        if sink is not None:
            notification = asyncio.create_task(
                sink.notify(
                    {
                        "event": str(EventType.quota_waiting),
                        "project_id": self._project_id,
                        "task_id": self._task_id,
                        "planning_session_id": self._planning_session_id,
                        **payload,
                    }
                )
            )
            _NOTIFICATIONS.add(notification)
            notification.add_done_callback(_NOTIFICATIONS.discard)

    async def resumed(
        self, wait: QuotaWait, *, waited_seconds: float, resumed_session: bool
    ) -> None:
        await self._emit(
            EventType.quota_resumed,
            {
                "engine": wait.engine,
                "key": wait.key,
                "waited_seconds": round(waited_seconds, 1),
                "resumed_session": resumed_session,
                "agent": self._agent,
                "node_id": self._node_id,
            },
        )

    async def _emit(self, type_: EventType, payload: dict[str, object]) -> None:
        await self._bus.emit(
            type=type_,
            project_id=self._project_id,
            task_id=self._task_id,
            planning_session_id=self._planning_session_id,
            agent=self._agent,
            payload=payload,
        )


class AgentRunner:
    """Dispatches the agents of one task (or one project-level job).

    Dispatches wait out usage limits on *quota_gate*, the daemon's shared gate (a private one
    when none is given), and the runner adds up the time they waited (:meth:`quota_waited_seconds`)
    so the task's wall-clock budget can leave it out.
    """

    def __init__(
        self,
        db: Database,
        bus: EventBus,
        provider: Provider,
        project_id: str,
        config: ConclaveConfig,
        *,
        quota_gate: QuotaGate | None = None,
    ) -> None:
        self._db = db
        self._bus = bus
        self._provider = provider
        self._project_id = project_id
        self._config = config
        self._quota_gate = quota_gate if quota_gate is not None else QuotaGate()
        self._quota_waits = WaitTracker(self._quota_gate.now)

    def quota_waited_seconds(self) -> float:
        """Seconds this runner's dispatches spent waiting for a usage limit (overlaps once)."""
        return self._quota_waits.seconds()

    async def run(
        self,
        *,
        agent: str,
        prompt: str,
        task_id: str | None = None,
        worktree: Path,
        repo_knowledge: str = "",
        project_rules: str = "",
        cancel_event: asyncio.Event | None = None,
        node_id: str | None = None,
        model_override: str | None = None,
        effort_override: str | None = None,
        cli_agent_override: str | None = None,
        skills_override: list[str] | None = None,
        thinking_override: bool | None = None,
    ) -> AgentResult:
        persona = await repo.get_agent(self._db, agent, self._project_id)
        system = (
            persona.persona_md
            if persona is not None
            else f"You are the {agent} agent of an autonomous software engineering team."
        )
        settings = resolve_agent(self._config, agent)
        model = model_override if model_override is not None else settings.model
        effort: Effort | str | None = (
            effort_override if effort_override is not None else settings.effort
        )
        cli_agent = cli_agent_override if cli_agent_override is not None else settings.cli_agent
        skills = (
            list(dict.fromkeys([*settings.skills, *skills_override]))
            if skills_override
            else list(settings.skills)
        )
        thinking = thinking_override if thinking_override is not None else settings.thinking
        choice = await resolve_dispatch_effort(self._provider, model, effort)
        if choice.clamped:
            await self._bus.emit(
                type=EventType.log,
                project_id=self._project_id,
                task_id=task_id,
                agent=agent,
                payload=effort_clamped_payload(choice, model, agent, node_id=node_id or agent),
            )
        profile = self._resolve_profile(
            model,
            choice.used,
            cli_agent=cli_agent,
            thinking=thinking,
            pure=settings.pure,
        )
        full_prompt = assemble_prompt(system, repo_knowledge, project_rules, prompt, skills=skills)

        # Once cancelled, no dispatch starts: no agent.dispatched, provider run or usage row.
        if cancel_event is not None and cancel_event.is_set():
            return AgentResult(ok=False, error="cancelled", cancelled=True)
        await self._bus.emit(
            type=EventType.agent_dispatched,
            project_id=self._project_id,
            task_id=task_id,
            agent=agent,
            payload={
                "profile": profile.name,
                "model": profile.model,
                "effort": profile.effort,
                "cwd": str(worktree),
                "node_id": node_id or agent,
            },
        )
        stream = _OutputStreamer(
            self._bus, self._project_id, task_id, agent, node_id=node_id or agent
        )
        started_at = time.monotonic()
        result = await dispatch_with_quota(
            self._provider,
            self._quota_gate,
            profile=profile,
            prompt=full_prompt,
            timeout_seconds=settings.timeout_minutes * 60,
            retry_minutes=self._config.execution.quota_retry_minutes,
            cwd=worktree,
            on_chunk=stream.feed,
            cancel_event=cancel_event,
            listener=QuotaEvents(
                self._db,
                self._bus,
                self._quota_gate,
                self._config,
                project_id=self._project_id,
                agent=agent,
                node_id=node_id or agent,
                task_id=task_id,
                record_usage=True,
            ),
            waits=self._quota_waits,
        )
        duration_seconds = max(0.0, time.monotonic() - started_at)
        await stream.flush()
        await repo.add_usage(
            self._db,
            agent=agent,
            project_id=self._project_id,
            task_id=task_id,
            model_reported=result.model_reported,
            cost_usd=result.cost_usd,
            num_turns=result.num_turns,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_read_tokens=result.cache_read_tokens,
            cache_creation_tokens=result.cache_creation_tokens,
        )
        try:
            first_line = next((ln.strip() for ln in prompt.splitlines() if ln.strip()), agent)
            log_entry = await log_agent_interaction(
                self._db,
                session_id=result.session_id,
                title=f"[{agent}] {first_line[:72]}",
                directory=str(worktree),
                model=profile.model or "opencode/default",
                agent=agent,
                prompt=prompt,
                result=result,
                duration_seconds=duration_seconds,
                project_id=self._project_id,
                task_id=task_id,
                node_id=node_id or agent,
                source="conclave",
            )
            await self._bus.emit(
                type="interaction.logged",
                project_id=self._project_id,
                task_id=task_id,
                agent=agent,
                payload={
                    "session_id": log_entry.session_id,
                    "agent": agent,
                    "model": log_entry.model,
                    "cost": log_entry.cost,
                },
            )
        except Exception:
            logger.warning("interaction logging failed for agent %s", agent, exc_info=True)

        await self._bus.emit(
            type=EventType.agent_result,
            project_id=self._project_id,
            task_id=task_id,
            agent=agent,
            payload={
                "ok": result.ok,
                "cost_usd": result.cost_usd,
                "model_reported": result.model_reported,
                "error": result.error,
                "node_id": node_id or agent,
            },
        )
        return result

    def _resolve_profile(
        self,
        model: str | None,
        effort: Effort | str | None,
        *,
        cli_agent: str | None = None,
        thinking: bool = False,
        pure: bool = False,
    ) -> ResolvedProfile:
        """Build the fixed ``opencode`` dispatch profile for this agent.

        opencode selects the model/provider and handles auth through its own config, so
        Conclave no longer looks up a stored Engine Profile. Only the per-agent model and
        effort overrides are carried through (``arg_mode=inherit`` adds no CLI flags; the
        opencode provider ignores a model that is not an opencode-format ``provider/model``),
        plus the project's ``execution.worker_isolation`` as ``isolated``.
        """
        return ResolvedProfile(
            name="opencode",
            arg_mode=ArgMode.inherit,
            model=model,
            effort=str(effort) if effort is not None else None,
            cli_agent=cli_agent,
            thinking=thinking,
            pure=pure,
            isolated=self._config.execution.worker_isolation,
        )
