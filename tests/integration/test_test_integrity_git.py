"""End-to-end test for the test-integrity guard's git-backed entry point.

:func:`collect_modified_or_deleted_tests` is the async half of the guard: it runs the real
``git diff --name-status <checkpoint>`` plumbing in a worktree and classifies the output. These
tests drive it through an actual throwaway git repo so the *content-independent source* claim is
proven end-to-end — that the file set comes from git itself, not from any (truncatable) prompt
text. The exhaustive M/D/A/rename matrix lives in the unit tests; here we prove the wiring.
All deterministic and LLM-free.
"""

from __future__ import annotations

from pathlib import Path

from conclave.engine import collect_modified_or_deleted_tests
from conclave.engine.gitio import run_git


async def _init_repo(path: Path) -> str:
    """A throwaway repo whose initial commit holds three tests + a source file. Returns its SHA."""
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    await run_git(path, "config", "user.email", "test@example.com")
    await run_git(path, "config", "user.name", "Test")
    (path / "tests").mkdir()
    (path / "src").mkdir()
    (path / "tests" / "test_modify.py").write_text("def test_modify() -> None:\n    assert True\n")
    (path / "tests" / "test_delete.py").write_text("def test_delete() -> None:\n    assert True\n")
    (path / "tests" / "test_keep.py").write_text("def test_keep() -> None:\n    assert True\n")
    (path / "src" / "app.py").write_text("X = 1\n")
    await run_git(path, "add", "-A")
    await run_git(path, "commit", "-m", "initial commit")
    _, sha = await run_git(path, "rev-parse", "HEAD")
    return sha.strip()


async def test_collects_modified_and_deleted_tests_from_real_git(tmp_path: Path) -> None:
    """Against a real checkpoint: modified + deleted tests are reported, added/untouched are not."""
    repo = tmp_path / "repo"
    checkpoint = await _init_repo(repo)

    # Mutate the tree the way a bug-fixer task would, then commit so the diff is unambiguous.
    (repo / "tests" / "test_modify.py").write_text(
        "def test_modify() -> None:\n    assert True  # tweaked\n"
    )
    (repo / "tests" / "test_delete.py").unlink()  # delete an existing test
    (repo / "tests" / "test_added.py").write_text(  # brand-new test → clean
        "def test_added() -> None:\n    assert True\n"
    )
    (repo / "src" / "app.py").write_text("X = 2\n")  # non-test modification → ignored
    await run_git(repo, "add", "-A")
    await run_git(repo, "commit", "-m", "task changes")

    touched = await collect_modified_or_deleted_tests(repo, checkpoint)

    assert touched == {"tests/test_modify.py", "tests/test_delete.py"}


async def test_no_test_changes_yields_empty_set(tmp_path: Path) -> None:
    """A task that touches only source (and adds a new test) reports no integrity violations."""
    repo = tmp_path / "repo"
    checkpoint = await _init_repo(repo)

    (repo / "src" / "app.py").write_text("X = 99\n")
    (repo / "tests" / "test_added.py").write_text("def test_added() -> None:\n    assert True\n")
    await run_git(repo, "add", "-A")
    await run_git(repo, "commit", "-m", "source-only change")

    assert await collect_modified_or_deleted_tests(repo, checkpoint) == set()


async def test_bad_checkpoint_yields_empty_set(tmp_path: Path) -> None:
    """A git error (unknown ref) is reported as 'nothing observed', never a crash."""
    repo = tmp_path / "repo"
    await _init_repo(repo)

    assert await collect_modified_or_deleted_tests(repo, "deadbeefdeadbeef") == set()
