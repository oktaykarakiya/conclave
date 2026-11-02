Make the test suite platform-neutral and run the backend CI on Windows too. A Windows install reports that all 533 tests pass there once two test defects are fixed; CI only runs on Linux, so nothing catches this class of bug. Test and CI changes only.

1. `tests/integration/test_orchestrator.py` compares worktree paths with the raw text of `git worktree list`: `assert str(worktree_path) in wt_list` / `not in wt_list` (around lines 153, 202, 427, 1328 and 1408; in the last three `worktree_path` is built as a `str`). Git for Windows prints paths with forward slashes, so on Windows the positive check fails and the negative checks pass without testing anything. Add one small typed helper in that module, e.g. `async def _worktree_paths(repo_path: Path) -> set[Path]`, that runs `git worktree list --porcelain` through the existing `run_git`, takes every line starting with `worktree `, and returns `{Path(p).resolve() for ...}`. Replace all five checks with `<path>.resolve() in await _worktree_paths(repo_path)` / `not in` (build a `Path`, not a `str`). Leave every other assertion in those tests unchanged.

2. `tests/integration/test_ws.py::_wait_for_subscribers` spins 500 x `await asyncio.sleep(0)`. The planning-stream handler awaits an aiosqlite lookup (it runs in a worker thread) before it subscribes, so a fixed number of event-loop ticks is not a time bound and the test flakes on slower hosts (about 13% on Windows). Replace it with a deadline-based wait: poll every 0.01 s for up to 5 s (use the running loop's clock), then raise the same AssertionError message as today.

3. Search the rest of `tests/` for the same two defect classes: (a) a filesystem path compared as a string against git or subprocess output; (b) waiting with a fixed count of `asyncio.sleep(0)` for work that involves a thread, subprocess or the database. Fix every real occurrence the same way and list what you found and changed in your final message. Do not touch tests that are not affected.

4. `.github/workflows/ci.yml`: run the `backend` job on a matrix `os: [ubuntu-latest, windows-latest]` with `fail-fast: false`, the job name showing the OS (e.g. `Backend (${{ matrix.os }})`), Python 3.12 and the same install/ruff/mypy/pytest steps. Keep the frontend job on ubuntu-latest only. Do not add caching, new tools or new steps.

Constraints:
- No production code changes (nothing under `src/`).
- No test may be skipped, marked xfail or weakened on any platform; every assertion keeps or gains strength.
- Windows itself can only be verified by CI after a push, so every change must be platform-neutral by construction.

Tests: the existing tests you touch must pass; no new test files are needed.

Quality gate (must stay green): `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit`.
