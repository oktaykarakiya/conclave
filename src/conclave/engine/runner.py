"""Agent runner: turns a (persona, task) into a provider dispatch with events + usage.

opencode owns model/provider selection and auth through its own config, so a dispatch
no longer resolves an Engine Profile from the database — it uses a fixed ``opencode``
profile carrying only the per-agent model/effort overrides. The runner assembles the
full prompt (persona + repo knowledge + project rules + task), invokes the provider,
records usage, and emits dispatch/result events.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..config import ArgMode, ConclaveConfig, Effort, resolve_agent
from ..db import Database
from ..db import repositories as repo
from ..events import EventBus, EventType
from ..providers import AgentResult, Provider, ResolvedProfile

logger = logging.getLogger("conclave.engine.runner")

# Live token streaming is flushed to the bus per newline OR once the buffer crosses this
# size, whichever comes first. Bounding the flush keeps a chatty agent from emitting one
# event per tiny provider chunk (which would flood the bus and every subscriber's queue)
# while still delivering near-real-time output to the Live tab.
_STREAM_FLUSH_BYTES = 1024


def assemble_prompt(system: str, knowledge: str, rules: str, task_prompt: str) -> str:
    parts = [f"SYSTEM CONTEXT:\n{system}"]
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
        self, bus: EventBus, project_id: str, task_id: str | None, agent: str
    ) -> None:
        self._bus = bus
        self._project_id = project_id
        self._task_id = task_id
        self._agent = agent
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
            await self._bus.emit(
                type=EventType.agent_output,
                project_id=self._project_id,
                task_id=self._task_id,
                agent=self._agent,
                payload={"text": text},
            )
        except Exception:  # best-effort: never let a live-output emit break the dispatch
            logger.warning(
                "agent_output stream emit failed for agent %s", self._agent, exc_info=True
            )


class AgentRunner:
    def __init__(
        self,
        db: Database,
        bus: EventBus,
        provider: Provider,
        project_id: str,
        config: ConclaveConfig,
    ) -> None:
        self._db = db
        self._bus = bus
        self._provider = provider
        self._project_id = project_id
        self._config = config

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
    ) -> AgentResult:
        persona = await repo.get_agent(self._db, agent, self._project_id)
        system = (
            persona.persona_md
            if persona is not None
            else f"You are the {agent} agent of an autonomous software engineering team."
        )
        settings = resolve_agent(self._config, agent)
        profile = self._resolve_profile(settings.model, settings.effort)
        full_prompt = assemble_prompt(system, repo_knowledge, project_rules, prompt)

        await self._bus.emit(
            type=EventType.agent_dispatched,
            project_id=self._project_id,
            task_id=task_id,
            agent=agent,
            payload={"profile": profile.name, "model": profile.model, "cwd": str(worktree)},
        )
        stream = _OutputStreamer(self._bus, self._project_id, task_id, agent)
        result = await self._provider.run_agent(
            profile=profile,
            prompt=full_prompt,
            timeout_seconds=settings.timeout_minutes * 60,
            cwd=worktree,
            on_chunk=stream.feed,
            cancel_event=cancel_event,
        )
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
            },
        )
        return result

    def _resolve_profile(
        self, model: str | None, effort: Effort | None
    ) -> ResolvedProfile:
        """Build the fixed ``opencode`` dispatch profile for this agent.

        opencode selects the model/provider and handles auth through its own config, so
        Conclave no longer looks up a stored Engine Profile. Only the per-agent model and
        effort overrides are carried through (``arg_mode=inherit`` adds no CLI flags; the
        opencode provider ignores a model that is not an opencode-format ``provider/model``).
        """
        return ResolvedProfile(
            name="opencode",
            arg_mode=ArgMode.inherit,
            model=model,
            effort=str(effort) if effort is not None else None,
        )
