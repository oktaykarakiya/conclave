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
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..config import ConclaveConfig, load_project_config, resolve_agent
from ..db import Database, Task, TaskState
from ..db import repositories as repo
from ..events import EventBus, EventType, build_notification_sink
from ..providers import AgentResult, Provider
from .baseline import build_baseline_preamble
from .gate import apply_quarantine, run_tests
from .gitio import run_git, run_shell
from .memory import AttemptMemory
from .pipeline import get_agent_pipeline
from .runner import AgentRunner
from .verdict import ParsedVerdict, check_grounding, parse_verdict
from .worktree import WorktreeError, WorktreeManager

# opencode reads AGENTS.md natively, so it leads the project-rule fallback list. CLAUDE.md
# and CONCLAVE.md remain supported for repos that still carry them.
_PROJECT_RULE_FILES = ("AGENTS.md", "CONCLAVE.md", "CLAUDE.md")

# A single reviewer dispatch can transiently return empty/non-ok (provider hiccup).
# Retry it a few times before giving up, so one flaky call doesn't discard the
# developer's work and re-run the whole develop→review loop.
_REVIEWER_DISPATCH_RETRIES = 2
_REVIEWER_RETRY_BACKOFF_S = 3.0

# Hard cap on diff characters injected into agent prompts to prevent context-window
# overflow from very large diffs. The truncation marker includes the original size so
# the agent can still reason about scope.
_MAX_DIFF_CHARS = 40_000

logger = logging.getLogger("conclave.engine.orchestrator")


class MergeResult(StrEnum):
    """Three-way outcome for :meth:`Orchestrator._merge`.

    ``success`` — the target now contains the task work.
    ``conflict`` — a real merge conflict; task branch is preserved, task is failed.
    ``error`` — a transient infra error (e.g. concurrent ref advance); retryable.
    """

    success = "success"
    conflict = "conflict"
    error = "error"


