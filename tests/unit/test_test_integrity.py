"""Unit tests for the test-integrity guard (engine/test_integrity.py).

Pure functions, no git and no LLM:

* :func:`is_test_path` — the heuristic that recognises pytest / jest / vitest / mocha test files
  (and anything under a ``tests/``, ``test/`` or ``__tests__/`` directory).
* :func:`modified_or_deleted_tests` — parses ``git diff --name-status`` text into the set of
  PRE-EXISTING test files a task modified or deleted; purely-added test files are clean.
* :func:`integrity_findings` — compares the before/after contents of a change's test files: deleted
  test files, removed tests and added skip markers are flagged; plain edits, moves and new test
  files are not.
* ``_parse_raw_diff`` and :func:`integrity_prompt_section` — the ``git diff --raw -z`` listing
  parser and the reviewer section.

The headline case is SECURITY: listings and contents are parsed in full, so a deletion buried in
the middle that the fitted reviewer diff omits is still detected
(``test_deletion_omitted_from_the_fitted_prompt_diff_is_still_found`` and the two ``..._whole``
tests). All deterministic and LLM-free.
"""

from __future__ import annotations

import pytest

from conclave.config import ContextSettings
from conclave.engine import (
    FileChange,
    IntegrityFinding,
    integrity_findings,
    is_test_path,
    modified_or_deleted_tests,
)
from conclave.engine.context_limits import fit_block
from conclave.engine.test_integrity import _parse_raw_diff, integrity_prompt_section

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
        "web/Cart.test.js",  # every JS/TS extension, test and spec
        "web/Cart.test.jsx",
        "web/cart.spec.js",
        "web/cart.test.mjs",
        "web/cart.test.cjs",
        "web/cart.spec.mts",
        "web/cart.test.cts",
        "web/test.ts",  # jest's ?(*.)+(spec|test) also matches a bare test.ts
        "src/__tests__/cart.js",  # jest's __tests__ directory
        "test/cart.js",  # mocha's default test/ directory
        "conftest.py",  # pytest fixtures and hooks outside tests/
        "pkg/conftest.py",
        "app/tests.py",  # unittest discovery / Django's per-app tests module
        "cypress/e2e/login.cy.ts",  # cypress
        "cypress/e2e/login.cy.js",
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
        "web/Cart.js",  # js but not .test/.spec
        "web/latest.mjs",
        "web/cart.test.d.ts",  # a type declaration, not a test
        "docs/spec.md",  # spec, but not a JS/TS file
        "src/contest.py",
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


# --- integrity_findings: Python ---------------------------------------------

_PY = """import pytest


class TestCart:
    def test_total(self) -> None:
        assert total([1, 2]) == 3

    def test_tax(self) -> None:
        assert tax(10) == 2


def test_empty() -> None:
    \"\"\"Mentions @pytest.mark.skip and pytest.skip( in prose only.\"\"\"
    assert total([]) == 0  # pytest.skip("not a marker in a comment")


async def test_async() -> None:
    assert await fetch() == 1
"""

_ASYNC_TEST = "async def test_async() -> None:\n    assert await fetch() == 1\n"
_WITHOUT_ASYNC_TEST = _PY.replace(_ASYNC_TEST, "")


def _edited(path: str, before: str, after: str) -> list[IntegrityFinding]:
    """Findings for one pre-existing file modified from *before* to *after*."""
    return integrity_findings([FileChange("M", path, path, before, after)])


def test_deleted_test_file_is_flagged() -> None:
    assert integrity_findings([FileChange("D", "tests/test_cart.py", None)]) == [
        IntegrityFinding("tests/test_cart.py", "", "deleted_file")
    ]


def test_deleted_binary_or_unparsed_test_file_is_flagged() -> None:
    """A deletion needs no content: a test-path fixture or binary file is flagged too."""
    assert integrity_findings([FileChange("D", "tests/fixtures/golden.png", None)]) == [
        IntegrityFinding("tests/fixtures/golden.png", "", "deleted_file")
    ]


def test_rename_out_of_test_hood_is_a_deleted_file() -> None:
    changes = [FileChange("R", "tests/test_cart.py", "notes/test_cart.py.bak", _PY, _PY)]
    assert integrity_findings(changes) == [
        IntegrityFinding("tests/test_cart.py", "", "deleted_file")
    ]


def test_removed_test_function_is_flagged() -> None:
    assert _edited("tests/test_cart.py", _PY, _WITHOUT_ASYNC_TEST) == [
        IntegrityFinding("tests/test_cart.py", "test_async", "removed_test")
    ]


def test_removed_test_class_method_is_flagged_with_its_class() -> None:
    after = _PY.replace("    def test_tax(self) -> None:\n        assert tax(10) == 2\n", "")
    assert _edited("tests/test_cart.py", _PY, after) == [
        IntegrityFinding("tests/test_cart.py", "TestCart.test_tax", "removed_test")
    ]


