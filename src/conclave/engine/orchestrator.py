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
from typing import Any

from ..config import ConclaveConfig, load_project_config, resolve_agent
from ..db import Database, Task, TaskState
from ..db import repositories as repo
from ..events import EventBus, EventType
from ..providers import Provider
from .baseline import build_baseline_preamble
from .gate import run_tests
from .gitio import run_git
from .memory import AttemptMemory
from .pipeline import get_agent_pipeline
from .runner import AgentRunner
from .verdict import ParsedVerdict, check_grounding, parse_verdict
from .worktree import WorktreeError, WorktreeManager

_PROJECT_RULE_FILES = ("CONCLAVE.md", "CLAUDE.md")


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

        try:
            worktree = await wm.create(task.id, target_branch, task_branch)
        except WorktreeError as exc:
            return await self._fail_early(task, wm, task_branch, f"worktree setup failed: {exc}")

        _, sha = await run_git(worktree, "rev-parse", "HEAD")
        checkpoint = sha.strip()

        knowledge_row = await repo.current_repo_knowledge(self._db, project.id)
        knowledge = _render_knowledge(knowledge_row.knowledge) if knowledge_row else ""
        rules = _read_project_rules(worktree)
        test_command = _test_command(config, knowledge_row.knowledge if knowledge_row else None)
        gate_timeout = resolve_agent(config, "tester").timeout_minutes * 60

        baseline_preamble = await self._baseline(
            project.id, worktree, checkpoint, target_branch, test_command, gate_timeout
        )
        plan_preamble = await self._maybe_plan(
            runner, task, worktree, knowledge, rules, baseline_preamble, config
        )

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
    ) -> str:
        if not config.experimental.planner_enabled:
            return ""
        threshold = config.experimental.auto_planner_char_threshold
        should_plan = (
            task.use_planner
            if task.use_planner is not None
            else len(task.request) >= threshold
        )
        if not should_plan:
            return ""
        prompt = (
            f"{task.request}{baseline_preamble}\n\n"
            "Produce a structured plan per your system prompt."
        )
        result = await runner.run(
            agent="planner",
            prompt=prompt,
            task_id=task.id,
            worktree=worktree,
            repo_knowledge=knowledge,
            project_rules=rules,
        )
        if not result.ok:
            return ""
        match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", result.text, re.DOTALL)
        if not match:
            return ""
        try:
            plan = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            return ""
        await repo.update_task_fields(self._db, task.id, plan=plan)
        await self._bus.emit(
            type=EventType.plan_artifact, project_id=task.project_id, task_id=task.id,
            payload={"plan": plan},
        )
        return (
            "\n\n=== PLAN (from the Planner — follow its scope strictly) ===\n"
            f"```json\n{match.group(1)}\n```\n=== END PLAN ===\n"
        )

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


def _render_knowledge(knowledge: dict[str, Any]) -> str:
    if not knowledge:
        return ""
    parts: list[str] = []
    summary = knowledge.get("architecture_summary")
    if isinstance(summary, str) and summary:
        parts.append(summary)
    langs = knowledge.get("languages")
    if isinstance(langs, list) and langs:
        parts.append("Languages: " + ", ".join(str(x) for x in langs))
    commands = knowledge.get("commands")
    if isinstance(commands, dict) and commands:
        rendered = ", ".join(f"{k}: `{v}`" for k, v in commands.items() if v)
        if rendered:
            parts.append("Commands — " + rendered)
    conventions = knowledge.get("conventions")
    if isinstance(conventions, list) and conventions:
        parts.append("Conventions:\n" + "\n".join(f"- {c}" for c in conventions))
    return "\n".join(parts)
