"""The orchestration loop (port of team-ai's ``process_task``).

Per task: create an isolated worktree from the target branch → snapshot baseline
failures → optional planner → retry loop (developer → diff-derived reviewers with
grounded verdicts → green-gate, feeding failures back with cross-attempt memory) →
on success commit + merge into the target branch; on failure clean up. All state is
in SQLite and every step emits an event.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shutil
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..config import ConclaveConfig, WorkflowNode, load_project_config, resolve_agent
from ..db import CheckpointStatus, Database, Project, Task, TaskState
from ..db import repositories as repo
from ..events import EventBus, EventType, build_notification_sink
from ..providers import AgentResult, Provider
from .baseline import build_baseline_preamble
from .context_limits import BulkBlock, BulkLimit, fit_block, resolve_bulk_limit
from .gate import GateResult, apply_quarantine, run_tests
from .gitio import run_git, run_shell
from .graph_executor import (
    derive_planner_nodes,
    derive_post_gate_nodes,
    derive_review_nodes,
    is_parallel_review,
    is_pass_through_review,
    resolve_workflow,
)
from .memory import AttemptMemory
from .runner import AgentRunner
from .test_integrity import (
    IntegrityFinding,
    collect_integrity_findings,
    integrity_prompt_section,
)
from .verdict import ParsedVerdict, check_grounding, parse_verdict
from .workflow_evolver import evolve_project_workflow
from .worktree import WorktreeManager

# opencode reads AGENTS.md natively, so it leads the project-rule fallback list. CLAUDE.md
# and CONCLAVE.md remain supported for repos that still carry them.
_PROJECT_RULE_FILES = ("AGENTS.md", "CONCLAVE.md", "CLAUDE.md")

# A single reviewer dispatch can transiently return empty/non-ok (provider hiccup).
# Retry it a few times before giving up, so one flaky call doesn't discard the
# developer's work and re-run the whole develop→review loop.
_REVIEWER_DISPATCH_RETRIES = 2
_REVIEWER_RETRY_BACKOFF_S = 3.0

# Bound on git's combined output captured in the result summary when a task commit fails,
# so a noisy hook cannot bloat the task row while its diagnostic tail stays visible.
_COMMIT_OUTPUT_TAIL_CHARS = 500

# Result summary of a task stopped because no test command resolves: a green gate that runs no
# tests proves nothing, so the operator must configure one or opt out explicitly.
_NO_TEST_COMMAND_SUMMARY = (
    "no test command: the green gate would run no tests, so the task stopped before any agent "
    "ran. Set execution.baseline_test_command (and execution.setup_command if the tests need an "
    "environment), or set execution.allow_no_test_command: true to run deliberately without a "
    "gate."
)

# Test-integrity findings listed in a result summary; the test_integrity.flagged event has all.
_INTEGRITY_NOTE_MAX_FINDINGS = 20

_NECESSITY_YAGNI_RULE = (
    "NECESSITY & YAGNI CHECK: Verify that every change is strictly necessary to fix a "
    "real bug, close a parity gap, or satisfy an explicit requirement before adding files "
    "or abstractions. Reject redundant or speculative code."
)

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


@dataclass
class _Baseline:
    """Pre-existing test failures on the target branch, kept whole until fitted for a reader."""

    target_branch: str
    command: str | None = None
    failures: str = ""
    # One fitted copy per limit, so each actual cut is made and reported once per task.
    fitted: dict[BulkLimit, str] = field(default_factory=dict)


@dataclass
class _GateNote:
    """A gate command that failed in a pass-through flow, kept whole until fitted for a reader."""

    label: str
    command: str
    gate: GateResult
    # One fitted output per limit, so each actual cut is made and reported once.
    fitted: dict[BulkLimit, str] = field(default_factory=dict)


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
        # Per-task pending operator steer — injected by the daemon through
        # :meth:`request_steer` and consumed by :meth:`process_task` before the
        # NEXT developer dispatch, then cleared. Mirrors ``_cancel_events`` in
        # shape: a task is steerable exactly while it is being processed (i.e. it
        # has a live cancellation event), so guidance lands on its next attempt.
        self._pending_steers: dict[str, str] = {}
        # Per-node operator steers — keyed by (task_id, node_id).
        self._node_steers: dict[tuple[str, str], str] = {}
        # Active Human-in-the-Loop waiters — keyed by task_id -> {node_or_edge_id: Event}.
        self._approval_waiters: dict[str, dict[str, asyncio.Event]] = {}
        # Operator resolutions/overrides — keyed by (task_id, node_or_edge_id).
        self._approval_resolutions: dict[tuple[str, str], dict[str, Any]] = {}
        # Dynamically armed HITL nodes/edges — keyed by task_id -> set of node/edge ids.
        self._dynamic_hitl: dict[str, set[str]] = {}
        self._repo_path: Path | None = None
        self._reloader: Any = None
        self._daemon: Any = None

    async def _refresh_task_config(
        self, project_id: str, runner: AgentRunner, current_config: ConclaveConfig
    ) -> ConclaveConfig:
        """Re-fetch ``project.config`` from SQLite and hot-plug it into *runner*."""
        project = await repo.get_project(self._db, project_id)
        if project is None:
            runner._config = current_config
            return current_config
        config = load_project_config(project.config)
        runner._config = config
        return config

    async def _fit_bulk(
        self,
        task: Task,
        config: ConclaveConfig,
        text: str,
        *,
        block: BulkBlock,
        how_to_get_rest: str,
        agent: str | None,
        node: WorkflowNode | None = None,
        cache: dict[BulkLimit, str] | None = None,
    ) -> str:
        """Fit one recoverable bulk *block* to the limit resolved for *agent*'s dispatch.

        *node* is the workflow node the agent runs as, if any. Emits one ``context_truncated``
        log event per actual cut. Readers sharing *cache* (e.g. every reviewer of one attempt)
        reuse a cut, and its single event, whenever they resolve to the same limit.
        """
        limit = resolve_bulk_limit(
            config, agent, node.compact_before_start if node is not None else None
        )
        if cache is not None and limit in cache:
            return cache[limit]
        fitted = fit_block(
            text, limit=limit.limit, field=limit.field, how_to_get_rest=how_to_get_rest
        )
        if fitted.truncated:
            await self._bus.emit(
                type=EventType.log,
                project_id=task.project_id,
                task_id=task.id,
                agent=agent,
                payload={
                    "stage": "context_truncated",
                    "field": limit.field,
                    "block": block,
                    "original_chars": fitted.original_chars,
                    "kept_chars": fitted.kept_chars,
                    "agent": agent,
                    "node_id": node.id if node is not None else agent,
                    # The Live tab renders log rows as "[stage] message".
                    "message": (
                        f"{block} for {agent or 'the task'}: kept {fitted.kept_chars} of "
                        f"{fitted.original_chars} chars (context.{limit.field} = {limit.limit})"
                    ),
                },
            )
        if cache is not None:
            cache[limit] = fitted.text
        return fitted.text

    async def _baseline_preamble(
        self,
        task: Task,
        config: ConclaveConfig,
        baseline: _Baseline,
        *,
        agent: str,
        node: WorkflowNode | None,
    ) -> str:
        """The pre-existing-failures preamble, with the baseline output fitted for *agent*."""
        if not baseline.failures:
            return ""
        failures = await self._fit_bulk(
            task, config, baseline.failures,
            block="gate_output",
            how_to_get_rest=(
                f"Re-run `{baseline.command}` in your working directory before changing "
                "anything for the full output."
            ),
            agent=agent,
            node=node,
            cache=baseline.fitted,
        )
        return build_baseline_preamble(baseline.target_branch, failures)

    async def _with_gate_notes(
        self,
        task: Task,
        config: ConclaveConfig,
        summary: str,
        notes: list[_GateNote],
        reader: WorkflowNode | None,
    ) -> str:
        """*summary* followed by each pass-through gate failure, fitted for *reader*."""
        parts = [summary] if summary else []
        for note in notes:
            output = await self._fit_bulk(
                task, config, note.gate.output,
                block="gate_output",
                how_to_get_rest=_rerun_hint(note.command),
                agent=(reader.agent or reader.id) if reader is not None else None,
                node=reader,
                cache=note.fitted,
            )
            parts.append(f"{note.label.upper()} GATE (exit {note.gate.exit_code}):\n{output}")
        return "\n\n".join(parts)

    def _build_venv_guidance(
        self,
        worktree: Path,
        config: ConclaveConfig,
        knowledge: dict[str, Any] | None = None,
        repo_path: Path | None = None,
    ) -> str:
        return _build_venv_guidance(
            worktree,
            config,
            knowledge,
            repo_path=repo_path if repo_path is not None else self._repo_path,
        )

    @staticmethod
    def _build_developer_prompt(
        task_or_request: Task | str,
        plan_preamble: str = "",
        baseline_preamble: str = "",
        test_command: str | None = None,
        check_commands: list[str] | None = None,
        *,
        use_memory: bool = False,
        memory: AttemptMemory | None = None,
        feedback: str = "",
        previous_diff: str = "",
    ) -> str:
        return _build_developer_prompt(
            task_or_request,
            plan_preamble,
            baseline_preamble,
            test_command,
            check_commands,
            use_memory=use_memory,
            memory=memory,
            feedback=feedback,
            previous_diff=previous_diff,
        )

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
        for waiter in self._approval_waiters.get(task_id, {}).values():
            waiter.set()
        return True

    def request_steer(
        self,
        task_id: str,
        message: str,
        *,
        node_id: str | None = None,
        edge_id: str | None = None,
    ) -> bool:
        """Queue operator *message* for injection into *task_id*'s next dispatch.

        When *node_id* or *edge_id* is provided, scopes the guidance to that specific
        workflow node/connector (and also queues on developer if node_id == 'developer').
        """
        target = (node_id or edge_id or "").strip()
        if target:
            key = (task_id, target)
            existing_node = self._node_steers.get(key)
            self._node_steers[key] = (
                f"{existing_node}\n{message}" if existing_node else message
            )
            if target == "developer" and task_id in self._cancel_events:
                existing_dev = self._pending_steers.get(task_id)
                self._pending_steers[task_id] = (
                    f"{existing_dev}\n{message}" if existing_dev else message
                )
            return True
        if task_id not in self._cancel_events:
            return False
        existing = self._pending_steers.get(task_id)
        self._pending_steers[task_id] = (
            f"{existing}\n{message}" if existing else message
        )
        return True

    def _consume_steer(self, task_id: str) -> str | None:
        """Pop any pending operator steer for *task_id* (None when none is queued)."""
        return self._pending_steers.pop(task_id, None)

    def _consume_node_steer(self, task_id: str, node_id: str) -> str | None:
        """Pop any pending per-node operator steer for *(task_id, node_id)*."""
        return self._node_steers.pop((task_id, node_id), None)

    def set_hitl_checkpoint(self, task_id: str, target_id: str, enabled: bool) -> list[str]:
        """Dynamically arm or disarm a HITL pause on *target_id* (node_id or edge_id)."""
        armed = self._dynamic_hitl.setdefault(task_id, set())
        if enabled:
            armed.add(target_id)
        else:
            armed.discard(target_id)
            waiters = self._approval_waiters.get(task_id, {})
            if target_id in waiters:
                waiters[target_id].set()
        return sorted(armed)

    def get_hitl_state(self, task_id: str) -> dict[str, list[str]]:
        """Return currently armed HITL ids and actively waiting node/edge ids for *task_id*."""
        return {
            "armed": sorted(self._dynamic_hitl.get(task_id, set())),
            "waiting": sorted(self._approval_waiters.get(task_id, {}).keys()),
        }

    def resolve_node(
        self,
        task_id: str,
        node_id: str,
        *,
        status: str = "completed",
        message: str | None = None,
        verdict: str | None = None,
        output: str | None = None,
        edge_id: str | None = None,
    ) -> bool:
        """Approve/complete or override a node or HITL connector on *task_id*."""
        res: dict[str, Any] = {
            "status": status,
            "message": message,
            "verdict": verdict,
            "output": output,
            "edge_id": edge_id,
        }
        self._approval_resolutions[(task_id, node_id)] = res
        if edge_id:
            self._approval_resolutions[(task_id, edge_id)] = res
        if message and message.strip():
            target_for_steer = None if node_id in ("*", "all", "") else node_id
            if target_for_steer:
                self.request_steer(task_id, message.strip(), node_id=target_for_steer)
            elif task_id in self._cancel_events:
                self.request_steer(task_id, message.strip())
        waiters = self._approval_waiters.get(task_id, {})
        signaled = False
        if node_id in ("*", "all", ""):
            for k, ev in list(waiters.items()):
                self._approval_resolutions[(task_id, k)] = res
                ev.set()
                signaled = True
        else:
            for wait_key in (node_id, edge_id):
                if wait_key and wait_key in waiters:
                    waiters[wait_key].set()
                    signaled = True
        return signaled

    def _should_pause_after_node(
        self,
        task_id: str,
        node_id: str,
        config: ConclaveConfig,
        target_node_id: str | None = None,
    ) -> tuple[bool, str | None]:
        """Check if *node_id* (or its outgoing edge to *target_node_id*) requires human pause."""
        armed = self._dynamic_hitl.get(task_id, set())
        if node_id in armed:
            return True, None
        wf = resolve_workflow(config)
        for n in wf.nodes:
            if n.id == node_id and (n.pause_after or n.await_human_approval):
                return True, None
        matching_edges = [
            e
            for e in wf.edges
            if e.source == node_id and (target_node_id is None or e.target == target_node_id)
        ]
        if not matching_edges and target_node_id is not None:
            matching_edges = [
                e
                for e in wf.edges
                if e.source == node_id and e.condition.value != "on_fail"
            ]
        for e in matching_edges:
            if e.require_human or e.id in armed:
                return True, e.id
        return False, None

    async def _await_human_approval(
        self,
        task: Task,
        node_id: str,
        attempt: int,
        config: ConclaveConfig,
        *,
        cancel_event: asyncio.Event | None = None,
        git_tree_sha: str | None = None,
        artifact: dict[str, Any] | None = None,
        target_node_id: str | None = None,
    ) -> dict[str, Any] | None:
        """If *node_id* or its outgoing connector requires human approval, pause and wait."""
        should_pause, edge_id = self._should_pause_after_node(
            task.id, node_id, config, target_node_id
        )
        if not should_pause:
            return self._approval_resolutions.pop((task.id, node_id), None)

        wait_artifact = dict(artifact or {})
        wait_artifact["waiting_for_approval"] = True
        if edge_id:
            wait_artifact["edge_id"] = edge_id
        if target_node_id:
            wait_artifact["target_node_id"] = target_node_id

        await repo.save_task_checkpoint(
            self._db,
            task_id=task.id,
            node_id=node_id,
            attempt=attempt,
            status=CheckpointStatus.waiting_for_approval,
            git_tree_sha=git_tree_sha,
            artifact=wait_artifact,
        )
        await repo.set_task_state(self._db, task.id, TaskState.waiting_for_approval)
        await self._bus.emit(
            type=EventType.node_waiting,
            project_id=task.project_id,
            task_id=task.id,
            payload={
                "node_id": node_id,
                "edge_id": edge_id,
                "target_node_id": target_node_id,
                "attempt": attempt,
                "status": "waiting_for_approval",
            },
        )
        await self._bus.emit(
            type=EventType.hitl_waiting,
            project_id=task.project_id,
            task_id=task.id,
            payload={
                "node_id": node_id,
                "edge_id": edge_id,
                "target_node_id": target_node_id,
                "attempt": attempt,
            },
        )

        wait_ev = asyncio.Event()
        task_waiters = self._approval_waiters.setdefault(task.id, {})
        task_waiters[node_id] = wait_ev
        if edge_id:
            task_waiters[edge_id] = wait_ev

        try:
            while not wait_ev.is_set():
                if cancel_event is not None and cancel_event.is_set():
                    break
                # Also allow external DB state transition (e.g. approve_task flipping state
                # from waiting_for_approval -> approved) to release the pause.
                cur_task = await repo.get_task(self._db, task.id)
                if cur_task is not None and cur_task.state in (
                    TaskState.approved,
                    TaskState.in_progress,
                ):
                    break
                if cur_task is not None and cur_task.state == TaskState.cancelled:
                    if cancel_event is not None:
                        cancel_event.set()
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wait_ev.wait(), timeout=0.25)
        finally:
            task_waiters.pop(node_id, None)
            if edge_id:
                task_waiters.pop(edge_id, None)

        if cancel_event is not None and cancel_event.is_set():
            return {"status": "cancelled"}

        await repo.set_task_state(self._db, task.id, TaskState.in_progress)
        resolution = (
            self._approval_resolutions.pop((task.id, node_id), None)
            or (self._approval_resolutions.pop((task.id, edge_id), None) if edge_id else None)
            or {"status": "completed"}
        )
        raw_status = str(resolution.get("status") or "completed")
        cp_status = (
            CheckpointStatus.failed
            if raw_status == "failed"
            else CheckpointStatus.skipped
            if raw_status == "skipped"
            else CheckpointStatus.completed
        )
        resolved_artifact = dict(artifact or {})
        if resolution.get("message"):
            resolved_artifact["operator_message"] = resolution["message"]
        if resolution.get("output"):
            resolved_artifact["operator_output"] = resolution["output"]
        if resolution.get("verdict"):
            resolved_artifact["verdict"] = resolution["verdict"]

        await repo.save_task_checkpoint(
            self._db,
            task_id=task.id,
            node_id=node_id,
            attempt=attempt,
            status=cp_status,
            git_tree_sha=git_tree_sha,
            artifact=resolved_artifact,
        )
        await self._bus.emit(
            type=EventType.hitl_resolved,
            project_id=task.project_id,
            task_id=task.id,
            payload={
                "node_id": node_id,
                "edge_id": edge_id,
                "attempt": attempt,
                "status": cp_status.value,
                "message": resolution.get("message"),
            },
        )
        await self._bus.emit(
            type=EventType.node_completed,
            project_id=task.project_id,
            task_id=task.id,
            payload={
                "node_id": node_id,
                "workflow_node_id": node_id,
                "attempt": attempt,
                "status": cp_status.value,
            },
        )
        return resolution

    async def process_task(self, task: Task, *, cancel_event: asyncio.Event | None = None) -> bool:
        project = await repo.get_project(self._db, task.project_id)
        if project is None:
            return False
        config = load_project_config(project.config)
        repo_path = Path(project.path)
        self._repo_path = repo_path
        target_branch = _resolve_target_branch(project, config)
        runner = AgentRunner(self._db, self._bus, self._provider, project.id, config)
        wm = WorktreeManager(repo_path, self._home / "projects" / project.id / "worktrees")
        task_branch = f"{config.execution.branch_prefix}{task.id}"

        checkout_branch = (task.source_branch or "").strip() or target_branch
        await self._bus.emit(
            type=EventType.task_started,
            project_id=project.id,
            task_id=task.id,
            payload={
                "request": task.request[:200],
                "target_branch": target_branch,
                "source_branch": task.source_branch,
            },
        )

        # The test command is known from the config alone, so a task whose green gate would run
        # no tests stops here: no worktree, setup, baseline or agent dispatch is spent. A config
        # error must not evolve the workflow, so this returns before the main try (as a failed
        # worktree setup does) and sends the terminal notification itself.
        if _missing_test_command(config, None):
            await self._fail_early(
                task, wm, task_branch, _NO_TEST_COMMAND_SUMMARY, reason="no_test_command"
            )
            await self._notify_terminal(project.id, task.id, config)
            return False

        try:
            worktree = await wm.create(task.id, checkout_branch, task_branch)
        except Exception as exc:
            # Broad on purpose: wm.create can surface more than WorktreeError (e.g. an
            # OSError spawning git). Anything escaping here used to propagate past the
            # worker's claim, stranding the task in_progress with no worktree and no
            # failure event — the worker then claimed the NEXT task, breaking the
            # one-task-at-a-time invariant. Fail the task properly instead.
            logger.exception("worktree creation failed for task %s", task.id)
            return await self._fail_early(task, wm, task_branch, f"worktree setup failed: {exc!r}")

        # Wrap the entire body after worktree creation so ANY unexpected exception
        # transitions the task to failed, cleans the worktree, and lets the worker
        # continue with the next task — never stranding a task in_progress or leaking
        # a worktree.
        gate_failed_after_review = False
        try:
            _, sha = await run_git(worktree, "rev-parse", "HEAD")
            base_head_sha = sha.strip()
            checkpoint = base_head_sha
            if task.source_branch:
                candidate_refs = (
                    (target_branch,)
                    if target_branch.startswith("origin/")
                    else (f"origin/{target_branch}", target_branch)
                )
                for ref in candidate_refs:
                    mb_rc, mb_out = await run_git(worktree, "merge-base", ref, "HEAD")
                    if mb_rc == 0 and mb_out.strip() and mb_out.strip() != base_head_sha:
                        checkpoint = mb_out.strip()
                        break
                if checkpoint == base_head_sha:
                    for ref in candidate_refs:
                        mb_rc, mb_out = await run_git(worktree, "merge-base", ref, "HEAD")
                        if mb_rc == 0 and mb_out.strip():
                            checkpoint = mb_out.strip()
                            break

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
                    setup_output = await self._fit_bulk(
                        task, config, out,
                        block="setup_output",
                        how_to_get_rest=_rerun_hint(setup_cmd),
                        agent=None,
                    )
                    return await self._fail_early(
                        task, wm, task_branch,
                        f"worktree setup failed (exit {rc}):\n{setup_output}",
                    )

            # Cooperative cancellation — check after setup, before baseline.
            if cancel_event is not None and cancel_event.is_set():
                return await self._finish_cancelled(task, wm, task_branch)

            # Repo context now comes from AGENTS.md (read by opencode natively and surfaced
            # through _read_project_rules), not Conclave's synthesized knowledge preamble.
            knowledge = ""
            rules = _read_project_rules(worktree, repo_path=repo_path)
            rules += self._build_venv_guidance(worktree, config, None)
            test_command = _test_command(config, None)
            # Inject active quarantine exclusions so flaky quarantined tests
            # don't fail the baseline snapshot or the green-gate.
            test_command = await apply_quarantine(self._db, project.id, test_command)
            check_commands = _check_commands(None)
            gate_timeout = resolve_agent(config, "tester").timeout_minutes * 60

            await repo.save_task_checkpoint(
                self._db,
                task_id=task.id,
                node_id="baseline",
                attempt=0,
                status=CheckpointStatus.running,
                git_tree_sha=checkpoint,
                artifact={"target_branch": target_branch},
            )
            await self._bus.emit(
                type=EventType.node_started,
                project_id=project.id,
                task_id=task.id,
                payload={"node_id": "baseline", "attempt": 0},
            )
            baseline = await self._baseline(
                project.id, worktree, checkpoint, target_branch, test_command, gate_timeout,
            )
            await repo.save_task_checkpoint(
                self._db,
                task_id=task.id,
                node_id="baseline",
                attempt=0,
                status=CheckpointStatus.completed,
                git_tree_sha=checkpoint,
                artifact={"target_branch": target_branch},
            )
            await self._bus.emit(
                type=EventType.node_completed,
                project_id=project.id,
                task_id=task.id,
                payload={"node_id": "baseline", "attempt": 0, "status": "completed"},
            )
            await self._await_human_approval(
                task,
                "baseline",
                0,
                config,
                cancel_event=cancel_event,
                git_tree_sha=checkpoint,
                artifact={"target_branch": target_branch},
            )
            if cancel_event is not None and cancel_event.is_set():
                return await self._finish_cancelled(task, wm, task_branch)
            config = await self._refresh_task_config(project.id, runner, config)
            plan_preamble = await self._maybe_plan(
                runner, task, worktree, knowledge, rules, baseline, config,
                cancel_event=cancel_event, checkpoint=checkpoint,
            )

            # Cooperative cancellation — check after planning, before developer loop.
            if cancel_event is not None and cancel_event.is_set():
                return await self._finish_cancelled(task, wm, task_branch)

            memory = AttemptMemory(config.experimental.cross_attempt_memory_entries)
            use_memory = config.experimental.cross_attempt_memory
            dev_node_cfg = _find_workflow_node(config, "developer", "developer")
            if dev_node_cfg is None and config.workflow is not None:
                dev_node_cfg = next(
                    (
                        n
                        for n in config.workflow.nodes
                        if n.enabled and n.kind.value == "developer"
                    ),
                    None,
                )
            has_enabled_developer = config.workflow is None or dev_node_cfg is not None
            max_retries = (
                resolve_agent(config, "developer").max_retries if has_enabled_developer else 1
            )
            budget = config.execution.wall_clock_budget_minutes
            started = time.monotonic()

            existing_cps = await repo.list_task_checkpoints(self._db, task.id)
            has_review_or_gate = {
                cp.attempt
                for cp in existing_cps
                if cp.node_id in ("review", "gate")
            }
            resumed_tree: str | None = None
            resumed_attempt: int | None = None
            for cp in reversed(existing_cps):
                if (
                    cp.node_id == "developer"
                    and cp.status == CheckpointStatus.completed
                    and cp.git_tree_sha
                    and cp.attempt not in has_review_or_gate
                ):
                    cat_rc, _ = await run_git(
                        worktree, "cat-file", "-e", f"{cp.git_tree_sha}^{{tree}}"
                    )
                    if cat_rc == 0:
                        resumed_tree = cp.git_tree_sha
                        resumed_attempt = cp.attempt
                        break

            feedback = ""
            previous_diff = ""
            attempts = max(0, resumed_attempt - 1) if resumed_attempt is not None else 0
            failed = False
            timed_out = False
            gate_failed_after_review = False
            # Test-integrity findings of the last checked attempt, and the mode they ran under.
            integrity: list[IntegrityFinding] = []
            integrity_mode = "off"

            while attempts < max_retries:
                config = await self._refresh_task_config(project.id, runner, config)
                use_memory = config.experimental.cross_attempt_memory
                test_command = _test_command(config, None)
                test_command = await apply_quarantine(self._db, project.id, test_command)
                gate_timeout = resolve_agent(config, "tester").timeout_minutes * 60
                dev_node_cfg = _find_workflow_node(config, "developer", "developer")
                if dev_node_cfg is None and config.workflow is not None:
                    dev_node_cfg = next(
                        (
                            n
                            for n in config.workflow.nodes
                            if n.enabled and n.kind.value == "developer"
                        ),
                        None,
                    )
                has_enabled_developer = config.workflow is None or dev_node_cfg is not None
                max_retries = (
                    resolve_agent(config, "developer").max_retries if has_enabled_developer else 1
                )
                budget = config.execution.wall_clock_budget_minutes
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

                if resumed_tree is not None:
                    saved_tree = resumed_tree
                    resumed_tree = None
                    await run_git(worktree, "read-tree", saved_tree)
                    await run_git(worktree, "checkout-index", "-a", "-f")
                    _, raw_diff = await run_git(worktree, "diff", "--cached", checkpoint)
                    full_diff = raw_diff
                    diff_cmd = f"git diff --cached {checkpoint}"
                    review_tree = saved_tree
                    await repo.end_attempt(
                        self._db, attempt_id, diff_stat=_diff_stat(full_diff)
                    )
                elif (
                    not has_enabled_developer
                    or (task.source_branch and attempts == 1)
                ):
                    await _stage_worktree_excluding_caches(worktree)
                    _, raw_diff = await run_git(worktree, "diff", "--cached", checkpoint)
                    diff_cmd = f"git diff --cached {checkpoint}"
                    if not raw_diff.strip():
                        _, raw_diff = await run_git(worktree, "diff", checkpoint, "HEAD")
                        diff_cmd = f"git diff {checkpoint} HEAD"
                    if not raw_diff.strip() and task.source_branch:
                        candidate_refs = (
                            (target_branch,)
                            if target_branch.startswith("origin/")
                            else (f"origin/{target_branch}", target_branch)
                        )
                        for ref in candidate_refs:
                            rc_d, d_out = await run_git(worktree, "diff", f"{ref}..HEAD")
                            if rc_d == 0 and d_out.strip():
                                raw_diff = d_out
                                diff_cmd = f"git diff {ref}..HEAD"
                                break
                    full_diff = raw_diff
                    _, review_tree = await run_git(worktree, "write-tree")
                    review_tree = review_tree.strip()
                    await repo.end_attempt(
                        self._db, attempt_id, diff_stat=_diff_stat(full_diff)
                    )
                    if has_enabled_developer:
                        await repo.save_task_checkpoint(
                            self._db,
                            task_id=task.id,
                            node_id="developer",
                            attempt=attempts,
                            status=CheckpointStatus.skipped,
                            git_tree_sha=review_tree,
                            artifact={
                                "diff_stat": _diff_stat(full_diff),
                                "source_branch": task.source_branch,
                            },
                        )
                        await self._bus.emit(
                            type=EventType.node_completed,
                            project_id=project.id,
                            task_id=task.id,
                            payload={
                                "node_id": "developer",
                                "attempt": attempts,
                                "status": "skipped",
                            },
                        )
                else:
                    dev_node = dev_node_cfg
                    dev_node_id = dev_node.id if dev_node else "developer"
                    dev_agent = (dev_node.agent or "developer") if dev_node else "developer"
                    prev_tree = ""
                    if attempts > 1:
                        _, previous_diff = await run_git(worktree, "diff", "--cached", checkpoint)
                        if use_memory:
                            memory.add(attempts - 1, previous_diff, feedback)
                        # The index holds the tree previous_diff came from: record it so the
                        # full previous diff stays recoverable after the reset below.
                        tree_rc, tree_out = await run_git(worktree, "write-tree")
                        prev_tree = tree_out.strip() if tree_rc == 0 else ""
                        await run_git(worktree, "reset", "--hard", base_head_sha)
                        # Preserve the provisioned .venv across attempts; rebuilding it each
                        # retry is slow and pointless.  .gitignore is the belt (the ideal
                        # way to protect .venv from git-clean); `-e .venv/` is the suspenders
                        # — the trailing-slash directory exclusion works even when .gitignore
                        # is absent or doesn't cover the venv.
                        await run_git(worktree, "clean", "-fd", "-e", ".venv/")

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

                    # The prompt shows the previous diff only next to feedback, so it is
                    # fitted only then.
                    prior_diff = ""
                    if feedback and previous_diff:
                        prior_diff = await self._fit_bulk(
                            task, config, previous_diff,
                            block="previous_diff",
                            how_to_get_rest=(
                                f"Run `git diff {checkpoint} {prev_tree}` in your working "
                                "directory for the full previous diff."
                                if prev_tree
                                else "The rest is not available: your previous attempt was "
                                "reset before this one started."
                            ),
                            agent=dev_agent,
                            node=dev_node,
                        )
                    dev_prompt = self._build_developer_prompt(
                        task.request,
                        plan_preamble,
                        await self._baseline_preamble(
                            task, config, baseline, agent=dev_agent, node=dev_node
                        ),
                        test_command,
                        check_commands,
                        use_memory=use_memory,
                        memory=memory,
                        feedback=feedback,
                        previous_diff=prior_diff,
                    )

                    # Operator steer — inject any guidance queued for this task into THIS
                    # developer dispatch, then clear it so it lands exactly once. Best-effort:
                    # absent a steer this is a no-op and never alters processing.
                    self._consume_node_steer(task.id, dev_node_id)
                    steer = self._consume_steer(task.id)
                    if steer:
                        dev_prompt += (
                            "\n\nOPERATOR STEER (live guidance from the operator — "
                            "prioritise this):\n" + steer
                        )
                        await self._bus.emit(
                            type=EventType.operator_steer,
                            project_id=project.id,
                            task_id=task.id,
                            payload={
                                "message": steer,
                                "attempt": attempts,
                                "node_id": dev_node_id,
                            },
                        )

                    if dev_node and dev_node.prompt_template:
                        dev_prompt += f"\n\n{dev_node.prompt_template}"
                    await repo.save_task_checkpoint(
                        self._db,
                        task_id=task.id,
                        node_id=dev_node_id,
                        attempt=attempts,
                        status=CheckpointStatus.running,
                        artifact={"agent": dev_agent},
                    )
                    await self._bus.emit(
                        type=EventType.node_started,
                        project_id=project.id,
                        task_id=task.id,
                        agent=dev_agent,
                        payload={"node_id": dev_node_id, "attempt": attempts},
                    )
                    dev = await runner.run(
                        agent=dev_agent,
                        prompt=dev_prompt,
                        task_id=task.id,
                        worktree=worktree,
                        repo_knowledge=knowledge,
                        project_rules=rules,
                        cancel_event=cancel_event,
                        node_id=dev_node_id,
                        model_override=dev_node.model if dev_node else None,
                        effort_override=dev_node.effort if dev_node else None,
                        cli_agent_override=dev_node.cli_agent if dev_node else None,
                        skills_override=(dev_node.skills or None) if dev_node else None,
                        thinking_override=dev_node.thinking if dev_node else None,
                    )
                    if not dev.ok:
                        feedback = f"Developer agent error: {dev.error}"
                        await repo.end_attempt(self._db, attempt_id)
                        await repo.save_task_checkpoint(
                            self._db,
                            task_id=task.id,
                            node_id=dev_node_id,
                            attempt=attempts,
                            status=CheckpointStatus.failed,
                            artifact={"error": dev.error},
                        )
                        await self._bus.emit(
                            type=EventType.node_completed,
                            project_id=project.id,
                            task_id=task.id,
                            agent=dev_agent,
                            payload={
                                "node_id": dev_node_id,
                                "attempt": attempts,
                                "status": "failed",
                            },
                        )
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
                    await _stage_worktree_excluding_caches(worktree)
                    _, raw_diff = await run_git(worktree, "diff", "--cached", checkpoint)
                    full_diff = raw_diff
                    diff_cmd = f"git diff --cached {checkpoint}"
                    _, review_tree = await run_git(worktree, "write-tree")
                    review_tree = review_tree.strip()
                    await repo.end_attempt(
                        self._db, attempt_id, diff_stat=_diff_stat(full_diff)
                    )
                    await repo.save_task_checkpoint(
                        self._db,
                        task_id=task.id,
                        node_id=dev_node_id,
                        attempt=attempts,
                        status=CheckpointStatus.completed,
                        git_tree_sha=review_tree,
                        artifact={"diff_stat": _diff_stat(full_diff)},
                    )
                    await self._bus.emit(
                        type=EventType.node_completed,
                        project_id=project.id,
                        task_id=task.id,
                        agent=dev_agent,
                        payload={
                            "node_id": dev_node_id,
                            "attempt": attempts,
                            "status": "completed",
                        },
                    )
                    dev_res = await self._await_human_approval(
                        task,
                        dev_node_id,
                        attempts,
                        config,
                        cancel_event=cancel_event,
                        git_tree_sha=review_tree,
                        artifact={"diff_stat": _diff_stat(full_diff)},
                    )
                    if cancel_event is not None and cancel_event.is_set():
                        return await self._finish_cancelled(task, wm, task_branch)
                    if dev_res and dev_res.get("status") == "failed":
                        feedback = str(
                            dev_res.get("message") or "Operator rejected developer changes."
                        )
                        failed = True
                        continue

                # Cooperative cancellation — check after developer, before review.
                if cancel_event is not None and cancel_event.is_set():
                    return await self._finish_cancelled(task, wm, task_branch)

                config = await self._refresh_task_config(project.id, runner, config)
                budget = config.execution.wall_clock_budget_minutes
                # Test integrity: which pre-existing tests this attempt deleted, removed or
                # skipped, read from git (never the fitted prompt diff) before the reviewers run,
                # since they can write to the worktree. The index holds the reviewed tree here.
                integrity_mode = config.execution.test_integrity if has_enabled_developer else "off"
                integrity = (
                    await collect_integrity_findings(worktree, checkpoint)
                    if integrity_mode != "off"
                    else []
                )
                if integrity:
                    await self._bus.emit(
                        type=EventType.test_integrity_flagged,
                        project_id=project.id,
                        task_id=task.id,
                        payload={
                            "findings": [asdict(finding) for finding in integrity],
                            "attempt": attempts,
                            "mode": integrity_mode,
                        },
                    )
                # One fitted diff per limit for this attempt, shared by the reviewers and the
                # post-gate nodes, so each actual cut is made and reported once.
                diff_cache: dict[BulkLimit, str] = {}
                failed, feedback, review_timed_out = await self._review(
                    runner, task, project.id, worktree, full_diff, attempts, config, knowledge,
                    rules, started, budget, cancel_event=cancel_event, diff_cmd=diff_cmd,
                    diff_cache=diff_cache, integrity_section=integrity_prompt_section(integrity),
                )
                if review_timed_out:
                    timed_out = True
                    failed = True
                    break
                if failed:
                    config = await self._refresh_task_config(project.id, runner, config)
                    dev_node_cfg = _find_workflow_node(config, "developer", "developer")
                    if dev_node_cfg is None and config.workflow is not None:
                        dev_node_cfg = next(
                            (
                                n
                                for n in config.workflow.nodes
                                if n.enabled and n.kind.value == "developer"
                            ),
                            None,
                        )
                    has_enabled_developer = config.workflow is None or dev_node_cfg is not None
                    max_retries = (
                        resolve_agent(config, "developer").max_retries
                        if has_enabled_developer
                        else 1
                    )
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

                config = await self._refresh_task_config(project.id, runner, config)
                test_command = _test_command(config, None)
                test_command = await apply_quarantine(self._db, project.id, test_command)
                gate_timeout = resolve_agent(config, "tester").timeout_minutes * 60
                dev_node_cfg = _find_workflow_node(config, "developer", "developer")
                if dev_node_cfg is None and config.workflow is not None:
                    dev_node_cfg = next(
                        (
                            n
                            for n in config.workflow.nodes
                            if n.enabled and n.kind.value == "developer"
                        ),
                        None,
                    )
                has_enabled_developer = config.workflow is None or dev_node_cfg is not None
                max_retries = (
                    resolve_agent(config, "developer").max_retries if has_enabled_developer else 1
                )
                budget = config.execution.wall_clock_budget_minutes
                await repo.save_task_checkpoint(
                    self._db,
                    task_id=task.id,
                    node_id="gate",
                    attempt=attempts,
                    status=CheckpointStatus.running,
                    git_tree_sha=review_tree,
                )
                await self._bus.emit(
                    type=EventType.node_started,
                    project_id=project.id,
                    task_id=task.id,
                    payload={"node_id": "gate", "attempt": attempts},
                )
                pass_through_flow = is_pass_through_review(config) and not has_enabled_developer
                post_gate_nodes = derive_post_gate_nodes(config)
                # Pass-through gate failures, kept whole until fitted for each post-gate reader.
                gate_notes: list[_GateNote] = []
                if config.execution.require_full_green:
                    gate_commands: list[tuple[str, str]] = []
                    if test_command:
                        gate_commands.append(("test", test_command))
                    for cmd in check_commands:
                        gate_commands.append(("check/lint", cmd))

                    if not gate_commands and has_enabled_developer:
                        # Reached with execution.allow_no_test_command (or once the test command
                        # was removed mid-task): the gate passes, but never silently.
                        await self._bus.emit(
                            type=EventType.log,
                            project_id=project.id,
                            task_id=task.id,
                            payload={
                                "stage": "gate_skipped_no_tests",
                                "attempt": attempts,
                                "message": (
                                    "no test command is configured, so the green gate ran "
                                    "no tests"
                                ),
                            },
                        )
                    if gate_commands:
                        # Cooperative cancellation — check before the gate (which may be slow).
                        if cancel_event is not None and cancel_event.is_set():
                            return await self._finish_cancelled(task, wm, task_branch)

                        # HARD cap: abort before running the gate if the wall-clock
                        # budget has been exhausted.
                        if _check_budget(started, budget):
                            timed_out = True
                            failed = True
                            break

                        gate_mark: str | None = None  # {"infra", "real", None}
                        for label, cmd in gate_commands:
                            gate = await run_tests(
                                worktree, cmd, timeout_seconds=gate_timeout
                            )
                            if not gate.passed:
                                if pass_through_flow:
                                    gate_notes.append(_GateNote(label, cmd, gate))
                                    continue
                                # Distinguish infra failures (timeout / missing command)
                                # from real test/check failures. Infra problems get one
                                # retry; if they persist the attempt is aborted — do NOT
                                # feed toolchain noise to the developer as code feedback.
                                if gate.outcome in ("timed_out", "missing_command"):
                                    gate = await run_tests(
                                        worktree, cmd, timeout_seconds=gate_timeout
                                    )
                                    if not gate.passed:
                                        reason = f"infra_{gate.outcome}"
                                        await self._bus.emit(
                                            type=EventType.attempt_failed,
                                            project_id=project.id,
                                            task_id=task.id,
                                            payload={
                                                "n": attempts,
                                                "stage": "gate",
                                                "reason": reason,
                                                "exit_code": gate.exit_code,
                                            },
                                        )
                                        failed = True
                                        gate_mark = "infra"
                                        break
                                else:
                                    gate_output = await self._fit_bulk(
                                        task, config, gate.output,
                                        block="gate_output",
                                        how_to_get_rest=_rerun_hint(cmd),
                                        agent=(
                                            (dev_node_cfg.agent or "developer")
                                            if dev_node_cfg
                                            else "developer"
                                        ),
                                        node=dev_node_cfg,
                                    )
                                    feedback = (
                                        f"{label.upper()} GATE is not green "
                                        f"(exit {gate.exit_code}). "
                                        f"Fix the failing {label}:\n{gate_output}"
                                    )
                                    await self._bus.emit(
                                        type=EventType.attempt_failed,
                                        project_id=project.id,
                                        task_id=task.id,
                                        payload={
                                            "n": attempts,
                                            "stage": "gate",
                                            "exit_code": gate.exit_code,
                                        },
                                    )
                                    failed = True
                                    gate_mark = "real"
                                    break

                        if gate_mark == "infra":
                            await repo.save_task_checkpoint(
                                self._db,
                                task_id=task.id,
                                node_id="gate",
                                attempt=attempts,
                                status=CheckpointStatus.failed,
                                git_tree_sha=review_tree,
                            )
                            await self._bus.emit(
                                type=EventType.node_completed,
                                project_id=project.id,
                                task_id=task.id,
                                payload={
                                    "node_id": "gate",
                                    "attempt": attempts,
                                    "status": "failed",
                                },
                            )
                            break  # infra failure — abort the attempt
                        elif gate_mark == "real":
                            gate_failed_after_review = True
                            await repo.save_task_checkpoint(
                                self._db,
                                task_id=task.id,
                                node_id="gate",
                                attempt=attempts,
                                status=CheckpointStatus.failed,
                                git_tree_sha=review_tree,
                            )
                            await self._bus.emit(
                                type=EventType.node_completed,
                                project_id=project.id,
                                task_id=task.id,
                                payload={
                                    "node_id": "gate",
                                    "attempt": attempts,
                                    "status": "failed",
                                },
                            )
                            continue  # real failure — feed back and retry

                next_after_gate = post_gate_nodes[0].id if post_gate_nodes else "merge"
                review_summary = feedback
                if gate_notes:
                    feedback = await self._with_gate_notes(
                        task, config, review_summary, gate_notes,
                        post_gate_nodes[0] if post_gate_nodes else None,
                    )
                gate_artifact: dict[str, Any] = {}
                if feedback:
                    gate_artifact["review_verdicts_summary"] = feedback
                await repo.save_task_checkpoint(
                    self._db,
                    task_id=task.id,
                    node_id="gate",
                    attempt=attempts,
                    status=CheckpointStatus.completed,
                    git_tree_sha=review_tree,
                    artifact=gate_artifact,
                )
                await self._bus.emit(
                    type=EventType.node_completed,
                    project_id=project.id,
                    task_id=task.id,
                    payload={"node_id": "gate", "attempt": attempts, "status": "completed"},
                )
                gate_res = await self._await_human_approval(
                    task,
                    "gate",
                    attempts,
                    config,
                    cancel_event=cancel_event,
                    git_tree_sha=review_tree,
                    artifact=gate_artifact,
                    target_node_id=next_after_gate,
                )
                if cancel_event is not None and cancel_event.is_set():
                    return await self._finish_cancelled(task, wm, task_branch)
                if gate_res and gate_res.get("status") == "failed":
                    feedback = str(
                        gate_res.get("message") or "Operator rejected green-gate stage."
                    )
                    failed = True
                    continue

                # Execute any post-gate custom nodes (e.g. 'submitter' in acme_pr_review)
                for pg_node in post_gate_nodes:
                    if cancel_event is not None and cancel_event.is_set():
                        return await self._finish_cancelled(task, wm, task_branch)
                    pg_agent = pg_node.agent or pg_node.id
                    pg_diff = await self._fit_bulk(
                        task, config, full_diff,
                        block="diff",
                        how_to_get_rest=_diff_hint(diff_cmd),
                        agent=pg_agent,
                        node=pg_node,
                        cache=diff_cache,
                    )
                    pg_prompt = f"{task.request}\n\nDIFF:\n```diff\n{pg_diff}\n```"
                    pg_feedback = (
                        await self._with_gate_notes(
                            task, config, review_summary, gate_notes, pg_node
                        )
                        if gate_notes
                        else feedback
                    )
                    if pg_feedback:
                        pg_prompt += f"\n\nREVIEW_VERDICTS_SUMMARY:\n{pg_feedback}"
                    if gate_res and (gate_res.get("message") or gate_res.get("output")):
                        op_notes = str(gate_res.get("message") or gate_res.get("output"))
                        pg_prompt += f"\n\nOPERATOR APPROVAL NOTES:\n{op_notes}"
                    if pg_node.prompt_template:
                        pg_prompt += f"\n\n{pg_node.prompt_template}"
                    pg_steer = self._consume_node_steer(task.id, pg_node.id)
                    if pg_steer:
                        pg_prompt += f"\n\nOPERATOR STEER:\n{pg_steer}"
                    await repo.save_task_checkpoint(
                        self._db,
                        task_id=task.id,
                        node_id=pg_node.id,
                        attempt=attempts,
                        status=CheckpointStatus.running,
                        git_tree_sha=review_tree,
                        artifact={"agent": pg_agent},
                    )
                    await self._bus.emit(
                        type=EventType.node_started,
                        project_id=project.id,
                        task_id=task.id,
                        agent=pg_agent,
                        payload={
                            "node_id": pg_node.id,
                            "workflow_node_id": pg_node.id,
                            "agent": pg_agent,
                            "attempt": attempts,
                        },
                    )
                    pg_result = await runner.run(
                        agent=pg_agent,
                        prompt=pg_prompt,
                        task_id=task.id,
                        worktree=worktree,
                        repo_knowledge=knowledge,
                        project_rules=rules,
                        cancel_event=cancel_event,
                        node_id=pg_node.id,
                        model_override=pg_node.model,
                        effort_override=pg_node.effort,
                        cli_agent_override=pg_node.cli_agent,
                        skills_override=pg_node.skills or None,
                        thinking_override=pg_node.thinking,
                    )
                    pg_status = (
                        CheckpointStatus.completed if pg_result.ok else CheckpointStatus.failed
                    )
                    await repo.save_task_checkpoint(
                        self._db,
                        task_id=task.id,
                        node_id=pg_node.id,
                        attempt=attempts,
                        status=pg_status,
                        git_tree_sha=review_tree,
                        artifact={"agent": pg_agent, "output": pg_result.text},
                    )
                    await self._bus.emit(
                        type=EventType.node_completed,
                        project_id=project.id,
                        task_id=task.id,
                        agent=pg_agent,
                        payload={
                            "node_id": pg_node.id,
                            "workflow_node_id": pg_node.id,
                            "agent": pg_agent,
                            "attempt": attempts,
                            "status": pg_status.value,
                        },
                    )
                    await self._await_human_approval(
                        task,
                        pg_node.id,
                        attempts,
                        config,
                        cancel_event=cancel_event,
                        git_tree_sha=review_tree,
                        artifact={"agent": pg_agent, "output": pg_result.text},
                    )
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
                    task, wm, task_branch, timed_out, attempts,
                    integrity=integrity, integrity_mode=integrity_mode,
                )
            return await self._finish_success(
                task, wm, task_branch, repo_path, target_branch, worktree, config, attempts,
                integrity=integrity, integrity_mode=integrity_mode,
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
            # Drop any uninjected steer too (e.g. queued after the final dispatch) so
            # _pending_steers stays bounded — harmless no-op when already consumed.
            self._pending_steers.pop(task.id, None)
            self._approval_waiters.pop(task.id, None)
            self._dynamic_hitl.pop(task.id, None)
            for k in [k for k in self._node_steers if k[0] == task.id]:
                self._node_steers.pop(k, None)
            for k in [k for k in self._approval_resolutions if k[0] == task.id]:
                self._approval_resolutions.pop(k, None)
            with contextlib.suppress(Exception):
                await evolve_project_workflow(
                    self._db,
                    self._bus,
                    project.id,
                    task_id=task.id,
                    gate_failed_after_review=gate_failed_after_review,
                )
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
            if (
                self._reloader is not None
                and self._daemon is not None
                and getattr(self._reloader, "reload_pending", False)
            ):
                with contextlib.suppress(Exception):
                    await self._reloader.on_task_complete(self._daemon)

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
    ) -> _Baseline:
        """Snapshot the pre-existing test failures at *checkpoint* (cached per SHA).

        The whole output is cached and returned; each reader fits it to its own limit when
        the preamble is built (:meth:`_baseline_preamble`).
        """
        if not test_command:
            return _Baseline(target_branch)
        cached = await repo.get_baseline(self._db, project_id, checkpoint)
        if cached is not None:
            return _Baseline(target_branch, test_command, cached.output)
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
        return _Baseline(target_branch, test_command, failures)

    async def _maybe_plan(
        self,
        runner: AgentRunner,
        task: Task,
        worktree: Path,
        knowledge: str,
        rules: str,
        baseline: _Baseline | None,
        config: ConclaveConfig,
        *,
        cancel_event: asyncio.Event | None = None,
        checkpoint: str | None = None,
    ) -> str:
        existing_cp = await repo.get_task_checkpoint(self._db, task.id, "planner", attempt=0)
        if (
            existing_cp is not None
            and existing_cp.status == CheckpointStatus.completed
            and existing_cp.artifact.get("plan_preamble") is not None
        ):
            return str(existing_cp.artifact["plan_preamble"])
        if task.plan is not None:
            plan_json = json.dumps(task.plan)
            plan_preamble = (
                "\n\n=== PLAN (from the Planner — follow its scope strictly) ===\n"
                f"```json\n{plan_json}\n```\n=== END PLAN ===\n"
            )
            await repo.save_task_checkpoint(
                self._db,
                task_id=task.id,
                node_id="planner",
                attempt=0,
                status=CheckpointStatus.completed,
                git_tree_sha=checkpoint,
                artifact={"plan_preamble": plan_preamble},
            )
            return plan_preamble

        planner_nodes = derive_planner_nodes(config)
        if config.workflow is not None and not planner_nodes:
            await repo.save_task_checkpoint(
                self._db,
                task_id=task.id,
                node_id="planner",
                attempt=0,
                status=CheckpointStatus.skipped,
                git_tree_sha=checkpoint,
                artifact={"plan_preamble": ""},
            )
            return ""

        if not config.experimental.planner_enabled or not planner_nodes:
            await repo.save_task_checkpoint(
                self._db,
                task_id=task.id,
                node_id="planner",
                attempt=0,
                status=CheckpointStatus.skipped,
                git_tree_sha=checkpoint,
                artifact={"plan_preamble": ""},
            )
            return ""

        planner_node = planner_nodes[0]
        threshold = config.experimental.auto_planner_char_threshold
        if planner_node.optional:
            should_plan = (
                task.use_planner
                if task.use_planner is not None
                else len(task.request) >= threshold
            )
        else:
            should_plan = task.use_planner if task.use_planner is not None else True

        if not should_plan:
            await repo.save_task_checkpoint(
                self._db,
                task_id=task.id,
                node_id="planner",
                attempt=0,
                status=CheckpointStatus.skipped,
                git_tree_sha=checkpoint,
                artifact={"plan_preamble": ""},
            )
            return ""

        planner_agent = planner_node.agent or "planner"
        baseline_preamble = (
            await self._baseline_preamble(
                task, config, baseline, agent=planner_agent, node=planner_node
            )
            if baseline is not None
            else ""
        )
        prompt = (
            f"{task.request}{baseline_preamble}\n\n"
            "Produce a structured plan per your system prompt.\n\n"
            f"{_NECESSITY_YAGNI_RULE}"
        )
        if planner_node.prompt_template:
            prompt += f"\n\n{planner_node.prompt_template}"
        planner_steer = self._consume_node_steer(task.id, planner_node.id) or (
            self._consume_node_steer(task.id, "planner") if planner_node.id != "planner" else None
        )
        if planner_steer:
            prompt += (
                "\n\nOPERATOR STEER (live guidance from the operator — "
                "prioritise this):\n" + planner_steer
            )
            await self._bus.emit(
                type=EventType.operator_steer,
                project_id=task.project_id,
                task_id=task.id,
                payload={"message": planner_steer, "attempt": 0, "node_id": planner_node.id},
            )
        await repo.save_task_checkpoint(
            self._db,
            task_id=task.id,
            node_id="planner",
            attempt=0,
            status=CheckpointStatus.running,
            git_tree_sha=checkpoint,
            artifact={"agent": planner_agent},
        )
        if planner_node.id != "planner":
            await repo.save_task_checkpoint(
                self._db,
                task_id=task.id,
                node_id=planner_node.id,
                attempt=0,
                status=CheckpointStatus.running,
                git_tree_sha=checkpoint,
                artifact={"agent": planner_agent},
            )
        await self._bus.emit(
            type=EventType.node_started,
            project_id=task.project_id,
            task_id=task.id,
            agent=planner_agent,
            payload={
                "node_id": planner_node.id,
                "workflow_node_id": planner_node.id,
                "agent": planner_agent,
                "attempt": 0,
            },
        )
        result = await runner.run(
            agent=planner_agent,
            prompt=prompt,
            task_id=task.id,
            worktree=worktree,
            repo_knowledge=knowledge,
            project_rules=rules,
            cancel_event=cancel_event,
            node_id=planner_node.id,
            model_override=planner_node.model,
            effort_override=planner_node.effort,
            cli_agent_override=planner_node.cli_agent,
            skills_override=planner_node.skills or None,
            thinking_override=planner_node.thinking,
        )
        plan_preamble = ""
        if result.ok:
            match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", result.text, re.DOTALL)
            if match:
                try:
                    plan = json.loads(match.group(1))
                except (json.JSONDecodeError, ValueError):
                    plan = None
                if plan is not None:
                    await repo.update_task_fields(self._db, task.id, plan=plan)
                    await self._bus.emit(
                        type=EventType.plan_artifact,
                        project_id=task.project_id,
                        task_id=task.id,
                        payload={"plan": plan},
                    )
                    plan_preamble = (
                        "\n\n=== PLAN (from the Planner — follow its scope strictly) ===\n"
                        f"```json\n{match.group(1)}\n```\n=== END PLAN ===\n"
                    )
            if not plan_preamble and config.workflow is not None and result.text.strip():
                plan_preamble = (
                    "\n\n=== PLANNER CONTEXT BRIEF ===\n"
                    f"{result.text.strip()}\n=== END PLANNER BRIEF ===\n"
                )
        await repo.save_task_checkpoint(
            self._db,
            task_id=task.id,
            node_id="planner",
            attempt=0,
            status=(
                CheckpointStatus.completed if plan_preamble else CheckpointStatus.skipped
            ),
            git_tree_sha=checkpoint,
            artifact={"plan_preamble": plan_preamble},
        )
        if planner_node.id != "planner":
            await repo.save_task_checkpoint(
                self._db,
                task_id=task.id,
                node_id=planner_node.id,
                attempt=0,
                status=(
                    CheckpointStatus.completed if plan_preamble else CheckpointStatus.skipped
                ),
                git_tree_sha=checkpoint,
                artifact={"plan_preamble": plan_preamble},
            )
        await self._bus.emit(
            type=EventType.node_completed,
            project_id=task.project_id,
            task_id=task.id,
            agent=planner_agent,
            payload={
                "node_id": planner_node.id,
                "workflow_node_id": planner_node.id,
                "agent": planner_agent,
                "attempt": 0,
                "status": "completed" if plan_preamble else "skipped",
            },
        )
        plan_res = await self._await_human_approval(
            task,
            planner_node.id,
            0,
            config,
            cancel_event=cancel_event,
            git_tree_sha=checkpoint,
            artifact={"plan_preamble": plan_preamble},
            target_node_id="developer",
        )
        if plan_res and plan_res.get("output"):
            plan_preamble += f"\n\n=== OPERATOR PLAN NOTES ===\n{plan_res['output']}\n"
        return plan_preamble

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
        node: WorkflowNode | None = None,
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
        run_kwargs: dict[str, Any] = {}
        if node is not None:
            run_kwargs = {
                "node_id": node.id,
                "model_override": node.model,
                "effort_override": node.effort,
                "cli_agent_override": node.cli_agent,
                "skills_override": node.skills or None,
                "thinking_override": node.thinking,
            }
        for attempt in range(_REVIEWER_DISPATCH_RETRIES + 1):
            result = await runner.run(
                agent=agent,
                prompt=prompt,
                task_id=task.id,
                worktree=worktree,
                repo_knowledge=knowledge,
                project_rules=rules,
                cancel_event=cancel_event,
                **run_kwargs,
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

    async def _record_reviewer_verdict(
        self,
        node: WorkflowNode,
        result: AgentResult,
        verdict: ParsedVerdict,
        *,
        task: Task,
        project_id: str,
        worktree: Path,
        current_diff: str,
        attempt: int,
        config: ConclaveConfig,
        grounding_diff: str | None = None,
        pass_through: bool = False,
    ) -> ParsedVerdict:
        agent = node.agent or node.id
        effective_diff = grounding_diff if grounding_diff is not None else current_diff
        if verdict.source == "none" and not result.text.strip():
            verdict = ParsedVerdict(
                verdict="unknown",
                reason=f"{agent} agent unavailable after retries: {result.error}",
                source="none",
            )
            await self._bus.emit(
                type=EventType.grounding_warning,
                project_id=project_id,
                task_id=task.id,
                agent=agent,
                payload={
                    "warning": f"{agent} dispatch failed after retries; skipped (non-blocking)"
                },
            )
        warnings: list[str] = []
        if config.experimental.grounding_checks:
            verdict, warnings = check_grounding(verdict, effective_diff, worktree)
        grounded = sum(
            1
            for e in verdict.evidence
            if isinstance(e, dict)
            and isinstance(e.get("file"), str)
            and e["file"] in effective_diff
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
                type=EventType.grounding_warning,
                project_id=project_id,
                task_id=task.id,
                agent=agent,
                payload={"warning": warning},
            )
        await self._bus.emit(
            type=EventType.verdict,
            project_id=project_id,
            task_id=task.id,
            agent=agent,
            payload={"verdict": verdict.verdict, "reason": verdict.reason},
        )
        if pass_through:
            cp_ok = verdict.verdict != "unknown" or bool(result.text.strip())
        else:
            cp_ok = verdict.verdict == "pass"
        await repo.save_task_checkpoint(
            self._db,
            task_id=task.id,
            node_id=node.id,
            attempt=attempt,
            status=CheckpointStatus.completed if cp_ok else CheckpointStatus.failed,
            artifact={
                "agent": agent,
                "verdict": verdict.verdict,
                "reason": verdict.reason,
            },
        )
        await self._bus.emit(
            type=EventType.node_completed,
            project_id=project_id,
            task_id=task.id,
            agent=agent,
            payload={
                "node_id": node.id,
                "workflow_node_id": node.id,
                "agent": agent,
                "attempt": attempt,
                "verdict": verdict.verdict,
                "status": "completed" if cp_ok else "failed",
            },
        )
        rev_res = await self._await_human_approval(
            task,
            node.id,
            attempt,
            config,
            artifact={"agent": agent, "verdict": verdict.verdict, "reason": verdict.reason},
            target_node_id="gate" if (cp_ok or pass_through) else "developer",
        )
        if rev_res:
            override_verdict = str(rev_res.get("verdict") or "").lower()
            override_status = str(rev_res.get("status") or "").lower()
            override_reason = str(
                rev_res.get("message")
                or rev_res.get("output")
                or verdict.reason
                or "Operator override"
            )
            if override_verdict == "pass" or (
                not override_verdict
                and override_status in ("completed", "skipped")
                and verdict.verdict != "pass"
            ):
                verdict = ParsedVerdict(
                    verdict="pass",
                    reason=override_reason,
                    source="operator",
                    evidence=verdict.evidence,
                )
                await repo.add_verdict(
                    self._db,
                    task_id=task.id,
                    attempt=attempt,
                    agent=agent,
                    verdict="pass",
                    reason=override_reason,
                    source="operator",
                    grounded_count=grounded,
                    evidence=verdict.evidence,
                )
                await repo.save_task_checkpoint(
                    self._db,
                    task_id=task.id,
                    node_id=node.id,
                    attempt=attempt,
                    status=CheckpointStatus.completed,
                    artifact={"agent": agent, "verdict": "pass", "operator_override": True},
                )
            elif override_verdict in ("fail", "block") or override_status == "failed":
                verdict = ParsedVerdict(
                    verdict="fail",
                    reason=override_reason,
                    source="operator",
                    evidence=verdict.evidence,
                )
                await repo.add_verdict(
                    self._db,
                    task_id=task.id,
                    attempt=attempt,
                    agent=agent,
                    verdict="fail",
                    reason=override_reason,
                    source="operator",
                    grounded_count=grounded,
                    evidence=verdict.evidence,
                )
                await repo.save_task_checkpoint(
                    self._db,
                    task_id=task.id,
                    node_id=node.id,
                    attempt=attempt,
                    status=CheckpointStatus.failed,
                    artifact={"agent": agent, "verdict": "fail", "operator_override": True},
                )
        return verdict

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
        grounding_diff: str | None = None,
        diff_cmd: str,
        diff_cache: dict[BulkLimit, str] | None = None,
        integrity_section: str = "",
    ) -> tuple[bool, str, bool]:
        """Review the current diff through the derived agent pipeline.

        Each reviewer's prompt carries ``current_diff`` fitted to that reviewer's bulk limit;
        *diff_cmd* is the command that reproduces the full diff in the worktree, and readers
        sharing *diff_cache* share each cut. *integrity_section* (the attempt's TEST INTEGRITY
        findings, if any) follows the diff in every reviewer prompt, whole.

        Returns ``(failed, feedback, timed_out)``. ``timed_out`` is ``True`` when the
        wall-clock budget was exceeded mid-review — the caller must abort the task, not
        retry the attempt.
        """
        effective_diff = grounding_diff if grounding_diff is not None else current_diff
        pass_through = is_pass_through_review(config)
        review_nodes = derive_review_nodes(effective_diff, config)
        pipeline = [n.agent or n.id for n in review_nodes]
        await self._bus.emit(
            type=EventType.pipeline_derived, project_id=project_id, task_id=task.id,
            payload={"pipeline": pipeline},
        )
        await repo.save_task_checkpoint(
            self._db,
            task_id=task.id,
            node_id="review",
            attempt=attempt,
            status=CheckpointStatus.running,
            artifact={"pipeline": pipeline},
        )
        await self._bus.emit(
            type=EventType.node_started,
            project_id=project_id,
            task_id=task.id,
            payload={"node_id": "review", "attempt": attempt, "pipeline": pipeline},
        )
        base_prompt = f"Review the changes made for this task:\n{task.request}"
        passes = 0
        all_summaries: list[str] = []

        async def _with_diff(n: WorkflowNode) -> str:
            """*base_prompt* plus the diff fitted for reviewer *n* (nothing to show if blank),
            then the whole *integrity_section*."""
            if not current_diff.strip():
                return base_prompt + integrity_section
            diff = await self._fit_bulk(
                task, config, current_diff,
                block="diff",
                how_to_get_rest=_diff_hint(diff_cmd),
                agent=n.agent or n.id,
                node=n,
                cache=diff_cache,
            )
            return f"{base_prompt}\n\nDIFF:\n```diff\n{diff}\n```{integrity_section}"

        if is_parallel_review(config) and len(review_nodes) > 1:
            if cancel_event is not None and cancel_event.is_set():
                return True, "cancelled", False
            if _check_budget(started, budget):
                return True, "Wall-clock budget exceeded during review.", True

            for node in review_nodes:
                agent_name = node.agent or node.id
                await repo.save_task_checkpoint(
                    self._db,
                    task_id=task.id,
                    node_id=node.id,
                    attempt=attempt,
                    status=CheckpointStatus.running,
                    artifact={"agent": agent_name},
                )
                await self._bus.emit(
                    type=EventType.node_started,
                    project_id=project_id,
                    task_id=task.id,
                    agent=agent_name,
                    payload={
                        "node_id": node.id,
                        "workflow_node_id": node.id,
                        "agent": agent_name,
                        "attempt": attempt,
                    },
                )

            async def _build_rev_prompt(n: WorkflowNode) -> str:
                p = await _with_diff(n)
                if n.prompt_template:
                    p += f"\n\n{n.prompt_template}"
                node_steer = self._consume_node_steer(task.id, n.id) or (
                    self._consume_node_steer(task.id, n.agent)
                    if n.agent and n.agent != n.id
                    else None
                )
                if node_steer:
                    p += (
                        "\n\nOPERATOR STEER (live guidance from the operator — "
                        "prioritise this):\n" + node_steer
                    )
                return p

            # Build every prompt first, in pipeline order, so shared cuts are made (and
            # reported) once and deterministically before the reviewers run concurrently.
            rev_prompts = [await _build_rev_prompt(node) for node in review_nodes]
            dispatched = await asyncio.gather(
                *(
                    self._dispatch_reviewer(
                        runner,
                        node.agent or node.id,
                        rev_prompt,
                        task,
                        worktree,
                        knowledge,
                        rules,
                        cancel_event=cancel_event,
                        node=node,
                    )
                    for node, rev_prompt in zip(review_nodes, rev_prompts, strict=True)
                )
            )
            if cancel_event is not None and cancel_event.is_set():
                return True, "cancelled", False

            failures: list[str] = []
            for node, (result, raw_verdict) in zip(review_nodes, dispatched, strict=True):
                agent = node.agent or node.id
                verdict = await self._record_reviewer_verdict(
                    node,
                    result,
                    raw_verdict,
                    task=task,
                    project_id=project_id,
                    worktree=worktree,
                    current_diff=current_diff,
                    attempt=attempt,
                    config=config,
                    grounding_diff=effective_diff,
                    pass_through=pass_through,
                )
                all_summaries.append(
                    f"- {agent.upper()} ({verdict.verdict.upper()}): {verdict.reason}"
                )
                if verdict.verdict in ("fail", "block", "decline"):
                    header = f"{agent.upper()} FEEDBACK ({verdict.verdict.upper()})"
                    failures.append(f"\n{header}:\n{verdict.reason}\n")
                elif verdict.verdict == "pass":
                    passes += 1

            if failures and not pass_through:
                await repo.save_task_checkpoint(
                    self._db,
                    task_id=task.id,
                    node_id="review",
                    attempt=attempt,
                    status=CheckpointStatus.failed,
                    artifact={"passes": passes, "pipeline": pipeline},
                )
                await self._bus.emit(
                    type=EventType.node_completed,
                    project_id=project_id,
                    task_id=task.id,
                    payload={
                        "node_id": "review",
                        "attempt": attempt,
                        "status": "failed",
                        "passes": passes,
                    },
                )
                return True, "".join(failures), False
        else:
            failures = []
            for node in review_nodes:
                # Cooperative cancellation — check between each reviewer dispatch.
                if cancel_event is not None and cancel_event.is_set():
                    return True, "cancelled", False

                # HARD cap: abort the review if the wall-clock budget has been exceeded
                # before dispatching this reviewer. The orchestrator will fail the task
                # as timed-out rather than retrying the attempt.
                if _check_budget(started, budget):
                    return True, "Wall-clock budget exceeded during review.", True

                agent = node.agent or node.id
                await repo.save_task_checkpoint(
                    self._db,
                    task_id=task.id,
                    node_id=node.id,
                    attempt=attempt,
                    status=CheckpointStatus.running,
                    artifact={"agent": agent},
                )
                await self._bus.emit(
                    type=EventType.node_started,
                    project_id=project_id,
                    task_id=task.id,
                    agent=agent,
                    payload={
                        "node_id": node.id,
                        "workflow_node_id": node.id,
                        "agent": agent,
                        "attempt": attempt,
                    },
                )
                reviewer_prompt = await _with_diff(node)
                if node.prompt_template:
                    reviewer_prompt += f"\n\n{node.prompt_template}"
                node_steer = self._consume_node_steer(task.id, node.id) or (
                    self._consume_node_steer(task.id, agent) if agent != node.id else None
                )
                if node_steer:
                    reviewer_prompt += (
                        "\n\nOPERATOR STEER (live guidance from the operator — "
                        "prioritise this):\n" + node_steer
                    )
                    await self._bus.emit(
                        type=EventType.operator_steer,
                        project_id=project_id,
                        task_id=task.id,
                        payload={"message": node_steer, "attempt": attempt, "node_id": node.id},
                    )
                # ENG-4: _dispatch_reviewer already parsed the output; the verdict is used as-is.
                result, raw_verdict = await self._dispatch_reviewer(
                    runner, agent, reviewer_prompt, task, worktree, knowledge, rules,
                    cancel_event=cancel_event, node=node,
                )
                verdict = await self._record_reviewer_verdict(
                    node,
                    result,
                    raw_verdict,
                    task=task,
                    project_id=project_id,
                    worktree=worktree,
                    current_diff=current_diff,
                    attempt=attempt,
                    config=config,
                    grounding_diff=effective_diff,
                    pass_through=pass_through,
                )
                all_summaries.append(
                    f"- {agent.upper()} ({verdict.verdict.upper()}): {verdict.reason}"
                )
                if verdict.verdict in ("fail", "block", "decline"):
                    header = f"{agent.upper()} FEEDBACK ({verdict.verdict.upper()})"
                    failures.append(f"\n{header}:\n{verdict.reason}\n")
                    if not pass_through:
                        await repo.save_task_checkpoint(
                            self._db,
                            task_id=task.id,
                            node_id="review",
                            attempt=attempt,
                            status=CheckpointStatus.failed,
                            artifact={"passes": passes, "pipeline": pipeline},
                        )
                        await self._bus.emit(
                            type=EventType.node_completed,
                            project_id=project_id,
                            task_id=task.id,
                            payload={
                                "node_id": "review",
                                "attempt": attempt,
                                "status": "failed",
                                "passes": passes,
                            },
                        )
                        return True, f"\n{header}:\n{verdict.reason}\n", False
                if verdict.verdict == "pass":
                    passes += 1

        # ENG-3: never merge on an all-'unknown' round. If reviewers were derived but
        # none rendered a usable verdict — e.g. the model backend was rate-limited/degraded
        # and every reviewer fell back to non-blocking 'unknown' — the review tier is
        # effectively offline. Fail the attempt so the retry loop re-runs it (giving the
        # backend time to recover) rather than auto-merging unvetted code.
        usable_verdicts = passes + (len(failures) if pass_through else 0)
        if pipeline and usable_verdicts == 0:
            await repo.save_task_checkpoint(
                self._db,
                task_id=task.id,
                node_id="review",
                attempt=attempt,
                status=CheckpointStatus.failed,
                artifact={"passes": 0, "pipeline": pipeline},
            )
            await self._bus.emit(
                type=EventType.node_completed,
                project_id=project_id,
                task_id=task.id,
                payload={
                    "node_id": "review",
                    "attempt": attempt,
                    "status": "failed",
                    "passes": 0,
                },
            )
            return True, (
                "\nREVIEW INCONCLUSIVE: no reviewer returned a usable verdict "
                "(all 'unknown' — the model backend was likely unavailable). "
                "Not merging without at least one grounded review; retrying.\n"
            ), False
        summary_text = "\n".join(all_summaries) if pass_through else ""
        await repo.save_task_checkpoint(
            self._db,
            task_id=task.id,
            node_id="review",
            attempt=attempt,
            status=CheckpointStatus.completed,
            artifact={
                "passes": passes,
                "failures": len(failures),
                "pipeline": pipeline,
            },
        )
        await self._bus.emit(
            type=EventType.node_completed,
            project_id=project_id,
            task_id=task.id,
            payload={
                "node_id": "review",
                "attempt": attempt,
                "status": "completed",
                "passes": passes,
            },
        )
        return False, summary_text, False

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
        *,
        integrity: Sequence[IntegrityFinding] = (),
        integrity_mode: str = "off",
    ) -> bool:
        """Commit the reviewed work, then merge it when ``execution.auto_merge`` allows.

        A change with test-integrity findings under ``block`` mode is never merged here: it ends
        ``done`` on its committed branch for a human to review and merge.
        """
        note = _integrity_note(integrity, integrity_mode)
        has_merge_node = config.workflow is None or any(
            n.enabled and n.kind.value == "merge" for n in config.workflow.nodes
        )
        if not has_merge_node:
            summary_msg = (
                f"completed in {attempts} attempt(s); review/submitter workflow finished{note}"
            )
            await repo.finalize_task(
                self._db,
                task.id,
                state=TaskState.done,
                result_summary=summary_msg,
            )
            await self._bus.emit(
                type=EventType.task_done,
                project_id=task.project_id,
                task_id=task.id,
                payload={"attempts": attempts, "merged": False},
            )
            await wm.cleanup(task.id, task_branch)
            return True

        await repo.save_task_checkpoint(
            self._db,
            task_id=task.id,
            node_id="merge",
            attempt=attempts,
            status=CheckpointStatus.running,
            artifact={"branch": task_branch},
        )
        await self._bus.emit(
            type=EventType.node_started,
            project_id=task.project_id,
            task_id=task.id,
            payload={"node_id": "merge", "attempt": attempts},
        )
        await _stage_worktree_excluding_caches(worktree)
        _, status = await run_git(worktree, "status", "--porcelain")
        if status.strip():
            commit_rc, commit_out = await run_git(
                worktree, "commit", "-m", _commit_message(task)
            )
            if commit_rc != 0:
                # A rejected commit (e.g. a repository pre-commit hook) must never be
                # reported as success: merging now would ship nothing and cleanup would
                # destroy the reviewed work. Fail loudly and preserve worktree + branch.
                summary = (
                    f"commit failed (exit {commit_rc}); work preserved uncommitted in "
                    f"worktree {worktree} on branch {task_branch}:\n"
                    f"{commit_out.strip()[-_COMMIT_OUTPUT_TAIL_CHARS:]}{note}"
                )
                await repo.save_task_checkpoint(
                    self._db,
                    task_id=task.id,
                    node_id="merge",
                    attempt=attempts,
                    status=CheckpointStatus.failed,
                    artifact={"branch": task_branch, "worktree": str(worktree)},
                )
                await repo.finalize_task(
                    self._db, task.id, state=TaskState.failed,
                    result_summary=summary, block_children=True,
                )
                await self._bus.emit(
                    type=EventType.node_completed,
                    project_id=task.project_id,
                    task_id=task.id,
                    payload={"node_id": "merge", "attempt": attempts, "status": "failed"},
                )
                await self._bus.emit(
                    type=EventType.task_failed, project_id=task.project_id, task_id=task.id,
                    payload={
                        "reason": "commit_failed",
                        "branch": task_branch,
                        "worktree": str(worktree),
                        "exit_code": commit_rc,
                    },
                )
                logger.warning(
                    "task %s commit failed (exit %s); work preserved in %s",
                    task.id, commit_rc, worktree,
                )
                return False
        await repo.update_task_fields(self._db, task.id, branch=task_branch)
        await self._bus.emit(
            type=EventType.task_committed, project_id=task.project_id, task_id=task.id,
            payload={"branch": task_branch},
        )

        held = bool(integrity) and integrity_mode == "block"
        if config.execution.auto_merge and not held:
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
                summary = (
                    f"merge conflict into {target_branch}; branch {task_branch} preserved{note}"
                )
                await repo.finalize_task(
                    self._db, task.id, state=TaskState.failed, result_summary=summary,
                )
                await self._bus.emit(
                    type=EventType.node_completed,
                    project_id=task.project_id,
                    task_id=task.id,
                    payload={"node_id": "merge", "attempt": attempts, "status": "failed"},
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
                summary = (
                    f"merge error into {target_branch}; branch {task_branch} preserved{note}"
                )
                await repo.finalize_task(
                    self._db, task.id, state=TaskState.failed, result_summary=summary,
                )
                await self._bus.emit(
                    type=EventType.node_completed,
                    project_id=task.project_id,
                    task_id=task.id,
                    payload={"node_id": "merge", "attempt": attempts, "status": "failed"},
                )
                await self._bus.emit(
                    type=EventType.task_failed, project_id=task.project_id, task_id=task.id,
                    payload={"reason": "merge_error", "target": target_branch},
                )
                await wm.cleanup(task.id, None)
                return False
        else:
            merged = False

        await repo.save_task_checkpoint(
            self._db,
            task_id=task.id,
            node_id="merge",
            attempt=attempts,
            status=CheckpointStatus.completed,
            artifact={"merged": merged, "branch": task_branch},
        )
        await self._bus.emit(
            type=EventType.node_completed,
            project_id=task.project_id,
            task_id=task.id,
            payload={
                "node_id": "merge",
                "attempt": attempts,
                "status": "completed",
                "merged": merged,
            },
        )

        if held and config.execution.auto_merge:
            note += (
                "; not auto-merged: review it with conclave_get_task_diff, then merge it with "
                "conclave_merge_task or the UI"
            )
        # One transaction: flip state→done and write the result summary together so the
        # lifecycle row is never observed half-updated.
        await repo.finalize_task(
            self._db, task.id, state=TaskState.done,
            result_summary=f"completed in {attempts} attempt(s); merged={merged}{note}",
        )
        await self._bus.emit(
            type=EventType.task_done, project_id=task.project_id, task_id=task.id,
            payload={"attempts": attempts, "merged": merged},
        )
        # Keep the branch if unmerged (work preserved); drop it once merged.
        await wm.cleanup(task.id, task_branch if merged else None)
        return True

    async def _finish_failure(
        self,
        task: Task,
        wm: WorktreeManager,
        task_branch: str,
        timed_out: bool,
        attempts: int,
        *,
        integrity: Sequence[IntegrityFinding] = (),
        integrity_mode: str = "off",
    ) -> bool:
        reason = "timeout" if timed_out else "max_retries"
        note = _integrity_note(integrity, integrity_mode)
        # One transaction: fail the task, record why, and block its descendants together so
        # a crash can't leave the task failed while children stay claimable. Announce only
        # once the transition is durably committed.
        blocked = await repo.finalize_task(
            self._db, task.id, state=TaskState.failed,
            result_summary=f"failed ({reason}) after {attempts} attempt(s){note}",
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
        The feedback is passed exactly as the developer received it, so any gate output in
        it is already fitted (with its marker). The analysis is recorded as a
        ``postmortem_draft`` event.

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
        self,
        task: Task,
        wm: WorktreeManager,
        task_branch: str,
        message: str,
        *,
        reason: str | None = None,
    ) -> bool:
        """Fail *task* with *message* as its summary; the event's reason is *reason*, else it."""
        # One transaction: fail + record the reason + block descendants atomically, then
        # announce once it is durably committed.
        blocked = await repo.finalize_task(
            self._db, task.id, state=TaskState.failed, result_summary=message,
            block_children=True,
        )
        await self._bus.emit(
            type=EventType.task_failed, project_id=task.project_id, task_id=task.id,
            payload={"reason": reason or message},
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
                res = await self._merge_locked(repo_path, target_branch, task_branch, task_id)
                if (
                    res is MergeResult.success
                    and self._reloader is not None
                    and (repo_path / "src" / "conclave").is_dir()
                ):
                    self._reloader.request_reload(
                        self._daemon, reason=f"self_merge:{task_id}"
                    )
                return res
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


def _find_workflow_node(
    config: ConclaveConfig, stage_id: str, agent_name: str | None = None
) -> WorkflowNode | None:
    """Look up a matching enabled :class:`WorkflowNode` in ``config.workflow``."""
    if config.workflow is None:
        return None
    for node in config.workflow.nodes:
        if node.enabled and node.id == stage_id:
            return node
    if agent_name is not None:
        for node in config.workflow.nodes:
            if node.enabled and (node.agent == agent_name or node.id == agent_name):
                return node
    return None


def _has_enabled_developer(config: ConclaveConfig) -> bool:
    """Whether a developer runs: the default flow, or a workflow with an enabled developer.

    The same predicate as the ``dev_node_cfg`` lookups in :meth:`Orchestrator.process_task`.
    """
    if config.workflow is None:
        return True
    return _find_workflow_node(config, "developer", "developer") is not None or any(
        n.enabled and n.kind.value == "developer" for n in config.workflow.nodes
    )


def _missing_test_command(config: ConclaveConfig, knowledge: dict[str, Any] | None) -> bool:
    """Whether a task must fail before any dispatch because its green gate would run no tests.

    Only when the gate is required and not explicitly waived, and only for flows with a
    developer: a review-only flow changes nothing, and its gate is informational.
    """
    execution = config.execution
    return (
        execution.require_full_green
        and not execution.allow_no_test_command
        and _test_command(config, knowledge) is None
        and _has_enabled_developer(config)
    )


def _integrity_note(findings: Sequence[IntegrityFinding], mode: str) -> str:
    """The result-summary suffix recording test-integrity *findings* (``""`` when none)."""
    if not findings:
        return ""
    shown = ", ".join(finding.label() for finding in findings[:_INTEGRITY_NOTE_MAX_FINDINGS])
    note = f"; test integrity ({mode}): {len(findings)} finding(s): {shown}"
    hidden = len(findings) - _INTEGRITY_NOTE_MAX_FINDINGS
    if hidden > 0:
        note += f" +{hidden} more (see the test_integrity.flagged event)"
    return note


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


def _diff_hint(diff_cmd: str) -> str:
    """How an agent recovers a fitted diff: the command that produced it."""
    return f"Run `{diff_cmd}` in your working directory for the full diff."


def _rerun_hint(command: str) -> str:
    """How an agent recovers the fitted output of a gate/test or setup *command*."""
    return f"Re-run `{command}` in your working directory for the full output."


def _diff_stat(diff: str) -> str:
    lines = diff.splitlines()
    files = sum(1 for ln in lines if ln.startswith("diff --git"))
    adds = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    dels = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    return f"{files} file(s), +{adds}/-{dels}"


def _read_project_rules(worktree: Path, repo_path: Path | None = None) -> str:
    sections: list[str] = []
    if repo_path is not None and repo_path.parent != repo_path:
        ws_agents = repo_path.parent / "AGENTS.md"
        if ws_agents.is_file():
            ws_text = ws_agents.read_text(encoding="utf-8", errors="replace").strip()
            if ws_text:
                sections.append(ws_text)
    for name in _PROJECT_RULE_FILES:
        candidate = worktree / name
        if candidate.is_file():
            local_text = candidate.read_text(encoding="utf-8", errors="replace")
            if local_text.strip() and local_text.strip() not in sections:
                sections.append(local_text)
            break
    return "\n\n".join(sections)


def _test_command(config: ConclaveConfig, knowledge: dict[str, Any] | None) -> str | None:
    # A blank command would run as a no-op that always passes, so it counts as unset.
    command = config.execution.baseline_test_command
    if command and command.strip():
        return command
    if knowledge:
        commands = knowledge.get("commands")
        if isinstance(commands, dict):
            test = commands.get("test")
            if isinstance(test, str) and test.strip():
                return test
    return None


def _check_commands(knowledge: dict[str, Any] | None) -> list[str]:
    if not knowledge:
        return []
    cmds = knowledge.get("commands")
    if not isinstance(cmds, dict):
        return []
    result = []
    for key in ("check", "lint"):
        val = cmds.get(key)
        if isinstance(val, str) and val.strip():
            result.append(val.strip())
    return result


def _build_developer_prompt(
    task_or_request: Task | str,
    plan_preamble: str = "",
    baseline_preamble: str = "",
    test_command: str | None = None,
    check_commands: list[str] | None = None,
    *,
    use_memory: bool = False,
    memory: AttemptMemory | None = None,
    feedback: str = "",
    previous_diff: str = "",
) -> str:
    """Assemble the developer agent prompt including the mandatory Necessity & YAGNI check."""
    request = task_or_request.request if isinstance(task_or_request, Task) else task_or_request
    dev_prompt = f"{request}{plan_preamble}{baseline_preamble}\n\n{_NECESSITY_YAGNI_RULE}"
    all_cmds: list[str] = []
    if test_command:
        all_cmds.append(test_command)
    if check_commands:
        all_cmds.extend(check_commands)
    if all_cmds:
        # Hand the developer EVERY authoritative gate command so its own
        # write→run→fix loop converges on what the orchestrator actually
        # enforces — not a partial self-check that passes here but fails the gate.
        dev_prompt += (
            "\n\nGREEN-GATE (authoritative — exactly what the orchestrator runs "
            "to accept your work). Before you finish, run EVERY command below in "
            "the worktree and iterate (write → run → read failures → fix → re-run) "
            "until ALL commands pass with NO NEW failures vs. the pre-existing "
            "baseline:\n"
            + "\n".join(f"```\n{cmd}\n```" for cmd in all_cmds) + "\n"
        )
    if use_memory and memory is not None:
        dev_prompt += memory.build_preamble()
    if feedback:
        dev_prompt += (
            "\n\nLATEST REVIEWER FEEDBACK (must fix these issues):\n" + feedback
        )
        if previous_diff:
            dev_prompt += (
                "\n\nYOUR IMMEDIATELY PRIOR DIFF:\n```diff\n" + previous_diff + "\n```"
            )
    return dev_prompt


def _build_venv_guidance(
    worktree: Path,
    config: ConclaveConfig,
    knowledge: dict[str, Any] | None = None,
    repo_path: Path | None = None,
) -> str:
    """Build tool/venv guidance lines derived from the project's actual configured commands.

    Checks both ``worktree / ".venv"`` and ``repo_path / ".venv"`` (when provided).
    When ``.venv/Scripts`` exists (Windows), formats commands using ``.venv/Scripts/``
    instead of POSIX ``.venv/bin/``.
    """
    venv_path: Path | None = None
    for candidate in (
        worktree / ".venv",
        (repo_path / ".venv") if repo_path is not None else None,
    ):
        if candidate is not None and candidate.is_dir():
            venv_path = candidate
            break
    if venv_path is None:
        return ""

    has_scripts = (venv_path / "Scripts").is_dir() or (
        repo_path is not None and (repo_path / ".venv" / "Scripts").is_dir()
    )
    bin_prefix = ".venv/Scripts/" if has_scripts else ".venv/bin/"

    commands: list[str] = []

    # Test command — derived from config or repo knowledge.
    test_cmd = _test_command(config, knowledge)
    if test_cmd:
        commands.append(test_cmd.strip())

    # Lint / check commands from repo knowledge.
    commands.extend(_check_commands(knowledge))

    if not commands:
        return (
            "\n\n## Worktree environment (MANDATORY)\n"
            "A provisioned virtualenv exists at `.venv/` in the worktree root with all "
            f"dependencies installed.  Run tooling through `{bin_prefix}` so your checks "
            "match the green-gate exactly.  Do NOT use system-wide tools — they are a "
            "different toolchain and will disagree with the gate."
        )

    lines = [f"- `{bin_prefix}{cmd}`" for cmd in commands]
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


def _resolve_target_branch(project: Project, config: ConclaveConfig) -> str:
    """Resolve the branch a project's tasks integrate into.

    Precedence: explicit ``config.execution.target_branch`` stored on the project,
    then the project's ``default_branch``, then the model default. Shared by the
    orchestration loop and the read-only task diff so both always agree on the
    comparison base.
    """
    raw_exec = project.config.get("execution") if isinstance(project.config, dict) else None
    explicit_target = (
        raw_exec.get("target_branch")
        if isinstance(raw_exec, dict) and raw_exec.get("target_branch")
        else None
    )
    return str(explicit_target or project.default_branch or config.execution.target_branch)


_TRANSIENT_CACHE_DIRS: tuple[str, ...] = (
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
)


async def _stage_worktree_excluding_caches(worktree: Path) -> None:
    """Remove transient tool/bytecode cache dirs before ``git add -A`` so parallel
    worktrees never commit or conflict on ``__pycache__`` / ``.pytest_cache`` artifacts."""
    for cache_name in _TRANSIENT_CACHE_DIRS:
        for p in worktree.rglob(cache_name):
            if ".git" in p.parts or ".venv" in p.parts:
                continue
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
    await run_git(worktree, "add", "-A")