def test_test_removed_and_re_added_in_another_file_is_not_flagged() -> None:
    """A moved test takes no coverage away."""
    changes = [
        FileChange("M", "tests/test_cart.py", "tests/test_cart.py", _PY, _WITHOUT_ASYNC_TEST),
        FileChange("A", None, "tests/test_fetch.py", None, _ASYNC_TEST),
    ]
    assert integrity_findings(changes) == []


def test_test_to_test_rename_is_not_flagged() -> None:
    changes = [FileChange("R", "tests/test_cart.py", "tests/unit/test_cart.py", _PY, _PY)]
    assert integrity_findings(changes) == []


def test_changed_assertion_and_new_test_are_not_flagged() -> None:
    after = _PY.replace("== 3", "== 4") + "\n\ndef test_new() -> None:\n    assert True\n"
    assert _edited("tests/test_cart.py", _PY, after) == []


def test_new_test_file_is_not_flagged_even_with_markers() -> None:
    new = "import pytest\n\n\n@pytest.mark.skip\ndef test_later() -> None:\n    pass\n"
    assert integrity_findings([FileChange("A", None, "tests/test_later.py", None, new)]) == []


def test_added_skip_decorator_is_flagged_on_the_decorated_test() -> None:
    after = _PY.replace("def test_empty()", "@pytest.mark.skip(reason='flaky')\ndef test_empty()")
    assert _edited("tests/test_cart.py", _PY, after) == [
        IntegrityFinding("tests/test_cart.py", "test_empty", "skip_marker")
    ]


@pytest.mark.parametrize(
    "decorator",
    [
        "@pytest.mark.skipif(sys.platform == 'linux', reason='x')",
        "@pytest.mark.xfail",
        "@unittest.skip('x')",
        "@unittest.expectedFailure",
    ],
)
def test_other_skip_decorators_are_flagged(decorator: str) -> None:
    after = _PY.replace("    def test_tax", f"    {decorator}\n    def test_tax")
    assert _edited("tests/test_cart.py", _PY, after) == [
        IntegrityFinding("tests/test_cart.py", "test_tax", "skip_marker")
    ]


def test_pytest_skip_call_is_flagged_on_the_enclosing_test() -> None:
    after = _PY.replace("assert tax(10) == 2", "pytest.skip('later')\n        assert tax(10) == 2")
    assert _edited("tests/test_cart.py", _PY, after) == [
        IntegrityFinding("tests/test_cart.py", "test_tax", "skip_marker")
    ]


def test_module_level_pytestmark_is_flagged_for_the_whole_file() -> None:
    after = _PY.replace("import pytest\n", "import pytest\n\npytestmark = pytest.mark.skip\n")
    findings = _edited("tests/test_cart.py", _PY, after)
    assert findings == [IntegrityFinding("tests/test_cart.py", "", "skip_marker")]
    assert findings[0].label() == "tests/test_cart.py · (whole file) · skip_marker"


def test_reformatting_an_existing_marker_is_not_flagged() -> None:
    """Quote style, line breaks and trailing commas are layout, not a new marker."""
    one_line = "@pytest.mark.skipif(sys.platform == 'win32', reason='x')\ndef test_empty()"
    wrapped = (
        '@pytest.mark.skipif(\n    sys.platform == "win32",\n    reason="x",\n)\ndef test_empty()'
    )
    before = _PY.replace("def test_empty()", one_line)
    assert _edited("tests/test_cart.py", before, _PY.replace("def test_empty()", wrapped)) == []


def test_marker_moved_with_its_test_to_another_file_is_not_flagged() -> None:
    skipped = "import pytest\n\n\n@pytest.mark.skip\ndef test_old() -> None:\n    pass\n"
    changes = [
        FileChange("M", "tests/test_a.py", "tests/test_a.py", skipped, "import pytest\n"),
        FileChange("M", "tests/test_b.py", "tests/test_b.py", "", skipped),
    ]
    assert integrity_findings(changes) == []


def test_test_moved_to_a_new_file_and_skipped_there_is_flagged() -> None:
    """A new file cannot launder a skip: a moved test that gets a marker is still flagged."""
    moved = "import pytest\n\n\n@pytest.mark.skip\nasync def test_async() -> None:\n    pass\n"
    changes = [
        FileChange("M", "tests/test_cart.py", "tests/test_cart.py", _PY, _WITHOUT_ASYNC_TEST),
        FileChange("A", None, "tests/test_fetch.py", None, moved),
    ]
    assert integrity_findings(changes) == [
        IntegrityFinding("tests/test_fetch.py", "test_async", "skip_marker")
    ]


