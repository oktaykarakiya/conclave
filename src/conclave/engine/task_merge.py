"""Operator-triggered merge of a finished task, shared by the REST API and the MCP server.

When ``execution.auto_merge`` is off a done task keeps its committed branch. This
helper merges that branch into the project's target branch through the orchestrator's
own :meth:`Orchestrator._merge` (per-target lock, guarded fast-forward / merge commit,
conflict abort), then finishes exactly like the auto-merge path: ``task.merged`` event
and branch deletion.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import resolve_project_config
from ..db import Project, Task, TaskState
from ..db import repositories as repo
from ..events import EventType
from .gitio import run_git
from .orchestrator import MergeResult, Orchestrator, _resolve_target_branch
from .task_diff import _find_task_branch
from .worktree import WorktreeManager


class TaskMergeError(RuntimeError):
    """A task could not be merged (``status_code`` drives the REST reply)."""

    def __init__(self, detail: str, status_code: int) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


async def _branch_sha(repo_path: Path, branch: str) -> str | None:
    code, out = await run_git(
        repo_path, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"
    )
    return out.strip() if code == 0 and out.strip() else None


async def merge_task(
    orchestrator: Orchestrator, project: Project, task: Task
) -> dict[str, Any]:
    """Merge a done task's branch into the project's target branch."""
    if task.state != TaskState.done:
        raise TaskMergeError(f"task is {task.state}, not done", 409)

    config = await resolve_project_config(orchestrator._db, project)
    target = _resolve_target_branch(project, config)
    repo_path = Path(project.path)

    found = await _find_task_branch(repo_path, config, task) if repo_path.is_dir() else None
    if found is None:
        raise TaskMergeError("task branch not found", 404)
    branch, branch_sha = found

    # _merge assumes the target exists; without it git runs in a missing merge worktree.
    target_sha = await _branch_sha(repo_path, target)
    if target_sha is None:
        raise TaskMergeError(f"target branch not found: {target}", 404)
    code, _ = await run_git(repo_path, "merge-base", "--is-ancestor", branch_sha, target_sha)
    if code == 0:
        raise TaskMergeError(f"task branch {branch} is already merged into {target}", 409)

    result = await orchestrator._merge(repo_path, target, branch, task.id)
    if result == MergeResult.conflict:
        raise TaskMergeError(
            f"merge conflict merging {branch} into {target}; branch preserved", 409
        )
    if result != MergeResult.success:
        raise TaskMergeError(f"transient merge error into {target}; retry", 503)

    await repo.update_task_fields(
        orchestrator._db, task.id, result_summary=f"merged into {target} by operator"
    )
    await orchestrator._bus.emit(
        type=EventType.task_merged, project_id=task.project_id, task_id=task.id,
        payload={"target": target},
    )
    wm = WorktreeManager(repo_path, orchestrator._home / "projects" / project.id / "worktrees")
    await wm.cleanup(task.id, branch)
    return {
        "task_id": task.id,
        "merged": True,
        "target": target,
        "branch": branch,
        "target_sha": await _branch_sha(repo_path, target),
    }
