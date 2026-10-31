"""Read-only task diff computation shared by the REST API and the MCP server.

The same helper backs both surfaces, so a task's diff body is identical whether
an operator asks over HTTP or through an MCP tool call. Nothing here mutates the
repository, the worktree, or the worktree's real index.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..config import ConclaveConfig, load_project_config
from ..db import Project, Task
from .gitio import run_git
from .orchestrator import _TRANSIENT_CACHE_DIRS, _resolve_target_branch
from .worktree import WorktreeManager

DEFAULT_DIFF_MAX_BYTES = 200_000
MAX_DIFF_MAX_BYTES = 2_000_000


class TaskDiffError(RuntimeError):
    """A task diff could not be produced (``status_code`` drives the REST reply)."""

    def __init__(self, detail: str, status_code: int) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    """Cut *text* to at most *max_bytes* UTF-8 bytes without splitting a character."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text, False
    return raw[:max_bytes].decode("utf-8", errors="ignore"), True


def _worktree_pathspec() -> list[str]:
    """All paths under the worktree root, minus the orchestrator's transient caches."""
    return [
        "--",
        ".",
        *(f":(exclude,glob)**/{name}/**" for name in _TRANSIENT_CACHE_DIRS),
    ]


def _payload(
    task_id: str,
    source: str,
    base: str,
    head: str | None,
    stat: str,
    diff: str,
    truncated: bool,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "source": source,
        "base": base,
        "head": head,
        "stat": stat,
        "diff": diff,
        "truncated": truncated,
    }


async def _merge_base(repo: Path, target_branch: str, head: str) -> str:
    """``git merge-base <target> <head>`` with the orchestrator's ``origin/`` fallback."""
    code, out = await run_git(repo, "merge-base", target_branch, head)
    if code != 0 and not target_branch.startswith("origin/"):
        code, out = await run_git(repo, "merge-base", f"origin/{target_branch}", head)
    if code != 0:
        raise TaskDiffError("could not compute the diff base", 409)
    return out.strip()


async def _diff_pair(
    repo: Path,
    revisions: list[str],
    pathspec: list[str],
    *,
    stat_only: bool,
    max_bytes: int,
    env: dict[str, str] | None = None,
) -> tuple[str, str, bool]:
    """Return ``(stat, diff, truncated)`` for one comparison."""
    code, stat = await run_git(
        repo,
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--stat",
        *revisions,
        *pathspec,
        env=env,
    )
    if code != 0:
        raise TaskDiffError("could not compute the task diff", 409)
    if stat_only:
        return stat, "", False
    code, diff = await run_git(
        repo, "diff", "--no-color", "--no-ext-diff", *revisions, *pathspec, env=env
    )
    if code != 0:
        raise TaskDiffError("could not compute the task diff", 409)
    diff, truncated = _truncate_utf8(diff, max_bytes)
    return stat, diff, truncated


async def _worktree_diff(
    worktree: Path,
    base: str,
    *,
    stat_only: bool,
    max_bytes: int,
) -> tuple[str, str, bool]:
    """Diff the live worktree (untracked files included) against *base*.

    Untracked files are recorded in a COPY of the worktree index via
    ``git add --intent-to-add`` so the real index, refs, and files stay untouched.
    """
    pathspec = _worktree_pathspec()
    code, out = await run_git(worktree, "rev-parse", "--git-path", "index")
    index_path: Path | None = None
    if code == 0 and out.strip():
        index_path = Path(out.strip())
        if not index_path.is_absolute():
            index_path = worktree / index_path

    with tempfile.TemporaryDirectory() as tmp:
        tmp_index = Path(tmp) / "index"
        if index_path is not None and index_path.is_file():
            shutil.copy2(index_path, tmp_index)
        env = {"GIT_INDEX_FILE": str(tmp_index)}
        code, _ = await run_git(
            worktree, "add", "--intent-to-add", "--all", *pathspec, env=env
        )
        if code != 0:
            raise TaskDiffError("could not stage the worktree for diffing", 409)
        return await _diff_pair(
            worktree,
            [base],
            pathspec,
            stat_only=stat_only,
            max_bytes=max_bytes,
            env=env,
        )


async def _find_task_branch(
    repo_path: Path, config: ConclaveConfig, task: Task
) -> tuple[str, str] | None:
    """Return ``(branch, sha)`` for the first existing task branch, else ``None``."""
    candidates = dict.fromkeys([f"{config.execution.branch_prefix}{task.id}", task.branch])
    for name in candidates:
        if not name:
            continue
        code, out = await run_git(
            repo_path, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}^{{commit}}"
        )
        if code == 0 and out.strip():
            return name, out.strip()
    return None


async def compute_task_diff(
    home: Path,
    project: Project,
    task: Task,
    *,
    stat_only: bool = False,
    max_bytes: int = DEFAULT_DIFF_MAX_BYTES,
) -> dict[str, Any]:
    """Resolve the task's change source and return the shared diff payload.

    Source order: the live worktree, then the task branch, then a 404.
    """
    config = load_project_config(project.config)
    target_branch = _resolve_target_branch(project, config)
    repo_path = Path(project.path)
    worktree = WorktreeManager(
        repo_path, home / "projects" / project.id / "worktrees"
    ).path_for(task.id)

    if (worktree / ".git").exists():
        base = await _merge_base(worktree, target_branch, "HEAD")
        stat, diff, truncated = await _worktree_diff(
            worktree, base, stat_only=stat_only, max_bytes=max_bytes
        )
        return _payload(task.id, "worktree", base, None, stat, diff, truncated)

    if repo_path.is_dir():
        found = await _find_task_branch(repo_path, config, task)
        if found is not None:
            _, head = found
            base = await _merge_base(repo_path, target_branch, head)
            stat, diff, truncated = await _diff_pair(
                repo_path,
                [base, head],
                [],
                stat_only=stat_only,
                max_bytes=max_bytes,
            )
            return _payload(task.id, "branch", base, head, stat, diff, truncated)

    raise TaskDiffError("no diff available for this task", 404)