def test_class_renamed_out_of_collection_removes_its_tests() -> None:
    """pytest collects only ``Test*`` classes (or TestCase subclasses): renaming one away hides
    its tests just like deleting them."""
    renamed = _PY.replace("class TestCart:", "class Cart:")
    assert _edited("tests/test_cart.py", _PY, renamed) == [
        IntegrityFinding("tests/test_cart.py", "TestCart.test_total", "removed_test"),
        IntegrityFinding("tests/test_cart.py", "TestCart.test_tax", "removed_test"),
    ]
    subclass = _PY.replace("class TestCart:", "class CartChecks(unittest.TestCase):")
    assert _edited("tests/test_cart.py", _PY, subclass) == []  # still collected, by unittest


def test_collect_ignore_in_a_conftest_is_flagged() -> None:
    before = "import pytest\n"
    after = "import pytest\n\ncollect_ignore = ['tests/test_cart.py']\n"
    assert _edited("conftest.py", before, after) == [
        IntegrityFinding("conftest.py", "", "skip_marker")
    ]


def test_dot_only_in_a_python_file_is_not_a_marker() -> None:
    """Markers are language-scoped: ``.only(`` focuses JS tests, it means nothing in Python."""
    assert _edited("tests/test_cart.py", _PY, _PY + "\nrows = query.only('id')\n") == []


# --- integrity_findings: JavaScript / TypeScript ----------------------------

_JS = """import { describe, it, test, expect } from 'vitest'

describe('cart', () => {
  it(
    'adds items',
    async () => {
      expect(add(1, 2)).toBe(3) // it.skip('not a marker in a comment')
    },
  )

  it.each([
    [1, 2],
  ])('sums %i and %i', (a, b) => {
    expect(a + b).toBeGreaterThan(0)
  })

  test.each`
    a    | b
    ${1} | ${2}
  `('table $a', ({ a }) => {
    expect(a).toBe(1)
  })

  it("renders", () => {
    render(<p>Don't panic :)</p>)
    expect(screen.getByText(/it's/)).toBeTruthy()
    expect(label).toBe('it.only(')
  })
})
"""

_MULTILINE_IT = (
    "  it(\n    'adds items',\n    async () => {\n"
    "      expect(add(1, 2)).toBe(3) // it.skip('not a marker in a comment')\n    },\n  )\n"
)
_EACH_IT = (
    "  it.each([\n    [1, 2],\n  ])('sums %i and %i', (a, b) => {\n"
    "    expect(a + b).toBeGreaterThan(0)\n  })\n"
)
_TABLE_TEST = (
    "  test.each`\n    a    | b\n    ${1} | ${2}\n  `('table $a', ({ a }) => {\n"
    "    expect(a).toBe(1)\n  })\n"
)


def test_deleted_js_test_file_is_flagged() -> None:
    assert integrity_findings([FileChange("D", "web/Cart.test.js", None)]) == [
        IntegrityFinding("web/Cart.test.js", "", "deleted_file")
    ]


@pytest.mark.parametrize(
    ("block", "test"),
    [
        (_MULTILINE_IT, "cart > adds items"),
        (_EACH_IT, "cart > sums %i and %i"),
        (_TABLE_TEST, "cart > table $a"),
    ],
)
def test_removed_multiline_and_each_js_tests_are_flagged(block: str, test: str) -> None:
    assert block in _JS
    assert _edited("web/Cart.test.js", _JS, _JS.replace(block, "")) == [
        IntegrityFinding("web/Cart.test.js", test, "removed_test")
    ]


@pytest.mark.parametrize(
    ("old", "new", "test"),
    [
        ('it("renders"', 'it.skip("renders"', "renders"),
        ('it("renders"', 'it.only("renders"', "renders"),
        ('it("renders"', 'xit("renders"', "renders"),
        ('it("renders", () => {', 'it("renders", { skip: true }, () => {', "renders"),
        ("describe('cart'", "describe.skip('cart'", "cart"),
        ("describe('cart'", "xdescribe('cart'", "cart"),
        ("it.each([", "it.skip.each([", "sums %i and %i"),
    ],
)
def test_added_js_skip_and_focus_markers_are_flagged(old: str, new: str, test: str) -> None:
    """Turning ``it(`` into ``it.skip(`` keeps the test, so it is a marker, not a removal."""
    assert old in _JS
    assert _edited("web/Cart.test.js", _JS, _JS.replace(old, new, 1)) == [
        IntegrityFinding("web/Cart.test.js", test, "skip_marker")
    ]


def test_generic_dot_only_is_flagged() -> None:
    before = "fixture('checkout')\ntest('pays', async (t) => {})\n"
    after = before.replace("fixture(", "fixture.only(")
    assert _edited("e2e/checkout.test.js", before, after) == [
        IntegrityFinding("e2e/checkout.test.js", "", "skip_marker")
    ]


