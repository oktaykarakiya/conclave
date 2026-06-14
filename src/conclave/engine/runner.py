"""Agent runner: turns a (persona, task) into a provider dispatch with events + usage.

Resolves the per-agent Engine Profile (model/effort overrides + secret), assembles the
full prompt (persona + repo knowledge + project rules + task), invokes the provider,
records usage, and emits dispatch/result events.
"""

from __future__ import annotations

from pathlib import Path

from ..config import ArgMode, ConclaveConfig, resolve_agent
from ..db import Database
from ..db import repositories as repo
from ..events import EventBus, EventType
from ..providers import AgentResult, Provider, ResolvedProfile, resolve_profile


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
    ) -> AgentResult:
        persona = await repo.get_agent(self._db, agent, self._project_id)
        system = (
            persona.persona_md
            if persona is not None
            else f"You are the {agent} agent of an autonomous software engineering team."
        )
        settings = resolve_agent(self._config, agent)
        profile = await self._resolve_profile(
            settings.engine_profile, settings.model, settings.effort
        )
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

    async def _resolve_profile(
        self, profile_name: str, model: str | None, effort: object | None
    ) -> ResolvedProfile:
        effort_str = str(effort) if effort is not None else None
        row = await repo.get_engine_profile(self._db, profile_name, self._project_id)
        if row is None and profile_name != "system-default":
            row = await repo.get_engine_profile(self._db, "system-default", self._project_id)
        if row is None:
            return ResolvedProfile(
                name="system-default", arg_mode=ArgMode.inherit, model=model, effort=effort_str
            )
        auth = (
            await repo.get_secret_value(self._db, row.auth_secret_id)
            if row.auth_secret_id
            else None
        )
        return resolve_profile(
            row, auth_token=auth, model_override=model, effort_override=effort_str
        )
