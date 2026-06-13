"""Git worktree lifecycle.

Each task runs in an isolated worktree rooted OUTSIDE the target repo (under
``$CONCLAVE_HOME/projects/<id>/worktrees/<task_id>``), sharing the repo's object
store. The operator's own checkout is never touched and no tool state lands in the
repo tree — replacing team-ai's in-tree branching + ``git clean -e`` hacks.
"""

from __future__ import annotations

from pathlib import Path

from .gitio import run_git


class WorktreeError(RuntimeError):
    pass


class WorktreeManager:
    def __init__(self, repo_path: Path, worktrees_root: Path) -> None:
        self.repo_path = repo_path
        self.root = worktrees_root

    def path_for(self, task_id: str) -> Path:
        return self.root / task_id

    async def create(self, task_id: str, base_branch: str, task_branch: str) -> Path:
        """Create a fresh worktree with ``task_branch`` branched from ``base_branch``."""
        path = self.path_for(task_id)
        await self.cleanup(task_id, task_branch)
        self.root.mkdir(parents=True, exist_ok=True)
        code, out = await run_git(
            self.repo_path, "worktree", "add", "-b", task_branch, str(path), base_branch
        )
        if code != 0:
            raise WorktreeError(f"git worktree add failed: {out.strip()}")
        return path

    async def cleanup(self, task_id: str, task_branch: str | None = None) -> None:
        """Remove the worktree (and optionally its branch); tolerant of absence."""
        path = self.path_for(task_id)
        await run_git(self.repo_path, "worktree", "remove", "--force", str(path))
        await run_git(self.repo_path, "worktree", "prune")
        if task_branch:
            await run_git(self.repo_path, "branch", "-D", task_branch)

    async def list_branches(self) -> list[str]:
        code, out = await run_git(self.repo_path, "worktree", "list", "--porcelain")
        if code != 0:
            return []
        branches: list[str] = []
        for line in out.splitlines():
            if line.startswith("branch "):
                branches.append(line.removeprefix("branch ").strip())
        return branches