def test_runtime_skip_inside_a_test_is_flagged_on_that_test() -> None:
    before = "test('logs in', async ({ page }) => {\n  await page.goto('/')\n})\n"
    after = before.replace("{ page }) => {\n", "{ page }) => {\n  test.skip(isWebkit, 'nope')\n")
    assert _edited("e2e/login.spec.ts", before, after) == [
        IntegrityFinding("e2e/login.spec.ts", "logs in", "skip_marker")
    ]


def test_js_reformatting_comments_and_strings_are_not_flagged() -> None:
    reformatted = _JS.replace("'adds items'", '"adds items"').replace("[1, 2],", "[1, 2]")
    assert _edited("web/Cart.test.js", _JS, reformatted) == []
    comment = "// it.skip('x') describe.only('y')\ndescribe('cart'"
    assert _edited("web/Cart.test.js", _JS, _JS.replace("describe('cart'", comment)) == []


def test_js_test_moved_between_files_is_not_flagged() -> None:
    before = "it('a', () => {})\nit('b', () => {})\n"
    changes = [
        FileChange("M", "web/a.test.ts", "web/a.test.ts", before, "it('a', () => {})\n"),
        FileChange("A", None, "web/b.test.ts", None, "it('b', () => {})\n"),
    ]
    assert integrity_findings(changes) == []


# --- inputs larger than the prompt limit ------------------------------------


def test_removed_test_buried_in_a_huge_file_is_found_whole() -> None:
    """A test removed from the middle of a > 200k-char file: the whole content is compared."""
    body = "".join(f"def test_case_{i:05d}() -> None:\n    assert {i}\n\n\n" for i in range(9000))
    buried = "def test_case_04500() -> None:\n    assert 4500\n\n\n"
    limit = ContextSettings().bulk_max_chars
    assert len(body) > limit and buried in body
    fitted = fit_block(body, limit=limit, field="bulk_max_chars", how_to_get_rest="-")
    assert fitted.truncated and buried not in fitted.text

    assert _edited("tests/test_big.py", body, body.replace(buried, "")) == [
        IntegrityFinding("tests/test_big.py", "test_case_04500", "removed_test")
    ]


def test_raw_listing_larger_than_the_prompt_limit_is_parsed_whole() -> None:
    """``git diff --raw -z``: a test deletion buried among thousands of records is still read."""
    blob, zero = "a" * 40, "0" * 40
    churn = [f":100644 100644 {blob} {blob} M\0src/module_{i:05d}.py\0" for i in range(4000)]
    buried = f":100644 000000 {blob} {zero} D\0tests/test_buried.py\0"
    raw = "".join(churn[:2000]) + buried + "".join(churn[2000:])
    assert len(raw) > ContextSettings().bulk_max_chars

    entries = _parse_raw_diff(raw)
    assert len(entries) == 4001
    deleted = [(e.old, e.new, e.old_blob, e.new_blob) for e in entries if e.status == "D"]
    assert deleted == [("tests/test_buried.py", None, blob, None)]


def test_raw_listing_renames_modes_and_a_leading_git_warning() -> None:
    """Renames carry two paths; symlinks get no blob; a merged stderr warning is skipped."""
    blob = "b" * 40
    raw = (
        "warning: inexact rename detection was skipped\n"
        f":100644 100644 {blob} {blob} R087\0tests/test_a.py\0tests/unit/test_a.py\0"
        f":120000 120000 {blob} {blob} M\0tests/link.py\0"
    )
    entries = _parse_raw_diff(raw)
    assert [(e.status, e.old, e.new) for e in entries] == [
        ("R", "tests/test_a.py", "tests/unit/test_a.py"),
        ("M", "tests/link.py", "tests/link.py"),
    ]
    assert entries[1].old_blob is None and entries[1].new_blob is None


# --- integrity_prompt_section -------------------------------------------------


def test_prompt_section_lists_every_finding_and_the_rule() -> None:
    findings = [
        IntegrityFinding("tests/test_a.py", "", "deleted_file"),
        IntegrityFinding("web/Cart.test.js", "cart > adds items", "removed_test"),
        IntegrityFinding("tests/test_b.py", "test_tax", "skip_marker"),
    ]
    section = integrity_prompt_section(findings)
    assert "TEST INTEGRITY" in section
    assert "- tests/test_a.py · (whole file) · deleted_file" in section
    assert "- web/Cart.test.js · cart > adds items · removed_test" in section
    assert "- tests/test_b.py · test_tax · skip_marker" in section
    assert "Return fail unless the task request explicitly asks" in section


def test_prompt_section_is_empty_without_findings() -> None:
    assert integrity_prompt_section([]) == ""
