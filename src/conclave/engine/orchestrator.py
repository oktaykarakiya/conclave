"""The orchestration loop (port of team-ai's ``process_task``).

Per task: create an isolated worktree from the target branch → snapshot baseline
failures → optional planner → retry loop (developer → diff-derived reviewers with
grounded verdicts → green-gate, feeding failures back with cross-attempt memory) →
on success commit + merge into the target branch; on failure clean up. All state is
in SQLite and every step emits an event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from ..config import ConclaveConfig, load_project_config, resolve_agent
from ..db import Database, Task, TaskState
from ..db import repositories as repo
from ..events import EventBus, EventType
from ..providers import AgentResult, Provider
from ..repo_intel.knowledge import render_preamble
from .baseline import build_baseline_preamble
from .gate import run_tests
from .gitio import run_git, run_shell
from .memory import AttemptMemory
from .pipeline import get_agent_pipeline
from .runner import AgentRunner
from .verdict import ParsedVerdict, check_grounding, parse_verdict
from .worktree import WorktreeError, WorktreeManager

_PROJECT_RULE_FILES = ("CONCLAVE.md", "CLAUDE.md")

# A single reviewer dispatch can transiently return empty/non-ok (provider hiccup).
# Retry it a few times before giving up, so one flaky call doesn't discard the
# developer's work and re-run the whole develop→review loop.
_REVIEWER_DISPATCH_RETRIES = 2
_REVIEWER_RETRY_BACKOFF_S = 3.0

logger = logging.getLogger("conclave.engine.orchestrator")


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

        # Wrap the entire body after worktree creation so ANY unexpected exception
        # transitions the task to failed, cleans the worktree, and lets the worker
        # continue with the next task — never stranding a task in_progress or leaking
        # a worktree.
        try:
            _, sha = await run_git(worktree, "rev-parse", "HEAD")
            checkpoint = sha.strip()

            # Provision the worktree environment ONCE (e.g. venv + deps) before any agent
            # runs, so developer self-checks, reviewer verification, and the green-gate all
            # share one toolchain. Without this, agents fall back to host-global tools and
            # report "clean" against a different environment than the gate enforces.
            setup_cmd = config.execution.setup_command
            if setup_cmd:
                await self._bus.emit(
                    type=EventType.log,
                    project_id=project.id,
                    task_id=task.id,
                    payload={"stage": "setup", "message": "provisioning worktree environment"},
                )
                rc, out = await run_shell(worktree, setup_cmd, timeout_seconds=900)
                if rc != 0:
                    return await self._fail_early(
                        task, wm, task_branch,
                        f"worktree setup failed (exit {rc}):\n{out[-1000:]}",
                    )

            knowledge_row = await repo.current_repo_knowledge(self._db, project.id)
            knowledge = render_preamble(knowledge_row.knowledge) if knowledge_row else ""
            rules = _read_project_rules(worktree)
            if setup_cmd:
                rules += (
                    "\n\n## Worktree environment (MANDATORY)\n"
                    "A provisioned virtualenv exists at `.venv/` in the worktree root with all "
                    "dependencies installed. Run EVERY verification through it so your checks "
                    "match the green-gate exactly:\n"
                    "- `.venv/bin/pytest -q`\n"
                    "- `.venv/bin/mypy`\n"
                    "- `.venv/bin/ruff check src tests`\n"
                    "Do NOT use system-wide `pytest`/`mypy`/`ruff` — they are a different "
                    "toolchain and will disagree with the gate."
                )
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
                    # Preserve the provisioned .venv across attempts; rebuilding it each retry
                    # is slow and pointless.
                    await run_git(worktree, "clean", "-fd", "-e", ".venv")

                dev_prompt = task.request + plan_preamble + baseline_preamble
                if use_memory:
                    dev_prompt += memory.build_preamble()
                if feedback:
                    dev_prompt += (
                        "\n\nLATEST REVIEWER FEEDBACK (must fix these issues):\n" + feedback
                    )
                    if previous_diff:
                        dev_prompt += (
                            "\n\nYOUR IMMEDIATELY PRIOR DIFF:\n```diff\n"
                            + previous_diff + "\n```"
                        )

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
                # ENG-2: snapshot the exact staged tree the reviewers are about to review.
                # Reviewers run with --dangerously-skip-permissions and CAN write to the
                # worktree, but anything they touch falls outside ``current_diff`` (so grounding
                # never sees it). write-tree records the index as an immutable tree object
                # without moving HEAD or creating a commit; after review we restore the worktree
                # to exactly this tree, so the gated+committed content is byte-for-byte what was
                # reviewed and a reviewer cannot smuggle in unreviewed changes.
                _, review_tree = await run_git(worktree, "write-tree")
                review_tree = review_tree.strip()
                await repo.end_attempt(self._db, attempt_id, diff_stat=_diff_stat(current_diff))

                failed, feedback = await self._review(
                    runner, task, project.id, worktree, current_diff, attempts, config, knowledge,
                    rules,
                )
                if failed:
                    await self._bus.emit(
                        type=EventType.attempt_failed,
                        project_id=project.id,
                        task_id=task.id,
                        payload={"n": attempts, "stage": "review"},
                    )
                    continue

                # ENG-2: the review passed, but reviewers may have written to the worktree.
                # Restore it to the exact tree that was reviewed BEFORE gating/committing so
                # unreviewed reviewer edits can neither be tested nor merged. This runs on the
                # success path unconditionally (outside the gate block below): even with no test
                # command, reviewer strays must not leak into the commit.
                #   read-tree    — reset the index to the reviewed tree
                #   checkout-index — force-rewrite the working tree from that index, overwriting
                #                    reviewer edits and restoring files reviewers deleted
                #   clean -fd    — drop reviewer-created strays (now untracked vs the index),
                #                  keeping the provisioned .venv as the retry loop does
                await run_git(worktree, "read-tree", review_tree)
                await run_git(worktree, "checkout-index", "-a", "-f")
                await run_git(worktree, "clean", "-fd", "-e", ".venv")

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
                            payload={
                                "n": attempts, "stage": "gate", "exit_code": gate.exit_code,
                            },
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
        except Exception:
            logger.exception("Unhandled exception processing task %s", task.id)
            return await self._fail_early(
                task, wm, task_branch,
                f"unhandled exception processing task {task.id}",
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

    async def _dispatch_reviewer(
        self,
        runner: AgentRunner,
        agent: str,
        prompt: str,
        task: Task,
        worktree: Path,
        knowledge: str,
        rules: str,
    ) -> tuple[AgentResult, ParsedVerdict]:
        """Run one reviewer; retry ONLY a genuinely empty/timeout/CLI-missing dispatch.

        ENG-4: the retry signal is "could we extract a verdict?", not the provider's
        ``result.ok`` success-hint heuristic. We parse the output each try and stop early
        when it carries an answer:
          * a real verdict (``source != "none"``) is returned immediately even if the
            process exited non-zero — re-dispatching a parseable pass/fail/block/decline
            just burns opus calls; and
          * any non-empty text is returned too — usable-but-unparseable output is not a
            transient infra failure (the caller records it as a non-blocking 'unknown').
        Only a response with no verdict AND no text (genuine empty/timeout/CLI-missing) is
        retried with the existing backoff. ``parsed`` is seeded so it is never unbound.
        """
        result = AgentResult(ok=False, error="not dispatched")
        parsed = ParsedVerdict()
        for attempt in range(_REVIEWER_DISPATCH_RETRIES + 1):
            result = await runner.run(
                agent=agent,
                prompt=prompt,
                task_id=task.id,
                worktree=worktree,
                repo_knowledge=knowledge,
                project_rules=rules,
            )
            parsed = parse_verdict(result.text)
            if parsed.source != "none" or result.text.strip():
                return result, parsed
            if attempt < _REVIEWER_DISPATCH_RETRIES:
                logger.info(
                    "reviewer %s returned no usable response (try %d/%d); retrying",
                    agent, attempt + 1, _REVIEWER_DISPATCH_RETRIES + 1,
                )
                await asyncio.sleep(_REVIEWER_RETRY_BACKOFF_S)
        return result, parsed

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
        passes = 0
        for agent in pipeline:
            # ENG-4: _dispatch_reviewer already parsed the output; the verdict is used as-is.
            # A parseable verdict (even one whose process exited non-zero) and any usable
            # text both come back here untouched. Only a genuinely empty response (no verdict
            # AND no text, i.e. exhausted retries on a real outage) is downgraded to a
            # non-blocking 'unknown' — a single reviewer that can't run is NOT a code defect,
            # so it must not fail the whole attempt (mirrors the grounding downgrade).
            result, verdict = await self._dispatch_reviewer(
                runner, agent, reviewer_prompt, task, worktree, knowledge, rules
            )
            if verdict.source == "none" and not result.text.strip():
                verdict = ParsedVerdict(
                    verdict="unknown",
                    reason=f"{agent} agent unavailable after retries: {result.error}",
                    source="none",
                )
                await self._bus.emit(
                    type=EventType.grounding_warning, project_id=project_id, task_id=task.id,
                    agent=agent,
                    payload={
                        "warning": f"{agent} dispatch failed after retries; skipped (non-blocking)"
                    },
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
            if verdict.verdict in ("fail", "block", "decline"):
                header = f"{agent.upper()} FEEDBACK ({verdict.verdict.upper()})"
                return True, f"\n{header}:\n{verdict.reason}\n"
            if verdict.verdict == "pass":
                passes += 1
        # ENG-3: never merge on an all-'unknown' round. If reviewers were derived but
        # none rendered a usable PASS — e.g. the model backend was rate-limited/degraded
        # and every reviewer fell back to non-blocking 'unknown' — the review tier is
        # effectively offline. Fail the attempt so the retry loop re-runs it (giving the
        # backend time to recover) rather than auto-merging unvetted code.
        if pipeline and passes == 0:
            return True, (
                "\nREVIEW INCONCLUSIVE: no reviewer returned a usable verdict "
                "(all 'unknown' — the model backend was likely unavailable). "
                "Not merging without at least one grounded review; retrying.\n"
            )
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

        # One transaction: flip state→done and write the result summary together so the
        # lifecycle row is never observed half-updated.
        await repo.finalize_task(
            self._db, task.id, state=TaskState.done,
            result_summary=f"completed in {attempts} attempt(s); merged={merged}",
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
        # One transaction: fail the task, record why, and block its descendants together so
        # a crash can't leave the task failed while children stay claimable. Announce only
        # once the transition is durably committed.
        blocked = await repo.finalize_task(
            self._db, task.id, state=TaskState.failed,
            result_summary=f"failed ({reason}) after {attempts} attempt(s)",
            block_children=True,
        )
        await self._bus.emit(
            type=EventType.task_failed, project_id=task.project_id, task_id=task.id,
            payload={"reason": reason, "attempts": attempts},
        )
        if blocked:
            logger.info("blocked %d descendant tasks after %s failed", blocked, task.id)
        await wm.cleanup(task.id, task_branch)
        return False

    async def _fail_early(
        self, task: Task, wm: WorktreeManager, task_branch: str, message: str
    ) -> bool:
        # One transaction: fail + record the reason + block descendants atomically, then
        # announce once it is durably committed.
        blocked = await repo.finalize_task(
            self._db, task.id, state=TaskState.failed, result_summary=message,
            block_children=True,
        )
        await self._bus.emit(
            type=EventType.task_failed, project_id=task.project_id, task_id=task.id,
            payload={"reason": message},
        )
        if blocked:
            logger.info("blocked %d descendant tasks after %s failed early", blocked, task.id)
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
