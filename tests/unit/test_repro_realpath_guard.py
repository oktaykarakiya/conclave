"""Unit tests for the bf-repro-realpath-guard (:func:`resolve_repro_test_path`).

The lexical bf-repro-pathguard is covered in ``test_repro.py``; this file exercises the
FILESYSTEM-aware layer that resolves a model-chosen repro path on disk and confines its realpath
to ``worktree/tests``. That confinement is the only check able to catch a *symlink escape*, where
every lexical component looks contained but a symlinked directory redirects the resolved target
out of the tests tree (or out of the worktree entirely). All deterministic and LLM-free.
"""

from __future__ import annotations

from pathlib import Path

from conclave.engine import resolve_repro_test_path


def _worktree_with_tests(tmp_path: Path) -> Path:
    """A worktree dir holding a real ``tests/`` tree — the in-bounds target for the guard."""
    worktree = tmp_path / "wt"
    (worktree / "tests").mkdir(parents=True)
    return worktree


# --- acceptance: a legitimate in-tree path ----------------------------------


def test_accepts_a_legitimate_in_tree_path(tmp_path: Path) -> None:
    """A clean relative test path under ``tests/`` resolves to its in-tree realpath."""
    worktree = _worktree_with_tests(tmp_path)
    result = resolve_repro_test_path(worktree, "tests/repro/test_target.py")
    # A non-existent tail is fine: the guard only RESOLVES; the writer mkdirs the parent later.
    assert result == (worktree / "tests/repro/test_target.py").resolve()
    assert result is not None and result.is_relative_to((worktree / "tests").resolve())


# --- rejection: absolute paths and ``..`` traversal -------------------------


def test_rejects_absolute_path(tmp_path: Path) -> None:
    """An absolute path never names an in-worktree test target."""
    worktree = _worktree_with_tests(tmp_path)
    assert resolve_repro_test_path(worktree, "/etc/test_evil.py") is None


def test_rejects_parent_traversal(tmp_path: Path) -> None:
    """Any ``..`` component that could climb out of the tests tree is refused."""
    worktree = _worktree_with_tests(tmp_path)
    assert resolve_repro_test_path(worktree, "../test_escape.py") is None
    assert resolve_repro_test_path(worktree, "tests/../../test_escape.py") is None


# --- rejection: symlink escapes (the realpath layer's reason to exist) -------


def test_rejects_symlink_escape_out_of_worktree(tmp_path: Path) -> None:
    """A symlinked dir inside ``tests/`` that points OUT of the worktree is refused.

    Every lexical component of ``tests/evil/test_x.py`` is contained, so only resolving the
    realpath on disk reveals that ``tests/evil`` redirects to a sibling outside the worktree.
    """
    worktree = _worktree_with_tests(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (worktree / "tests" / "evil").symlink_to(outside)
    assert resolve_repro_test_path(worktree, "tests/evil/test_x.py") is None


def test_rejects_symlink_redirect_out_of_tests_but_inside_worktree(tmp_path: Path) -> None:
    """A symlink that stays IN the worktree but leaves ``tests/`` is still refused.

    This is what tests-dir confinement buys over a bare worktree-root check: ``tests/sneak``
    redirects into ``src/`` (in-worktree, but out of the tests tree) — a vector to clobber source
    under the guise of a reproduction. The realpath lands in ``src/`` and is rejected.
    """
    worktree = _worktree_with_tests(tmp_path)
    (worktree / "src").mkdir()
    (worktree / "tests" / "sneak").symlink_to(worktree / "src")
    assert resolve_repro_test_path(worktree, "tests/sneak/test_x.py") is None


def test_accepts_symlink_that_stays_within_tests(tmp_path: Path) -> None:
    """A symlink is not refused merely for being one — only for escaping the tests tree.

    ``tests/link`` → ``tests/real`` keeps the realpath inside ``tests/``, so the guard accepts it.
    """
    worktree = _worktree_with_tests(tmp_path)
    (worktree / "tests" / "real").mkdir()
    (worktree / "tests" / "link").symlink_to(worktree / "tests" / "real")
    result = resolve_repro_test_path(worktree, "tests/link/test_x.py")
    assert result == (worktree / "tests" / "real" / "test_x.py").resolve()


# --- rejection: lexically clean, but outside the tests tree -----------------


def test_rejects_clean_path_outside_tests_dir(tmp_path: Path) -> None:
    """A path the LEXICAL guard accepts is still refused when it falls outside ``tests/``.

    ``test_top.py`` (top-level) and ``pkg/widget_test.py`` clear ``repro_pathguard`` but are not
    under the tests tree, so the realpath confinement — stricter than the lexical guard — rejects
    them. No symlink involved: this is the plain tests-dir bound.
    """
    worktree = _worktree_with_tests(tmp_path)
    assert resolve_repro_test_path(worktree, "test_top.py") is None
    assert resolve_repro_test_path(worktree, "pkg/widget_test.py") is None


def test_honors_a_custom_tests_dir(tmp_path: Path) -> None:
    """The confinement root is a parameter: a path under the named dir is accepted, others not."""
    worktree = tmp_path / "wt"
    (worktree / "spec").mkdir(parents=True)
    assert resolve_repro_test_path(worktree, "spec/test_x.py", tests_dir="spec") is not None
    # With ``spec`` as the bound, a default ``tests/`` path now falls outside it.
    assert resolve_repro_test_path(worktree, "tests/test_x.py", tests_dir="spec") is None


def test_rejects_non_string_candidate(tmp_path: Path) -> None:
    """Defense in depth: a non-string candidate is rejected by the lexical layer underneath."""
    worktree = _worktree_with_tests(tmp_path)
    assert resolve_repro_test_path(worktree, None) is None
    assert resolve_repro_test_path(worktree, 123) is None
