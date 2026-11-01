"""Unit tests for the bug-fixer test-integrity guard (engine/test_integrity.py).

Two pure functions, no git and no LLM:

* :func:`is_test_path` — the heuristic that recognises pytest / jest test files (and anything under
  a ``tests/`` directory).
* :func:`modified_or_deleted_tests` — parses ``git diff --name-status`` text into the set of
  PRE-EXISTING test files a task modified or deleted; purely-added test files are clean.

The headline case is SECURITY: the listing is parsed in full, so a deletion buried in the middle
that the fitted reviewer diff omits is still detected
(``test_deletion_omitted_from_the_fitted_prompt_diff_is_still_found``). All deterministic and
LLM-free.
"""

from __future__ import annotations

import pytest

from conclave.config import ContextSettings
from conclave.engine import is_test_path, modified_or_deleted_tests
from conclave.engine.context_limits import fit_block

# --- is_test_path: positives ------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "test_foo.py",  # pytest: leading test_
        "src/pkg/test_foo.py",  # ...nested
        "foo_test.py",  # pytest: trailing _test
        "tests/unit/helpers.py",  # any file under tests/ counts (shared fixtures)
        "tests/conftest.py",
        "a/b/tests/c/d.py",  # tests/ at any depth
        "web/Button.test.ts",  # jest/vitest
        "web/Button.test.tsx",
        "web/api.spec.ts",
        "web/api.spec.tsx",
    ],
)
def test_is_test_path_recognises_test_files(path: str) -> None:
    assert is_test_path(path) is True


# --- is_test_path: negatives ------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "src/app.py",  # ordinary source
        "src/testing.py",  # 'test' substring but not a test name
        "src/latest.py",
        "my_tests/thing.py",  # exact-component match: my_tests/ is NOT tests/
        "integration_tests/thing.py",
        "src/Button.ts",  # ts but not .test/.spec
        "src/widget.tsx",
        "README.md",
        "tests",  # a file literally named 'tests' is not "under tests/"
    ],
)
def test_is_test_path_rejects_non_test_files(path: str) -> None:
    assert is_test_path(path) is False


# --- modified_or_deleted_tests: the acceptance matrix -----------------------


def test_added_only_tests_are_clean() -> None:
    """Brand-new test files take no prior coverage away — the result is empty."""
    name_status = (
        "A\ttests/unit/test_new_feature.py\n"
        "A\tweb/Button.test.tsx\n"
        "M\tsrc/app.py\n"  # a non-test source modification is irrelevant
    )
    assert modified_or_deleted_tests(name_status) == set()


def test_modified_existing_test_is_detected() -> None:
    name_status = "M\ttests/unit/test_payments.py\n"
    assert modified_or_deleted_tests(name_status) == {"tests/unit/test_payments.py"}


def test_deleted_existing_test_is_detected() -> None:
    name_status = "D\ttests/unit/test_payments.py\n"
    assert modified_or_deleted_tests(name_status) == {"tests/unit/test_payments.py"}


def test_mixed_diff_reports_only_modified_and_deleted_tests() -> None:
    """Added test + modified test + deleted test + non-test churn → only the M/D tests."""
    name_status = (
        "A\ttests/unit/test_added.py\n"  # added → clean
        "M\ttests/unit/test_modified.py\n"  # modified → flagged
        "D\ttests/unit/test_deleted.py\n"  # deleted → flagged
        "M\tsrc/service.py\n"  # non-test modification → ignored
        "D\tdocs/old.md\n"  # non-test deletion → ignored
        "A\tsrc/new_module.py\n"  # non-test addition → ignored
        "M\tweb/Button.test.tsx\n"  # modified jest test → flagged
    )
    assert modified_or_deleted_tests(name_status) == {
        "tests/unit/test_modified.py",
        "tests/unit/test_deleted.py",
        "web/Button.test.tsx",
    }


def test_type_change_on_a_test_is_treated_as_modification() -> None:
    """A ``T`` (type-changed) test file altered prior coverage just like an ``M``."""
    assert modified_or_deleted_tests("T\ttests/unit/test_x.py\n") == {"tests/unit/test_x.py"}


# --- modified_or_deleted_tests: renames -------------------------------------


def test_rename_of_a_test_out_of_test_hood_flags_the_source() -> None:
    """Renaming a test to a non-test path silently drops it from collection — flag the source."""
    name_status = "R100\ttests/unit/test_critical.py\tnotes/disabled_critical.py.bak\n"
    assert modified_or_deleted_tests(name_status) == {"tests/unit/test_critical.py"}


def test_benign_test_to_test_rename_is_not_flagged() -> None:
    """A test → test relocation keeps it collectable; it removes no coverage."""
    name_status = "R100\ttests/unit/test_a.py\ttests/unit/renamed/test_a.py\n"
    assert modified_or_deleted_tests(name_status) == set()


def test_copy_leaves_source_intact_and_is_ignored() -> None:
    """A copy preserves the source test, so it takes nothing away."""
    name_status = "C75\ttests/unit/test_a.py\ttests/unit/test_b.py\n"
    assert modified_or_deleted_tests(name_status) == set()


# --- modified_or_deleted_tests: robustness ----------------------------------


def test_blank_and_malformed_lines_are_ignored() -> None:
    """Empty lines and status-only records must not crash or pollute the result."""
    name_status = "\n  \nM\nD\ttests/unit/test_real.py\n\n"
    assert modified_or_deleted_tests(name_status) == {"tests/unit/test_real.py"}


def test_empty_input_is_empty_set() -> None:
    assert modified_or_deleted_tests("") == set()


# --- the SECURITY case: a deletion the fitted prompt diff omits -------------


def test_deletion_omitted_from_the_fitted_prompt_diff_is_still_found() -> None:
    """A test deletion buried mid-listing is detected — the whole listing is scanned.

    Prompt diffs are fitted to ``context.bulk_max_chars`` by keeping only their head and tail, so
    a deletion whose record lands in the omitted middle would be invisible there.
    ``--name-status`` is content-independent (one short line per file), and this parser reads ALL
    of it, so the buried deletion is still reported. The surrounding churn here is all non-test,
    so the deletion is the ONLY result — proving it was actually found rather than swept in with
    everything else.
    """
    # Tens of thousands of non-test modifications, enough to exceed the default prompt limit.
    churn = [f"M\tsrc/module_{i:05d}.py\n" for i in range(30_000)]
    buried_deletion = "D\ttests/unit/test_buried_deep.py\n"
    half = len(churn) // 2
    name_status = "".join(churn[:half]) + buried_deletion + "".join(churn[half:])

    # A reader of the fitted prompt text really would have missed the record.
    limit = ContextSettings().bulk_max_chars
    fitted = fit_block(name_status, limit=limit, field="bulk_max_chars", how_to_get_rest="-")
    assert fitted.truncated
    assert buried_deletion not in fitted.text

    assert modified_or_deleted_tests(name_status) == {"tests/unit/test_buried_deep.py"}
