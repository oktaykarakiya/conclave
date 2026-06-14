"""The orchestration loop (port of team-ai's ``process_task``).

Per task: create an isolated worktree from the target branch → snapshot baseline
failures → optional planner → retry loop (developer → diff-derived reviewers with
grounded verdicts → green-gate, feeding failures back with cross-attempt memory) →
on success commit + merge into the target branch; on failure clean up. All state is
in SQLite and every step emits an event.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, cast

from ..config import ConclaveConfig, load_project_config, resolve_agent
from ..config.models import L2Settings
from ..db import Database, Task, TaskState
from ..db import repositories as repo
from ..events import EventBus, EventType
from ..providers import Provider
from ..repo_intel.knowledge import render_preamble
from .baseline import build_baseline_preamble
from .gate import run_tests
from .gitio import run_git
from .level_router import classify_level
from .memory import AttemptMemory
from .pipeline import get_agent_pipeline
from .runner import AgentRunner
from .verdict import ParsedVerdict, check_grounding, parse_verdict
from .worktree import WorktreeError, WorktreeManager

_PROJECT_RULE_FILES = ("CONCLAVE.md", "CLAUDE.md")
_L4_DECOMPOSED = object()  # sentinel: _run_l4 decomposed the epic into children


class Orchestrator:
    def __init__(
        self, db: Database, bus: EventBus, provider: Provider, conclave_home: Path
    ) -> None:
        self._db = db
        self._bus = bus
        self._provider = provider
        self._home = conclave_home

    async def recover(self, project_id: str) -> int:
        """Crash recovery: return orphaned in_progress tasks to approved."""
        return await repo.recover_in_progress(self._db, project_id)

    async def process_task(self, task: Task) -> bool:
        project = await repo.get_project(self._db, task.project_id)
        if project is None:
            return False
        config = load_project_config(project.config)
        repo_path = Path(project.path)
        target_branch = config.execution.target_branch or project.default_branch
        runner = AgentRunner(self._db, self._bus, self._provider, project.id, config)
        wm = WorktreeManager(repo_path, self._home / "projects" / project.id / "worktrees")
        task_branch = f"{config.execution.branch_prefix}{task.id}"

        await self._bus.emit(
            type=EventType.task_started,
            project_id=project.id,
            task_id=task.id,
            payload={"request": task.request[:200], "target_branch": target_branch},
        )

        # Scale-adaptive planning level (BMad L0-L4). Classified up-front — before any
        # worktree creation/setup — so a future L4 path (task 9) can skip setup entirely;
        # persisted and broadcast so the UI/event stream reflects the routing for EVERY task.
        level = classify_level(
            task.request,
            config.planning,
            use_planner=task.use_planner,
            planner_enabled=config.experimental.planner_enabled,
        )
        await repo.update_task_fields(self._db, task.id, level=level)
        await self._bus.emit(
            type=EventType.plan_level,
            project_id=project.id,
            task_id=task.id,
            payload={"task_id": task.id, "level": level},
        )

        try:
            worktree = await wm.create(task.id, target_branch, task_branch)
        except WorktreeError as exc:
            return await self._fail_early(task, wm, task_branch, f"worktree setup failed: {exc}")

        _, sha = await run_git(worktree, "rev-parse", "HEAD")
        checkpoint = sha.strip()

        knowledge_row = await repo.current_repo_knowledge(self._db, project.id)
        knowledge = render_preamble(knowledge_row.knowledge) if knowledge_row else ""
        rules = _read_project_rules(worktree)
        test_command = _test_command(config, knowledge_row.knowledge if knowledge_row else None)
        gate_timeout = resolve_agent(config, "tester").timeout_minutes * 60

        baseline_preamble = await self._baseline(
            project.id, task.id, worktree, checkpoint, target_branch, test_command, gate_timeout
        )
        plan_preamble = await self._maybe_plan(
            runner, task, worktree, knowledge, rules, baseline_preamble, config, level
        )

        # L4 short-circuit: the epic was decomposed into child tasks — skip the entire
        # developer/review retry loop, clean up the unused worktree, and return success.
        if plan_preamble is _L4_DECOMPOSED:
            await wm.cleanup(task.id, None)
            return True

        # plan_preamble is str at this point (the L4 sentinel returned above), but
        # mypy can't narrow `str | object` through an `is` check on a plain
        # `object()` sentinel.
        plan_preamble = cast(str, plan_preamble)

        memory = AttemptMemory(config.experimental.cross_attempt_memory_entries)
        use_memory = config.experimental.cross_attempt_memory
        max_retries = resolve_agent(config, "developer").max_retries
        budget = config.execution.wall_clock_budget_minutes
        started = time.monotonic()

        feedback = ""
        previous_diff = ""
        attempts = 0
        failed = False
        timed_out = False

        while attempts < max_retries:
            if budget > 0 and (time.monotonic() - started) / 60.0 >= budget:
                timed_out = True
                failed = True
                break
            attempts += 1
            failed = False
            await self._bus.emit(
                type=EventType.attempt_started,
                project_id=project.id,
                task_id=task.id,
                payload={"n": attempts},
            )
            attempt_id = await repo.start_attempt(self._db, task.id, attempts)

            if attempts > 1:
                _, previous_diff = await run_git(worktree, "diff", "--cached", checkpoint)
                if use_memory:
                    memory.add(attempts - 1, previous_diff, feedback)
                await run_git(worktree, "reset", "--hard", checkpoint)
                await run_git(worktree, "clean", "-fd")

            dev_prompt = task.request + plan_preamble + baseline_preamble
            if use_memory:
                dev_prompt += memory.build_preamble()
            if feedback:
                dev_prompt += f"\n\nLATEST REVIEWER FEEDBACK (must fix these issues):\n{feedback}"
                if previous_diff:
                    dev_prompt += f"\n\nYOUR IMMEDIATELY PRIOR DIFF:\n```diff\n{previous_diff}\n```"

            dev = await runner.run(
                agent="developer",
                prompt=dev_prompt,
                task_id=task.id,
                worktree=worktree,
                repo_knowledge=knowledge,
                project_rules=rules,
            )
            if not dev.ok:
                feedback = f"Developer agent error: {dev.error}"
                await repo.end_attempt(self._db, attempt_id)
                await self._bus.emit(
                    type=EventType.attempt_failed,
                    project_id=project.id,
                    task_id=task.id,
                    payload={"n": attempts, "stage": "developer", "error": dev.error},
                )
                failed = True
                continue

            # Stage everything so new (untracked) files appear in the diff the
            # reviewers and grounding see — not just modifications to tracked files.
            await run_git(worktree, "add", "-A")
            _, current_diff = await run_git(worktree, "diff", "--cached", checkpoint)
            await repo.end_attempt(self._db, attempt_id, diff_stat=_diff_stat(current_diff))

            failed, feedback = await self._review(
                runner, task, project.id, worktree, current_diff, attempts, config, knowledge, rules
            )
            if failed:
                await self._bus.emit(
                    type=EventType.attempt_failed,
                    project_id=project.id,
                    task_id=task.id,
                    payload={"n": attempts, "stage": "review"},
                )
                continue

            if test_command and config.execution.require_full_green:
                gate = await run_tests(worktree, test_command, timeout_seconds=gate_timeout)
                if not gate.passed:
                    feedback = (
                        f"TEST GATE is not green (exit {gate.exit_code}). "
                        f"Fix the failing tests:\n{gate.output[-2000:]}"
                    )
                    await self._bus.emit(
                        type=EventType.attempt_failed,
                        project_id=project.id,
                        task_id=task.id,
                        payload={"n": attempts, "stage": "gate", "exit_code": gate.exit_code},
                    )
                    failed = True
                    continue

            break  # success

        if failed or attempts == 0:
            return await self._finish_failure(
                task, wm, task_branch, timed_out, attempts
            )
        return await self._finish_success(
            task, wm, task_branch, repo_path, target_branch, worktree, config, attempts
        )

    # --- phases ---------------------------------------------------------------

    async def _baseline(
        self,
        project_id: str,
        task_id: str,
        worktree: Path,
        checkpoint: str,
        target_branch: str,
        test_command: str | None,
        gate_timeout: int,
    ) -> str:
        if not test_command:
            return ""
        cached = await repo.get_baseline(self._db, project_id, checkpoint)
        if cached is not None:
            return build_baseline_preamble(target_branch, cached.output)
        await self._bus.emit(
            type=EventType.baseline_snapshot,
            project_id=project_id,
            task_id=task_id,
            payload={"sha": checkpoint[:12]},
        )
        gate = await run_tests(worktree, test_command, timeout_seconds=gate_timeout)
        failures = "" if gate.passed else gate.output
        await repo.save_baseline(self._db, project_id, checkpoint, failures)
        await repo.gc_baselines(self._db, project_id)
        return build_baseline_preamble(target_branch, failures)

    async def _maybe_plan(
        self,
        runner: AgentRunner,
        task: Task,
        worktree: Path,
        knowledge: str,
        rules: str,
        baseline_preamble: str,
        config: ConclaveConfig,
        level: int,
    ) -> str | object:
        # Defensive: classify_level already collapses planner_enabled=False to level 0,
        # so this guard is redundant with the level == 0 check below. Kept to keep the
        # config flag referenced and to fail closed regardless of the passed-in level.
        if not config.experimental.planner_enabled:
            return ""
        # Level gate (SUPERSEDES auto_planner_char_threshold): L0 => no planner, no
        # dispatch. Levels 1-4 run the one-shot planner; L2/3/4 currently reuse the L1
        # path as a placeholder (tasks 5/7/9 specialize them).
        if level == 0:
            return ""
        # L4: epic-decomposition — dispatch a decomposer persona, create child tasks,
        # and short-circuit the retry loop on success; degrade to L1 on empty/malformed
        # output so the work is still attempted under the green-gate.
        if level == 4:
            return await self._run_l4(runner, task, worktree, knowledge, rules, config)
        # L3: three-agent sequential planning (PM → Architect-as-Planner → Planner),
        # each stage flag-gated by l3_settings. Intermediate None → skip-and-continue;
        # final planner None → degrade to L1-style empty preamble.
        if level == 3:
            return await self._run_l3(runner, task, worktree, knowledge, rules, config)
        prompt = (
            f"{task.request}{baseline_preamble}\n\n"
            "Produce a structured plan per your system prompt."
        )
        # L2 (enhanced one-shot): DEMAND the gated fields up-front. Appended AFTER the
        # marker line above so persona routing is unaffected; empty (a no-op) when both
        # l2_settings flags are off, collapsing L2 back to a plain L1 one-shot.
        if level == 2:
            prompt += _l2_plan_instruction(config.planning.l2_settings)
        plan = await self._dispatch_plan(
            runner, "planner", prompt, task, worktree, knowledge, rules
        )
        # L1 degradation (shared verbatim by L2): a dispatch error / no fence / parse
        # failure yields no preamble — never a task failure — matching today's behavior.
        if plan is None:
            return ""
        await repo.update_task_fields(self._db, task.id, plan=plan)
        await self._bus.emit(
            type=EventType.plan_artifact, project_id=task.project_id, task_id=task.id,
            payload={"plan": plan},
        )
        preamble = (
            "\n\n=== PLAN (from the Planner — follow its scope strictly) ===\n"
            f"```json\n{json.dumps(plan)}\n```\n=== END PLAN ===\n"
        )
        # L2: fold a clearly-labeled corrective note into the preamble for any demanded
        # field the planner still omitted, so the developer must supply it (vs. failing).
        if level == 2:
            preamble += _l2_corrective_notes(plan, config.planning.l2_settings)
        return preamble

    async def _dispatch_plan(
        self,
        runner: AgentRunner,
        agent: str,
        prompt: str,
        task: Task,
        worktree: Path,
        knowledge: str,
        rules: str,
    ) -> dict[str, Any] | None:
        """Run a planning persona and parse its first ```json fenced block.

        Returns the parsed plan object, or ``None`` when the agent dispatch
        fails, emits no fenced JSON block, or the block fails to JSON-parse.
        This ``None`` contract is the shared degradation primitive consumed by
        the higher scale-adaptive planning levels (L2/L3/L4).
        """
        result = await runner.run(
            agent=agent,
            prompt=prompt,
            task_id=task.id,
            worktree=worktree,
            repo_knowledge=knowledge,
            project_rules=rules,
        )
        if not result.ok:
            return None
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", result.text, re.DOTALL)
        if not match:
            return None
        try:
            parsed: dict[str, Any] = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed

    async def _run_l3(
        self,
        runner: AgentRunner,
        task: Task,
        worktree: Path,
        knowledge: str,
        rules: str,
        config: ConclaveConfig,
    ) -> str:
        """Three-agent sequential (BMad L3) planning: PM → Architect-as-Planner → Planner.

        Each stage is flag-gated by :class:`~conclave.config.models.L3Settings`.
        Intermediate ``None`` results are **skipped**; a ``None`` from the final
        planner stage degrades to an **empty preamble** (L1 degradation, never a
        task failure). Collected sections are assembled into a combined plan dict
        and a labeled preamble, both persisted and emitted.
        """
        l3 = config.planning.l3_settings
        sections: list[tuple[str, str, bool]] = [
            ("pm", "PRD-lite", l3.produce_prd),
            ("architect-as-planner", "Architecture Note", l3.produce_arch_note),
            ("planner", "Story/Plan", l3.decompose_into_stories),
        ]
        collected: list[tuple[str, dict[str, Any]]] = []
        prior_snippets: list[str] = []

        for agent, label, enabled in sections:
            if not enabled:
                continue

            prompt = task.request
            if prior_snippets:
                prompt += "\n\nPrior planning artifacts:\n" + "\n\n".join(prior_snippets)
            # The planner stage is the final, story/plan-producing dispatch — keep the
            # shared "Produce a structured plan" phrase so the FakeProvider (and any
            # routing layer keyed on it) can identify the persona.
            if agent == "planner":
                prompt += "\n\nProduce a structured plan (story/plan JSON) per your system prompt."
            else:
                prompt += "\n\nProduce a " + label.lower() + " section per your system prompt."

            plan = await self._dispatch_plan(
                runner, agent, prompt, task, worktree, knowledge, rules
            )
            if plan is None:
                # Intermediate stages (pm, architect-as-planner): skip-and-continue,
                # omitting that section. Final story/plan stage (planner): fall back
                # to an L1-style empty preamble without raising.
                if agent == "planner":
                    return ""
                continue

            collected.append((label, plan))
            prior_snippets.append(
                f"[{label}]\n```json\n{json.dumps(plan, indent=2)}\n```"
            )

        if not collected:
            return ""

        # Assemble combined plan dict with stable section keys.
        key_map = {
            "PRD-lite": "prd_lite",
            "Architecture Note": "architecture_note",
            "Story/Plan": "story_plan",
        }
        combined: dict[str, Any] = {}
        for label_, data in collected:
            combined[key_map[label_]] = data

        await repo.update_task_fields(self._db, task.id, plan=combined)
        await self._bus.emit(
            type=EventType.plan_artifact,
            project_id=task.project_id,
            task_id=task.id,
            payload={"plan": combined},
        )

        # Build labeled preamble so the developer sees every enabled section.
        parts = ["\n\n=== L3 PLAN (Scale-Adaptive) ===\n"]
        for label_, data in collected:
            parts.append(
                f"\n--- {label_} ---\n```json\n{json.dumps(data, indent=2)}\n```\n"
            )
        parts.append("=== END L3 PLAN ===\n")
        return "".join(parts)

    async def _run_l4(
        self,
        runner: AgentRunner,
        task: Task,
        worktree: Path,
        knowledge: str,
        rules: str,
        config: ConclaveConfig,
    ) -> str | object:
        """Epic-decomposition (BMad L4) planning path.

        Gated by :attr:`L4Settings.auto_create_children`. Dispatches a decomposer
        persona, parses ``child_tasks`` from the JSON output, and calls the shared
        :func:`~conclave.db.repositories.create_child_tasks` helper.

        **Happy path** — valid decomposition produced:
        1. Emits :attr:`EventType.plan_artifact` with the decomposition JSON.
        2. Creates child tasks (capped at ``max_child_tasks``).
        3. Emits :attr:`EventType.plan_decomposition_complete` with child ids.
        4. Marks the epic terminal (:meth:`repo.set_task_state` → ``done``).
        5. Returns ``_L4_DECOMPOSED`` sentinel — the caller short-circuits the
           entire retry loop (no developer/review dispatch).

        **Fallback** — dispatch fails, ``child_tasks`` empty/absent, or no valid
        children created:
        1. Emits :attr:`EventType.plan_decomposition_fallback`.
        2. Returns ``""`` (empty string) — the epic falls through to the normal
           L1 developer/review loop so the work is still attempted.
        """
        l4 = config.planning.l4_settings
        if not l4.auto_create_children:
            return ""

        prompt = (
            f"{task.request}\n\n"
            "Decompose this epic into child tasks. "
            "Output a JSON block with a `child_tasks` array "
            "where each entry has a non-empty `title` and `description`. "
            f"Create at most {l4.max_child_tasks} children."
        )
        plan = await self._dispatch_plan(
            runner, "decomposer", prompt, task, worktree, knowledge, rules
        )

        # --- dispatch failed ---
        if plan is None:
            await self._bus.emit(
                type=EventType.plan_decomposition_fallback,
                project_id=task.project_id,
                task_id=task.id,
                payload={"reason": "dispatch returned None"},
            )
            return ""

        child_tasks = plan.get("child_tasks") if isinstance(plan, dict) else None
        if not isinstance(child_tasks, list) or not child_tasks:
            await self._bus.emit(
                type=EventType.plan_decomposition_fallback,
                project_id=task.project_id,
                task_id=task.id,
                payload={"reason": "empty or absent child_tasks in decomposition"},
            )
            return ""

        # Persist and emit the decomposition plan so the UI/audit stream captures it.
        await repo.update_task_fields(self._db, task.id, plan=plan)
        await self._bus.emit(
            type=EventType.plan_artifact,
            project_id=task.project_id,
            task_id=task.id,
            payload={"plan": plan},
        )

        # Create the child tasks (sanitized + autonomy-gated via the shared helper).
        created_ids = await repo.create_child_tasks(
            self._db,
            parent_task=task,
            children=child_tasks,
            max_children=l4.max_child_tasks,
            project_id=task.project_id,
        )

        if not created_ids:
            await self._bus.emit(
                type=EventType.plan_decomposition_fallback,
                project_id=task.project_id,
                task_id=task.id,
                payload={
                    "reason": "create_child_tasks produced zero valid children "
                    f"from {len(child_tasks)} raw entries"
                },
            )
            return ""

        # --- happy path: decomposition succeeded ---
        await repo.set_task_state(self._db, task.id, TaskState.done)
        await self._bus.emit(
            type=EventType.plan_decomposition_complete,
            project_id=task.project_id,
            task_id=task.id,
            payload={"child_count": len(created_ids), "child_ids": created_ids},
        )

        return _L4_DECOMPOSED

    async def _review(
        self,
        runner: AgentRunner,
        task: Task,
        project_id: str,
        worktree: Path,
        current_diff: str,
        attempt: int,
        config: ConclaveConfig,
        knowledge: str,
        rules: str,
    ) -> tuple[bool, str]:
        pipeline = get_agent_pipeline(current_diff, config.agents)
        await self._bus.emit(
            type=EventType.pipeline_derived, project_id=project_id, task_id=task.id,
            payload={"pipeline": pipeline},
        )
        reviewer_prompt = f"Review the changes made for this task:\n{task.request}"
        for agent in pipeline:
            result = await runner.run(
                agent=agent,
                prompt=reviewer_prompt,
                task_id=task.id,
                worktree=worktree,
                repo_knowledge=knowledge,
                project_rules=rules,
            )
            if result.ok:
                verdict = parse_verdict(result.text)
            else:
                verdict = ParsedVerdict(
                    verdict="fail", reason=f"{agent} agent error: {result.error}", source="none"
                )
            warnings: list[str] = []
            if config.experimental.grounding_checks:
                verdict, warnings = check_grounding(verdict, current_diff, worktree)
            grounded = sum(
                1
                for e in verdict.evidence
                if isinstance(e, dict)
                and isinstance(e.get("file"), str)
                and e["file"] in current_diff
            )
            await repo.add_verdict(
                self._db,
                task_id=task.id,
                attempt=attempt,
                agent=agent,
                verdict=verdict.verdict,
                reason=verdict.reason,
                source=verdict.source,
                grounded_count=grounded,
                evidence=verdict.evidence,
            )
            for warning in warnings:
                await self._bus.emit(
                    type=EventType.grounding_warning, project_id=project_id, task_id=task.id,
                    agent=agent, payload={"warning": warning},
                )
            await self._bus.emit(
                type=EventType.verdict, project_id=project_id, task_id=task.id, agent=agent,
                payload={"verdict": verdict.verdict, "reason": verdict.reason},
            )
            if verdict.verdict in ("fail", "block"):
                header = f"{agent.upper()} FEEDBACK ({verdict.verdict.upper()})"
                return True, f"\n{header}:\n{verdict.reason}\n"
        return False, ""

    async def _finish_success(
        self,
        task: Task,
        wm: WorktreeManager,
        task_branch: str,
        repo_path: Path,
        target_branch: str,
        worktree: Path,
        config: ConclaveConfig,
        attempts: int,
    ) -> bool:
        _, status = await run_git(worktree, "status", "--porcelain")
        if status.strip():
            await run_git(worktree, "add", "-A")
            await run_git(worktree, "commit", "-m", _commit_message(task))
        await repo.update_task_fields(self._db, task.id, branch=task_branch)
        await self._bus.emit(
            type=EventType.task_committed, project_id=task.project_id, task_id=task.id,
            payload={"branch": task_branch},
        )

        merged = False
        if config.execution.auto_merge:
            merged = await self._merge(repo_path, target_branch, task_branch)
            if merged:
                await self._bus.emit(
                    type=EventType.task_merged, project_id=task.project_id, task_id=task.id,
                    payload={"target": target_branch},
                )

        await repo.set_task_state(self._db, task.id, TaskState.done)
        await repo.update_task_fields(
            self._db, task.id, result_summary=f"completed in {attempts} attempt(s); merged={merged}"
        )
        await self._bus.emit(
            type=EventType.task_done, project_id=task.project_id, task_id=task.id,
            payload={"attempts": attempts, "merged": merged},
        )
        # Keep the branch if unmerged (work preserved); drop it once merged.
        await wm.cleanup(task.id, task_branch if merged else None)
        return True

    async def _finish_failure(
        self, task: Task, wm: WorktreeManager, task_branch: str, timed_out: bool, attempts: int
    ) -> bool:
        reason = "timeout" if timed_out else "max_retries"
        await repo.set_task_state(self._db, task.id, TaskState.failed)
        await repo.update_task_fields(
            self._db, task.id, result_summary=f"failed ({reason}) after {attempts} attempt(s)"
        )
        await self._bus.emit(
            type=EventType.task_failed, project_id=task.project_id, task_id=task.id,
            payload={"reason": reason, "attempts": attempts},
        )
        await wm.cleanup(task.id, task_branch)
        return False

    async def _fail_early(
        self, task: Task, wm: WorktreeManager, task_branch: str, message: str
    ) -> bool:
        await repo.set_task_state(self._db, task.id, TaskState.failed)
        await repo.update_task_fields(self._db, task.id, result_summary=message)
        await self._bus.emit(
            type=EventType.task_failed, project_id=task.project_id, task_id=task.id,
            payload={"reason": message},
        )
        await wm.cleanup(task.id, task_branch)
        return False

    async def _merge(self, repo_path: Path, target_branch: str, task_branch: str) -> bool:
        """Merge ``task_branch`` into ``target_branch`` without disturbing the user's checkout
        when possible (fast-forward via ``update-ref``); otherwise merge in the checked-out
        target. Returns whether the target now contains the task work."""
        ancestor_code, _ = await run_git(
            repo_path, "merge-base", "--is-ancestor", target_branch, task_branch
        )
        is_ff = ancestor_code == 0
        _, current = await run_git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
        current_branch = current.strip()
        _, task_sha = await run_git(repo_path, "rev-parse", task_branch)
        task_sha = task_sha.strip()

        if is_ff and current_branch != target_branch:
            code, _ = await run_git(
                repo_path, "update-ref", f"refs/heads/{target_branch}", task_sha
            )
            return code == 0

        if current_branch == target_branch:
            code, _ = await run_git(repo_path, "merge", "--ff-only", task_branch)
            if code != 0:
                code, _ = await run_git(
                    repo_path, "merge", "--no-ff", "-m",
                    f"merge(conclave): {task_branch}", task_branch,
                )
            return code == 0

        # target is not checked out and history diverged: merge via a temp worktree.
        merge_path = wm_merge_path(repo_path, target_branch)
        await run_git(repo_path, "worktree", "add", str(merge_path), target_branch)
        try:
            code, _ = await run_git(
                merge_path, "merge", "--no-ff", "-m", f"merge(conclave): {task_branch}", task_branch
            )
        finally:
            await run_git(repo_path, "worktree", "remove", "--force", str(merge_path))
            await run_git(repo_path, "worktree", "prune")
        return code == 0


# --- module helpers ---------------------------------------------------------


def wm_merge_path(repo_path: Path, target_branch: str) -> Path:
    safe = target_branch.replace("/", "_")
    return repo_path.parent / f".conclave-merge-{repo_path.name}-{safe}"


def _commit_message(task: Task) -> str:
    first_line = next((ln.strip() for ln in task.request.splitlines() if ln.strip()), task.id)
    subject = task.title or first_line
    return f"feat(conclave): {subject[:72]}\n\n{task.request}"


def _diff_stat(diff: str) -> str:
    lines = diff.splitlines()
    files = sum(1 for ln in lines if ln.startswith("diff --git"))
    adds = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    dels = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    return f"{files} file(s), +{adds}/-{dels}"


def _read_project_rules(worktree: Path) -> str:
    for name in _PROJECT_RULE_FILES:
        candidate = worktree / name
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="replace")
    return ""


def _test_command(config: ConclaveConfig, knowledge: dict[str, Any] | None) -> str | None:
    if config.execution.baseline_test_command:
        return config.execution.baseline_test_command
    if knowledge:
        commands = knowledge.get("commands")
        if isinstance(commands, dict):
            test = commands.get("test")
            if isinstance(test, str) and test.strip():
                return test
    return None


def _l2_plan_instruction(l2: L2Settings) -> str:
    """Planner-prompt augmentation for the enhanced one-shot (L2) path.

    Demands each gated field so the planner front-loads it. Returns '' when neither flag
    is set, so an all-False :class:`L2Settings` leaves the prompt byte-identical to L1.
    """
    demands: list[str] = []
    if l2.require_acceptance_criteria:
        demands.append("non-empty `acceptance_criteria`")
    if l2.require_risk_assessment:
        demands.append("non-empty `risks`")
    if not demands:
        return ""
    return "\n\nL2 REQUIREMENT: the plan JSON MUST include " + " and ".join(demands) + "."


def _l2_corrective_notes(plan: dict[str, Any], l2: L2Settings) -> str:
    """Corrective notes for any L2-demanded field the planner left missing/empty.

    A field counts as missing when ``not plan.get(field)`` — absent OR an empty list. The
    real planner persona emits both fields, so the happy path returns '' and the
    developer's plan preamble is byte-identical to L1.
    """
    notes: list[str] = []
    if l2.require_acceptance_criteria and not plan.get("acceptance_criteria"):
        notes.append(
            "L2 NOTE: planner omitted acceptance_criteria — "
            "developer must define and verify them."
        )
    if l2.require_risk_assessment and not plan.get("risks"):
        notes.append(
            "L2 NOTE: planner omitted risks — developer must assess and mitigate them."
        )
    if not notes:
        return ""
    return "\n" + "\n".join(notes) + "\n"
