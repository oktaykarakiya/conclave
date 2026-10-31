Bug: when the final `git commit` in a task worktree fails (for example a repository pre-commit hook rejects it), the orchestrator ignores the exit code (`orchestrator.py`, the `run_git(worktree, "commit", ...)` call in the commit/merge stage). It then reports success: it merges or finishes a branch that does not contain the work, deletes the branch and force-removes the worktree — so the developer's reviewed, gate-green changes are silently lost. Fix it.

Required behaviour when `git commit` exits non-zero:
1. `process_task` returns False and the task is finalized as `failed` (same transactional path as other failures, so descendants are blocked).
2. `result_summary` starts with `commit failed` and includes the tail of git's output (bounded, at most 500 characters), so the operator can see e.g. the hook's message.
3. A `task.failed` event is emitted with payload `reason: "commit_failed"`.
4. No merge is attempted; the target branch is unchanged.
5. The work is preserved: the task worktree (with the uncommitted, already-staged changes) is NOT removed, and the task branch is NOT deleted. Include the worktree path in `result_summary`.
6. Never bypass hooks (`--no-verify` is not acceptable): hooks are the repository owner's policy.
7. The successful-commit path, and the existing "nothing to commit" path, behave exactly as before.

Tests: integration tests with a real repository whose `.git/hooks/pre-commit` exits 1 — assert the task fails with the summary/event above, the target is unchanged, and the changed file still exists (uncommitted) in the preserved worktree; plus a regression test that a normal commit still merges.

Keep the quality gate green: `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit`. Follow AGENTS.md conventions.
