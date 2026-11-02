"""End-to-end test for the test-integrity guard's git-backed entry point.

:func:`collect_integrity_findings` is the async half of the guard: it runs the real ``git diff``
plumbing (``--name-status`` gate, ``--raw`` listing, ``cat-file`` blob reads) in a worktree and
compares the before/after versions of the touched test files. These tests drive it through an
actual throwaway git repo, with the change STAGED but not committed exactly as the orchestrator
leaves it before review, so the *content-independent source* claim is proven end-to-end — the
findings come from git itself, not from any (truncatable) prompt text. The exhaustive parser
matrix lives in the unit tests; here we prove the wiring. All deterministic and LLM-free.
"""

from __future__ import annotations

from pathlib import Path

from conclave.engine import IntegrityFinding, collect_integrity_findings
from conclave.engine.gitio import run_git


async def _init_repo(path: Path) -> str:
    """A throwaway repo whose first commit holds Python and JS tests plus source; its SHA."""
    path.mkdir(parents=True, exist_ok=True)
    await run_git(path, "init", "-b", "main")
    await run_git(path, "config", "user.email", "test@example.com")
    await run_git(path, "config", "user.name", "Test")
    (path / "tests").mkdir()
    (path / "src").mkdir()
    (path / "web").mkdir()
    (path / "tests" / "test_modify.py").write_text(
        "def test_modify() -> None:\n    assert True\n\n\n"
        "def test_drop() -> None:\n    assert True\n"
    )
    (path / "tests" / "test_delete.py").write_text("def test_delete() -> None:\n    assert True\n")
    (path / "tests" / "test_skip.py").write_text("def test_skip() -> None:\n    assert True\n")
    (path / "tests" / "test_keep.py").write_text("def test_keep() -> None:\n    assert True\n")
    (path / "web" / "Cart.test.js").write_text(
        "describe('cart', () => {\n  it(\n    'adds items',\n    () => {},\n  )\n})\n"
    )
    (path / "web" / "Order.test.js").write_text("it('orders', () => {})\n")
    (path / "src" / "app.py").write_text("X = 1\n")
    await run_git(path, "add", "-A")
    await run_git(path, "commit", "-m", "initial commit")
    _, sha = await run_git(path, "rev-parse", "HEAD")
    return sha.strip()


async def _stage(path: Path) -> None:
    """Stage everything, as the orchestrator does after the developer before reviewing."""
    await run_git(path, "add", "-A")


async def test_collects_integrity_findings_from_real_git(tmp_path: Path) -> None:
    """A staged deletion, a removed test and an added skip are found; plain edits are not."""
    repo = tmp_path / "repo"
    checkpoint = await _init_repo(repo)

    (repo / "tests" / "test_modify.py").write_text(  # plain edit + a removed test
        "def test_modify() -> None:\n    assert 1 == 1  # tweaked\n"
    )
    (repo / "tests" / "test_delete.py").unlink()  # delete an existing test file
    (repo / "tests" / "test_skip.py").write_text(  # switch an existing test off
        "import pytest\n\n\n@pytest.mark.skip(reason='flaky')\n"
        "def test_skip() -> None:\n    assert True\n"
    )
    (repo / "tests" / "test_added.py").write_text(  # brand-new test → clean
        "def test_added() -> None:\n    assert True\n"
    )
    (repo / "src" / "app.py").write_text("X = 2\n")  # non-test modification → ignored
    await _stage(repo)

    findings = await collect_integrity_findings(repo, checkpoint)

    assert set(findings) == {
        IntegrityFinding("tests/test_delete.py", "", "deleted_file"),
        IntegrityFinding("tests/test_modify.py", "test_drop", "removed_test"),
        IntegrityFinding("tests/test_skip.py", "test_skip", "skip_marker"),
    }


async def test_javascript_test_files_are_read_whole(tmp_path: Path) -> None:
    """A ``*.test.js`` deletion and an ``it.skip`` are flagged; a multi-line ``it(`` is a test."""
    repo = tmp_path / "repo"
    checkpoint = await _init_repo(repo)

    (repo / "web" / "Cart.test.js").write_text("describe('cart', () => {})\n")  # it( removed
    (repo / "web" / "Order.test.js").unlink()
    await _stage(repo)
    assert set(await collect_integrity_findings(repo, checkpoint)) == {
        IntegrityFinding("web/Cart.test.js", "cart > adds items", "removed_test"),
        IntegrityFinding("web/Order.test.js", "", "deleted_file"),
    }

    await run_git(repo, "reset", "--hard", "-q", checkpoint)
    (repo / "web" / "Order.test.js").write_text("it.skip('orders', () => {})\n")
    await _stage(repo)
    assert await collect_integrity_findings(repo, checkpoint) == [
        IntegrityFinding("web/Order.test.js", "orders", "skip_marker"),
    ]


async def test_moved_and_renamed_tests_are_not_flagged(tmp_path: Path) -> None:
    """A test → test rename and a test moved into another file take no coverage away."""
    repo = tmp_path / "repo"
    checkpoint = await _init_repo(repo)

    await run_git(repo, "mv", "tests/test_keep.py", "tests/test_kept.py")
    (repo / "tests" / "test_modify.py").write_text("def test_modify() -> None:\n    assert True\n")
    (repo / "tests" / "test_moved.py").write_text("def test_drop() -> None:\n    assert True\n")
    await _stage(repo)

    assert await collect_integrity_findings(repo, checkpoint) == []


async def test_no_test_changes_yields_empty_list(tmp_path: Path) -> None:
    """A change that touches only source (and adds a new test) reports no integrity findings."""
    repo = tmp_path / "repo"
    checkpoint = await _init_repo(repo)

    (repo / "src" / "app.py").write_text("X = 99\n")
    (repo / "tests" / "test_added.py").write_text(
        "import pytest\n\n\n@pytest.mark.skip\ndef test_added() -> None:\n    assert True\n"
    )
    await _stage(repo)

    assert await collect_integrity_findings(repo, checkpoint) == []


async def test_bad_checkpoint_yields_empty_list(tmp_path: Path) -> None:
    """A git error (unknown ref) is reported as 'nothing observed', never a crash."""
    repo = tmp_path / "repo"
    await _init_repo(repo)

    assert await collect_integrity_findings(repo, "deadbeefdeadbeef") == []