class Orchestrator:
    def __init__(
        self, db: Database, bus: EventBus, provider: Provider, conclave_home: Path
    ) -> None:
        self._db = db
        self._bus = bus
        self._provider = provider
        self._home = conclave_home
        # Serialize merges into the same target branch so two tasks merging
        # "main" concurrently can never race on a shared worktree path or
        # clobber each other's ref update. Reference-counted (see _merge) so the
        # lock dict stays bounded — an entry is evicted once no merge references it.
        self._merge_locks: dict[str, asyncio.Lock] = {}
        self._merge_lock_refs: dict[str, int] = {}
        # Per-task cancellation events — set by the daemon through
        # :meth:`request_cancel` and checked by :meth:`process_task` between
        # every pipeline stage for cooperative cancellation.
        self._cancel_events: dict[str, asyncio.Event] = {}

    async def recover(self, project_id: str) -> tuple[int, int]:
        """Crash recovery: return orphaned in_progress tasks to approved and re-block
        descendants of failed/blocked parents. Returns ``(recovered, reblocked)``."""
        return await repo.recover_in_progress(self._db, project_id)

    def request_cancel(self, task_id: str) -> bool:
        """Signal cooperative cancellation for *task_id*.

        Returns ``True`` when a cancellation event was set (the task was in-flight),
        ``False`` when no event exists (the task is not currently being processed).
        The caller (daemon/cancel endpoint) still transitions non-running tasks
        (inbox/approved) directly — this only covers the in-progress path.
        """
        event = self._cancel_events.get(task_id)
        if event is None:
            return False
        event.set()
        return True

    async def process_task(self, task: Task, *, cancel_event: asyncio.Event | None = None) -> bool:
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
                rc, out = await run_shell(
                    worktree, setup_cmd,
                    timeout_seconds=config.execution.setup_timeout_seconds,
                )
                if rc != 0:
                    return await self._fail_early(
                        task, wm, task_branch,
                        f"worktree setup failed (exit {rc}):\n{out[-1000:]}",
                    )

            # Cooperative cancellation — check after setup, before baseline.
            if cancel_event is not None and cancel_event.is_set():
                return await self._finish_cancelled(task, wm, task_branch)

            # Repo context now comes from AGENTS.md (read by opencode natively and surfaced
            # through _read_project_rules), not Conclave's synthesized knowledge preamble.
            knowledge = ""
            rules = _read_project_rules(worktree)
            rules += _build_venv_guidance(worktree, config, None)
            test_command = _test_command(config, None)
            # Inject active quarantine exclusions so flaky quarantined tests
            # don't fail the baseline snapshot or the green-gate.
            test_command = await apply_quarantine(self._db, project.id, test_command)
            gate_timeout = resolve_agent(config, "tester").timeout_minutes * 60

            baseline_preamble = await self._baseline(
                project.id, worktree, checkpoint, target_branch, test_command, gate_timeout,
            )
            plan_preamble = await self._maybe_plan(
                runner, task, worktree, knowledge, rules, baseline_preamble, config,
                cancel_event=cancel_event,
            )

            # Cooperative cancellation — check after planning, before developer loop.
            if cancel_event is not None and cancel_event.is_set():
                return await self._finish_cancelled(task, wm, task_branch)

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
                if _check_budget(started, budget):
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
                    # Preserve the provisioned .venv across attempts; rebuilding it each
                    # retry is slow and pointless.  .gitignore is the belt (the ideal
                    # way to protect .venv from git-clean); `-e .venv/` is the suspenders
                    # — the trailing-slash directory exclusion works even when .gitignore
                    # is absent or doesn't cover the venv.
                    await run_git(worktree, "clean", "-fd", "-e", ".venv/")

                dev_prompt = task.request + plan_preamble + baseline_preamble
                if test_command:
                    # Hand the developer the EXACT authoritative gate so its own
                    # write→run→fix loop converges on what the orchestrator actually
                    # enforces — not a partial self-check that passes here but fails the gate.
                    dev_prompt += (
                        "\n\nGREEN-GATE (authoritative — exactly what the orchestrator runs "
                        "to accept your work). Before you finish, run this EXACT command in "
                        "the worktree and iterate (write → run → read failures → fix → re-run) "
                        "until it passes with NO NEW failures vs. the pre-existing baseline:\n"
                        f"```\n{test_command}\n```\n"
                    )
                if use_memory:
                    dev_prompt += memory.build_preamble()
                if feedback:
                    dev_prompt += (
                        "\n\nLATEST REVIEWER FEEDBACK (must fix these issues):\n" + feedback
                    )
                    if previous_diff:
                        dev_prompt += (
                            "\n\nYOUR IMMEDIATELY PRIOR DIFF:\n```diff\n"
                            + _truncate_diff(previous_diff) + "\n```"
                        )

                # HARD cap: abort the task if the wall-clock budget has been exceeded
                # before launching the developer agent. The top-of-loop check catches
                # inter-attempt overruns; this one catches a series of fast attempts
                # whose cumulative setup (planning, baseline) ate the budget.
                if _check_budget(started, budget):
                    timed_out = True
                    failed = True
                    break

                # Cooperative cancellation — check before developer dispatch.
                if cancel_event is not None and cancel_event.is_set():
                    return await self._finish_cancelled(task, wm, task_branch)

                dev = await runner.run(
                    agent="developer",
                    prompt=dev_prompt,
                    task_id=task.id,
                    worktree=worktree,
                    repo_knowledge=knowledge,
                    project_rules=rules,
                    cancel_event=cancel_event,
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
                current_diff = _truncate_diff(current_diff)
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

                # Cooperative cancellation — check after developer, before review.
                if cancel_event is not None and cancel_event.is_set():
                    return await self._finish_cancelled(task, wm, task_branch)

                failed, feedback, review_timed_out = await self._review(
                    runner, task, project.id, worktree, current_diff, attempts, config, knowledge,
                    rules, started, budget, cancel_event=cancel_event,
                )
                if review_timed_out:
                    timed_out = True
                    failed = True
                    break
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
                await run_git(worktree, "clean", "-fd", "-e", ".venv/")

                if test_command and config.execution.require_full_green:
                    # Cooperative cancellation — check before the gate (which may be slow).
                    if cancel_event is not None and cancel_event.is_set():
                        return await self._finish_cancelled(task, wm, task_branch)

                    # HARD cap: abort before running the test gate if the wall-clock
                    # budget has been exhausted. The gate itself may be slow; checking
                    # here prevents a very long test run from blowing past the cap.
                    if _check_budget(started, budget):
                        timed_out = True
                        failed = True
                        break

                    gate = await run_tests(worktree, test_command, timeout_seconds=gate_timeout)
                    if not gate.passed:
                        # ENG-7: distinguish infra failures (timeout / missing command)
                        # from real test failures.  Infra problems get one retry; if they
                        # persist the attempt is aborted — do NOT feed toolchain noise to
                        # the developer as code feedback.
                        if gate.outcome in ("timed_out", "missing_command"):
                            gate = await run_tests(
                                worktree, test_command, timeout_seconds=gate_timeout
                            )
                            if not gate.passed:
                                reason = f"infra_{gate.outcome}"
                                await self._bus.emit(
                                    type=EventType.attempt_failed,
                                    project_id=project.id,
                                    task_id=task.id,
                                    payload={
                                        "n": attempts, "stage": "gate", "reason": reason,
                                        "exit_code": gate.exit_code,
                                    },
                                )
                                failed = True
                                break  # infra failure — don't feed to developer
                        else:
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

            # A cancellation can land on the FINAL attempt: a per-stage check inside the
            # loop (e.g. _review) sets failed=True and the loop exits here rather than
            # returning _finish_cancelled. Route any pending cancellation to cancelled so
            # the task is never mislabelled as failed when the operator actually cancelled it.
            if cancel_event is not None and cancel_event.is_set():
                return await self._finish_cancelled(task, wm, task_branch)

            if failed or attempts == 0:
                # Best-effort post-mortem BEFORE cleanup, while the worktree + agent
                # context still exist. Never lets a post-mortem failure crash the worker.
                await self._maybe_post_mortem(
                    runner, task, worktree, knowledge, rules, config, feedback, attempts,
                    cancel_event=cancel_event,
                )
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
        finally:
            # Always clean the cancellation event so the dict doesn't leak memory
            # — harmless no-op when the entry was already removed by _finish_cancelled.
            self._cancel_events.pop(task.id, None)
            # Prune events/baselines unconditionally — every processed task, regardless of
            # outcome (success/failure/cancel/exception) and crucially even for projects with
            # NO test command (which never run a baseline). Folding GC into _baseline meant
            # those projects' events/baselines grew unbounded. Both DELETEs are cheap when
            # under the cap (the subquery returns all rows, so none match) and are best-effort:
            # a sweep failure must never mask the task's real result.
            try:
                await repo.gc_events(
                    self._db, project.id, keep=config.execution.retention_events_max
                )
                await repo.gc_baselines(self._db, project.id)
            except Exception:
                logger.warning("GC sweep failed for task %s", task.id, exc_info=True)
            # Fire the terminal-task notification last, after the outcome is durably written.
            # Reads the freshly-persisted task so the payload reflects the final state +
            # summary; inert unless a webhook is configured, and fully best-effort.
            await self._notify_terminal(project.id, task.id, config)

    async def _notify_terminal(
        self, project_id: str, task_id: str, config: ConclaveConfig
    ) -> None:
        """POST a compact notification for a task that finished done/failed (best-effort).

        Inert unless ``notifications.webhook_url`` is configured. Re-reads the task to get
        its durably-written terminal state/summary, fires only for ``done``/``failed``, and
        swallows every error so a notification can never affect task processing.
        """
        sink = build_notification_sink(config)
        if sink is None:
            return
        try:
            task = await repo.get_task(self._db, task_id)
            if task is None or task.state not in (TaskState.done, TaskState.failed):
                return
            await sink.notify(
                {
                    "event": f"task.{task.state.value}",
                    "task_id": task.id,
                    "project_id": project_id,
                    "state": task.state.value,
                    "title": task.title,
                    "result_summary": task.result_summary,
                }
            )
        except Exception:
            logger.warning("terminal notification failed for task %s", task_id, exc_info=True)

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
        # ENG-7: infra failures (timeout / missing command) must not be cached as
        # pre-existing test failures — they are toolchain noise, not code defects.
        if gate.passed or gate.outcome in ("timed_out", "missing_command"):
            failures = ""
        else:
            failures = gate.output
        await repo.save_baseline(self._db, project_id, checkpoint, failures)
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
        *,
        cancel_event: asyncio.Event | None = None,
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
            cancel_event=cancel_event,
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
        *,
        cancel_event: asyncio.Event | None = None,
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
                cancel_event=cancel_event,
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
        started: float,
        budget: float,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> tuple[bool, str, bool]:
        """Review the current diff through the derived agent pipeline.

        Returns ``(failed, feedback, timed_out)``. ``timed_out`` is ``True`` when the
        wall-clock budget was exceeded mid-review — the caller must abort the task, not
        retry the attempt.
        """
        pipeline = get_agent_pipeline(current_diff, config.agents)
        await self._bus.emit(
            type=EventType.pipeline_derived, project_id=project_id, task_id=task.id,
            payload={"pipeline": pipeline},
        )
        reviewer_prompt = f"Review the changes made for this task:\n{task.request}"
        passes = 0
        for agent in pipeline:
            # Cooperative cancellation — check between each reviewer dispatch.
            if cancel_event is not None and cancel_event.is_set():
                return True, "cancelled", False

            # HARD cap: abort the review if the wall-clock budget has been exceeded
            # before dispatching this reviewer. The orchestrator will fail the task
            # as timed-out rather than retrying the attempt.
            if _check_budget(started, budget):
                return True, "Wall-clock budget exceeded during review.", True

            # ENG-4: _dispatch_reviewer already parsed the output; the verdict is used as-is.
            # A parseable verdict (even one whose process exited non-zero) and any usable
            # text both come back here untouched. Only a genuinely empty response (no verdict
            # AND no text, i.e. exhausted retries on a real outage) is downgraded to a
            # non-blocking 'unknown' — a single reviewer that can't run is NOT a code defect,
            # so it must not fail the whole attempt (mirrors the grounding downgrade).
            result, verdict = await self._dispatch_reviewer(
                runner, agent, reviewer_prompt, task, worktree, knowledge, rules,
                cancel_event=cancel_event,
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
                return True, f"\n{header}:\n{verdict.reason}\n", False
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
            ), False
        return False, "", False

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

        if config.execution.auto_merge:
            merge_result = await self._merge(repo_path, target_branch, task_branch, task.id)
            if merge_result is MergeResult.success:
                await self._bus.emit(
                    type=EventType.task_merged, project_id=task.project_id, task_id=task.id,
                    payload={"target": target_branch},
                )
                merged = True
            elif merge_result is MergeResult.conflict:
                # Real merge conflict: task work committed but cannot be merged.
                # Mark failed, KEEP the branch so the work is preserved, and emit
                # a loud failure event so the operator can resolve it manually.
                summary = f"merge conflict into {target_branch}; branch {task_branch} preserved"
                await repo.finalize_task(
                    self._db, task.id, state=TaskState.failed, result_summary=summary,
                )
                await self._bus.emit(
                    type=EventType.task_failed, project_id=task.project_id, task_id=task.id,
                    payload={"reason": "merge_conflict", "target": target_branch},
                )
                await wm.cleanup(task.id, None)  # keep the branch
                return False
            else:
                # MergeResult.error — transient infra error (e.g. concurrent ref
                # advance the retry couldn't resolve). Mark failed but preserve the
                # branch so nothing is lost.
                summary = f"merge error into {target_branch}; branch {task_branch} preserved"
                await repo.finalize_task(
                    self._db, task.id, state=TaskState.failed, result_summary=summary,
                )
                await self._bus.emit(
                    type=EventType.task_failed, project_id=task.project_id, task_id=task.id,
                    payload={"reason": "merge_error", "target": target_branch},
                )
                await wm.cleanup(task.id, None)
                return False
        else:
            merged = False

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

    async def _maybe_post_mortem(
        self,
        runner: AgentRunner,
        task: Task,
        worktree: Path,
        knowledge: str,
        rules: str,
        config: ConclaveConfig,
        feedback: str,
        attempts: int,
        *,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        """Dispatch the ``postmortem`` persona on a terminal failure (best-effort).

        Gated by ``experimental.post_mortem_enabled``. Runs BEFORE the worktree is cleaned
        so the agent can inspect the failed work, and feeds it the last reviewer/gate
        feedback plus the recorded verdicts so it can reason about why the task failed.
        The analysis is recorded as a ``postmortem_draft`` event.

        Wholly best-effort: ANY failure (provider error, cancelled dispatch, DB hiccup) is
        swallowed and logged — a post-mortem must never crash the worker or change the
        task's already-decided failure outcome.
        """
        if not config.experimental.post_mortem_enabled:
            return
        try:
            verdicts = await repo.list_verdicts(self._db, task.id)
            verdict_lines = "\n".join(
                f"- attempt {v.attempt} · {v.agent}: {v.verdict}"
                + (f" — {v.reason}" if v.reason else "")
                for v in verdicts
            )
            prompt = (
                "This task FAILED after exhausting its retries. Analyze why and produce a "
                "rewritten task specification more likely to succeed, per your system prompt.\n\n"
                f"ORIGINAL TASK:\n{task.request}\n\n"
                f"ATTEMPTS MADE: {attempts}\n"
            )
            if verdict_lines:
                prompt += f"\nREVIEWER VERDICTS ACROSS ATTEMPTS:\n{verdict_lines}\n"
            if feedback:
                prompt += f"\nLAST FAILURE FEEDBACK:\n{feedback}\n"

            result = await runner.run(
                agent="postmortem",
                prompt=prompt,
                task_id=task.id,
                worktree=worktree,
                repo_knowledge=knowledge,
                project_rules=rules,
                cancel_event=cancel_event,
            )
            if not result.ok or not result.text.strip():
                logger.info("post-mortem produced no usable output for task %s", task.id)
                return
            await self._bus.emit(
                type=EventType.postmortem_draft,
                project_id=task.project_id,
                task_id=task.id,
                agent="postmortem",
                payload={"analysis": result.text, "attempts": attempts},
            )
        except Exception:
            logger.warning("post-mortem failed for task %s", task.id, exc_info=True)

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

    async def _finish_cancelled(
        self, task: Task, wm: WorktreeManager, task_branch: str
    ) -> bool:
        """Transition *task* to ``cancelled``, clean its worktree, emit the event.

        Returns ``False`` so the worker loop continues to the next task. The
        cancellation-event entry in ``_cancel_events`` is cleaned here; the
        ``finally`` block in :meth:`process_task` is a belt-and-suspenders no-op
        when this already ran.
        """
        await repo.finalize_task(
            self._db, task.id, state=TaskState.cancelled,
            result_summary="cancelled by operator",
        )
        await self._bus.emit(
            type=EventType.task_cancelled, project_id=task.project_id, task_id=task.id,
        )
        # Worktree cleanup is tolerant of missing paths (git worktree remove --force
        # handles absent/locked worktrees).
        await wm.cleanup(task.id, task_branch)
        self._cancel_events.pop(task.id, None)
        return False

    async def _merge(
        self, repo_path: Path, target_branch: str, task_branch: str, task_id: str
    ) -> MergeResult:
        """Merge ``task_branch`` into ``target_branch`` without disturbing the user's
        checkout. Serialises merges into the same target via an asyncio lock so two
        concurrent tasks can never race on the shared merge-worktree path or clobber
        each other's ref update.

        Returns a three-way :class:`MergeResult`:
        * ``success`` — the target now contains the task work.
        * ``conflict`` — a real merge conflict (caller must fail the task, preserve
          the branch).
        * ``error`` — transient infra failure (e.g. concurrent ref advance); retryable.

        The per-target lock is reference-counted so ``_merge_locks`` stays bounded: a
        long-running daemon merging into many ephemeral targets would otherwise accrue one
        permanent lock per branch ever seen. The counter is bumped before acquiring and
        dropped after releasing; the entry is evicted once no merge still references it.
        All counter/dict mutations below run synchronously (no ``await`` between them), so
        in the single-threaded event loop a concurrent merge into the same target always
        observes the same live lock — the serialization guarantee is preserved.
        """
        lock = self._merge_locks.setdefault(target_branch, asyncio.Lock())
        self._merge_lock_refs[target_branch] = self._merge_lock_refs.get(target_branch, 0) + 1
        try:
            async with lock:
                return await self._merge_locked(repo_path, target_branch, task_branch, task_id)
        finally:
            refs = self._merge_lock_refs[target_branch] - 1
            if refs <= 0:
                # Last referer out — drop both entries so the dicts can't grow unbounded.
                self._merge_lock_refs.pop(target_branch, None)
                self._merge_locks.pop(target_branch, None)
            else:
                self._merge_lock_refs[target_branch] = refs

    async def _merge_locked(
        self, repo_path: Path, target_branch: str, task_branch: str, task_id: str
    ) -> MergeResult:
        """Body of :meth:`_merge` — runs while holding the per-target-branch lock."""
        ancestor_code, _ = await run_git(
            repo_path, "merge-base", "--is-ancestor", target_branch, task_branch
        )
        is_ff = ancestor_code == 0
        _, current = await run_git(repo_path, "rev-parse", "--abbrev-ref", "HEAD")
        current_branch = current.strip()
        _, task_sha = await run_git(repo_path, "rev-parse", task_branch)
        task_sha = task_sha.strip()

        if is_ff and current_branch != target_branch:
            # Fast-forward via GUARDED update-ref so a concurrent advance is
            # detected rather than silently clobbered. Retry once with the new
            # HEAD when the old-value doesn't match (target advanced while we
            # were computing task_sha).
            _, old_sha = await run_git(repo_path, "rev-parse", target_branch)
            old_sha = old_sha.strip()
            code, _ = await run_git(
                repo_path, "update-ref", f"refs/heads/{target_branch}", task_sha, old_sha,
            )
            if code != 0:
                _, new_old = await run_git(repo_path, "rev-parse", target_branch)
                new_old = new_old.strip()
                if new_old != old_sha:
                    code, _ = await run_git(
                        repo_path, "update-ref", f"refs/heads/{target_branch}",
                        task_sha, new_old,
                    )
            if code == 0:
                return MergeResult.success
            return MergeResult.error

        if current_branch == target_branch:
            code, _ = await run_git(repo_path, "merge", "--ff-only", task_branch)
            if code != 0:
                code, _ = await run_git(
                    repo_path, "merge", "--no-ff", "-m",
                    f"merge(conclave): {task_branch}", task_branch,
                )
                if code != 0:
                    # Abort the failed merge to leave the repo clean.
                    await run_git(repo_path, "merge", "--abort")
                    return MergeResult.conflict
            return MergeResult.success

        # target is not checked out and history diverged: merge via a temp worktree.
        # Include task_id so concurrent merges into the same target never collide.
        merge_path = wm_merge_path(repo_path, target_branch, task_id)
        await run_git(repo_path, "worktree", "add", str(merge_path), target_branch)
        try:
            code, _ = await run_git(
                merge_path, "merge", "--no-ff", "-m",
                f"merge(conclave): {task_branch}", task_branch,
            )
            if code != 0:
                return MergeResult.conflict
        finally:
            await run_git(repo_path, "worktree", "remove", "--force", str(merge_path))
            await run_git(repo_path, "worktree", "prune")
        return MergeResult.success


# --- module helpers ---------------------------------------------------------


def wm_merge_path(repo_path: Path, target_branch: str, task_id: str) -> Path:
    """Deterministic merge-worktree path unique per (repo, target, task).

    Including ``task_id`` guarantees two concurrent merges into the same target
    branch never share a worktree directory.
    """
    safe = target_branch.replace("/", "_")
    safe_task = task_id.replace("/", "_")
    return repo_path.parent / f".conclave-merge-{repo_path.name}-{safe}-{safe_task}"


def _commit_message(task: Task) -> str:
    first_line = next((ln.strip() for ln in task.request.splitlines() if ln.strip()), task.id)
    subject = task.title or first_line
    return f"feat(conclave): {subject[:72]}\n\n{task.request}"


def _check_budget(started: float, budget_minutes: float) -> bool:
    """Return ``True`` when the wall-clock budget has been exceeded.

    Called before every agent dispatch within an attempt (developer, each reviewer, gate)
    so a single long-running agent call can't blow past the configured cap — the task is
    failed as timed-out as soon as the cap is reached.
    """
    if budget_minutes <= 0:
        return False
    return (time.monotonic() - started) / 60.0 >= budget_minutes


def _truncate_diff(diff: str) -> str:
    """Truncate an oversized diff with a clear marker so it doesn't overflow context."""
    if len(diff) <= _MAX_DIFF_CHARS:
        return diff
    return diff[:_MAX_DIFF_CHARS] + f"\n... [diff truncated — original was {len(diff)} chars]"


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


def _build_venv_guidance(
    worktree: Path, config: ConclaveConfig, knowledge: dict[str, Any] | None,
) -> str:
    """Build tool/venv guidance lines derived from the project's actual configured commands.

    Only injects guidance when ``.venv/`` actually exists in the worktree — i.e. the
    ``setup_command`` ran and provisioned it.  Derives the test command via
    :func:`_test_command` and also picks up ``lint`` / ``check`` commands from repo
    knowledge.  Produces ``.venv/bin/``-prefixed lines for each command so the guidance
    reflects the real toolchain instead of hard-coded pytest/mypy/ruff.  When no commands
    are known, emits a generic ``.venv/bin/`` hint.
    """
    venv_path = worktree / ".venv"
    if not venv_path.is_dir():
        return ""

    commands: list[str] = []

    # Test command — derived from config or repo knowledge.
    test_cmd = _test_command(config, knowledge)
    if test_cmd:
        commands.append(test_cmd.strip())

    # Lint / check commands from repo knowledge (forward-compatible: keys may not exist).
    if knowledge:
        cmds = knowledge.get("commands")
        if isinstance(cmds, dict):
            for key in ("lint", "check"):
                val = cmds.get(key)
                if isinstance(val, str) and val.strip():
                    commands.append(val.strip())

    if not commands:
        return (
            "\n\n## Worktree environment (MANDATORY)\n"
            "A provisioned virtualenv exists at `.venv/` in the worktree root with all "
            "dependencies installed.  Run tooling through `.venv/bin/` so your checks "
            "match the green-gate exactly.  Do NOT use system-wide tools — they are a "
            "different toolchain and will disagree with the gate."
        )

    lines = [f"- `.venv/bin/{cmd}`" for cmd in commands]
    unique_tools = sorted({cmd.split()[0] for cmd in commands})
    tool_refs = "/".join(f"`{t}`" for t in unique_tools)

    return (
        "\n\n## Worktree environment (MANDATORY)\n"
        "A provisioned virtualenv exists at `.venv/` in the worktree root with all "
        "dependencies installed. Run EVERY verification through it so your checks "
        "match the green-gate exactly:\n"
        + "\n".join(lines) + "\n"
        f"Do NOT use system-wide {tool_refs} — they are a different "
        "toolchain and will disagree with the gate."
    )
