"""Agent runner: turns a (persona, task) into a provider dispatch with events + usage.

opencode owns model/provider selection and auth through its own config, so a dispatch
no longer resolves an Engine Profile from the database — it uses a fixed ``opencode``
profile carrying only the per-agent model/effort overrides. The runner assembles the
full prompt (persona + repo knowledge + project rules + task), invokes the provider,
records usage, and emits dispatch/result events.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..config import ArgMode, ConclaveConfig, Effort, resolve_agent
from ..db import Database
from ..db import repositories as repo
from ..events import EventBus, EventType
from ..providers import AgentResult, Provider, ResolvedProfile


def assemble_prompt(system: str, knowledge: str, rules: str, task_prompt: str) -> str:
    parts = [f"SYSTEM CONTEXT:\n{system}"]
    if knowledge:
        parts.append(f"REPO KNOWLEDGE:\n{knowledge}")
    if rules:
        parts.append(f"PROJECT RULES:\n{rules}")
    parts.append(f"TASK:\n{task_prompt}")
    return "\n\n".join(parts) + "\n"


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
        result = await self._provider.run_agent(
            profile=profile,
            prompt=full_prompt,
            timeout_seconds=settings.timeout_minutes * 60,
            cwd=worktree,
            cancel_event=cancel_event,
        )
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
