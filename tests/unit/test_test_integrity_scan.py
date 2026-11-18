"""The JS/TS focus-marker scan of the test-integrity guard stays linear in the file size.

A file with thousands of ``.only`` / ``fit(`` / ``fdescribe(`` markers used to cost one pass over
every test (and every definition) per marker. :func:`_reference_js_suite` is a verbatim copy of
that implementation: the scan must find exactly what it found, on a corpus covering each way a
marker selects the tests it switches off. Pure, no git and no LLM.
"""

from __future__ import annotations

import time

import pytest

from conclave.engine import test_integrity
from conclave.engine.test_integrity import (
    FileChange,
    IntegrityFinding,
    _js_suite,
    _JsDefinition,
    _JsScan,
    _Marker,
    _Span,
    _Suite,
    _Test,
    integrity_findings,
)


def _reference_js_suite(source: str) -> _Suite:
    """Tests (``it`` / ``test`` calls, by title) and markers of a JS/TS file."""
    scan = _JsScan(source)
    scan.scan()
    definitions = sorted(scan.definitions, key=lambda d: d.open)

    def inside(definition: _JsDefinition, index: int) -> bool:
        return definition.open < index <= definition.close

    # Bracket matching nests the calls, so one pass with a stack finds each test's suites.
    tests: list[tuple[_JsDefinition, _Test]] = []
    open_suites: list[_JsDefinition] = []
    for definition in definitions:
        while open_suites and not inside(open_suites[-1], definition.open):
            open_suites.pop()
        if definition.is_suite:
            open_suites.append(definition)
        else:
            path = (*(suite.title for suite in open_suites), definition.title)
            tests.append((definition, _Test(definition.title, path)))

    def enclosing(index: int) -> _JsDefinition | None:
        holders = [d for d in definitions if inside(d, index)]
        return max(holders, key=lambda d: d.open) if holders else None

    def covered(scope: _JsDefinition | None, focus: bool) -> frozenset[str]:
        if focus:  # a focused test or suite switches off every other test of the file
            others = frozenset(
                t.name for d, t in tests
                if scope is None or not (d is scope or inside(scope, d.open))
            )
            if others:
                return others
        if scope is not None and not scope.is_suite:
            return frozenset((scope.title,))
        return frozenset(t.name for d, t in tests if scope is None or inside(scope, d.open))

    markers: list[_Marker] = []
    for mark in sorted(scan.marks, key=lambda m: m.index):
        scope = mark.definition or enclosing(mark.index)
        markers.append(_Marker(scope.title if scope else "", mark.text, covered(scope, mark.focus)))
    return _Suite(" > ", [test for _, test in tests], markers)


# --- scale ----------------------------------------------------------------------

_MARKERS = 20_000
_MARKER_LINES = ("fit('f{i}',f)", "it.only('o{i}',f)", "fdescribe('d{i}',f)", "x.only()")


def test_twenty_thousand_focus_markers_scan_in_linear_time() -> None:
    before = "it('a', f)\nit('b', f)\nit('c', f)\n"
    after = before + "".join(
        _MARKER_LINES[i % len(_MARKER_LINES)].format(i=i) + "\n" for i in range(_MARKERS)
    )
    changes = [FileChange("M", "app.test.js", "app.test.js", before, after)]

    started = time.perf_counter()
    findings = integrity_findings(changes)
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0, f"{_MARKERS} markers took {elapsed:.2f}s"
    # One finding per focused test or suite (5,000 of each kind) and one for the bare .only().
    assert len(findings) == 3 * _MARKERS // 4 + 1
    for target in ("f0", "o1", "d2", "", f"d{_MARKERS - 2}"):
        assert IntegrityFinding("app.test.js", target, "skip_marker") in findings
    assert all(finding.kind == "skip_marker" for finding in findings)


# --- the same findings as the reference -----------------------------------------

_PRE_EXISTING = """\
describe('cart', () => {
  it('adds an item', () => {});
  it('removes an item', () => {});
  describe('totals', () => {
    it('sums prices', () => {});
    it('applies a discount', () => {});
  });
});
it('dup', () => {});
it('checks out', () => {});
it.each([1, 2])('rounds %i', (n) => {});
"""

# The pre-existing file after its tests moved out (only 'checks out' stays).
_KEPT = "it('checks out', () => {});\n"

_NEW_FILES = {
    "focus-test-with-others": """\
it('adds an item', () => {});
fit('brand new', () => {});
""",
    "focus-test-without-moved-others": """\
fit('adds an item', () => {});
it('brand new', () => {});
""",
    "focus-on-the-only-test": "it.only('removes an item', () => {});\n",
    "focus-suite-with-others": """\
fdescribe('focused', () => {
  it('fresh one', () => {});
});
it('sums prices', () => {});
""",
    "focus-suite-holding-the-moved": """\
fdescribe('focused', () => {
  it('sums prices', () => {});
});
it('fresh one', () => {});
""",
    "focus-on-the-only-suite": """\
describe.only('totals', () => {
  it('sums prices', () => {});
  it('applies a discount', () => {});
});
""",
    "nested-skips": """\
describe('outer', () => {
  describe.skip('skipped suite', () => {
    it('sums prices', () => {});
  });
  xdescribe('x suite', () => {
    it('fresh one', () => {});
  });
  describe('inner', () => {
    it.skip('applies a discount', () => {});
    it('fresh two', () => {});
  });
});
""",
    "skip-only-new-tests": """\
describe.skip('fresh suite', () => {
  it('fresh one', () => {});
});
it('adds an item', () => {});
xit('fresh two', () => {});
""",
    "bare-markers-in-tests": """\
describe('group', () => {
  it('adds an item', function () { this.skip(); });
  it('fresh one', () => { x.only(); });
});
it('fresh two', () => {});
""",
    "bare-markers-in-a-suite-body": """\
describe('group', () => {
  this.skip();
  it('removes an item', () => {});
});
it('fresh one', () => {});
""",
    "bare-only-in-a-suite-body": """\
describe('group', () => {
  x.only();
  it('fresh one', () => {});
});
it('removes an item', () => {});
""",
    "module-level-markers": """\
this.skip();
it('sums prices', () => {});
""",
    "module-level-only-without-tests": "x.only();\n",
    "options-objects": """\
it('adds an item', {skip: true}, () => {});
it('fresh one', {only: true}, () => {});
it('fresh two', {skip: false}, () => {});
test('removes an item', {todo: 'later'}, () => {});
""",
    "each-tables": """\
it.each([1, 2])('rounds %i', (n) => {});
it.only.each([3])('fresh %i', (n) => {});
test.skip.each([4])('fresh skipped %i', (n) => {});
""",
    "duplicate-titles-in-and-out-of-a-focused-suite": """\
fdescribe('focused', () => {
  it('dup', () => {});
});
it('dup', () => {});
""",
    "duplicate-titles-only-inside-a-focused-suite": """\
fdescribe('focused', () => {
  it('dup', () => {});
  it('dup', () => {});
});
it('fresh one', () => {});
""",
    "pending-and-value-markers": """\
it('adds an item');
const skipped = it.skip;
skipped('removes an item', () => {});
""",
    "unclosed-suite": """\
fdescribe('open', () => {
  it('sums prices', () => {});
""",
}


def _move(name: str, new_file: str) -> list[FileChange]:
    """Tests moved out of a pre-existing file into a new one holding *new_file*."""
    return [
        FileChange("M", "cart.test.js", "cart.test.js", _PRE_EXISTING, _KEPT),
        FileChange("A", None, f"{name}.test.js", None, new_file),
    ]


_CORPUS: dict[str, list[FileChange]] = {
    **{f"moved-into:{name}": _move(name, source) for name, source in _NEW_FILES.items()},
    **{
        f"edited:{name}": [
            FileChange("M", "cart.test.js", "cart.test.js", _PRE_EXISTING, _PRE_EXISTING + source)
        ]
        for name, source in _NEW_FILES.items()
    },
    "new-file-moving-nothing": [
        FileChange("A", None, "fresh.test.js", None, _NEW_FILES["focus-test-with-others"])
    ],
    "two-new-files": [
        FileChange("M", "cart.test.js", "cart.test.js", _PRE_EXISTING, _KEPT),
        FileChange("A", None, "a.test.js", None, _NEW_FILES["focus-suite-with-others"]),
        FileChange("A", None, "b.test.ts", None, _NEW_FILES["nested-skips"]),
    ],
}


@pytest.mark.parametrize("changes", list(_CORPUS.values()), ids=list(_CORPUS))
def test_findings_match_the_reference_scan(
    changes: list[FileChange], monkeypatch: pytest.MonkeyPatch
) -> None:
    findings = integrity_findings(changes)
    with monkeypatch.context() as patched:
        patched.setattr(test_integrity, "_js_suite", _reference_js_suite)
        expected = integrity_findings(changes)
    assert findings == expected


def test_the_corpus_exercises_both_outcomes_of_a_new_files_markers() -> None:
    """Moved tests are switched off in some new files and left running in others."""
    flagged = {
        name
        for name, source in _NEW_FILES.items()
        if any(f.file == f"{name}.test.js" for f in integrity_findings(_move(name, source)))
    }
    assert {
        "focus-test-with-others",
        "focus-on-the-only-test",
        "focus-suite-with-others",
        "focus-on-the-only-suite",
        "nested-skips",
        "duplicate-titles-in-and-out-of-a-focused-suite",
        "module-level-markers",
    } <= flagged
    assert {
        "focus-suite-holding-the-moved",
        "skip-only-new-tests",
        "duplicate-titles-only-inside-a-focused-suite",
        "module-level-only-without-tests",
    }.isdisjoint(flagged)


def _covered_names(suite: _Suite, marker: _Marker) -> frozenset[str]:
    covers = marker.covers
    if not isinstance(covers, _Span):
        return covers
    names = [test.name for test in suite.tests]
    if covers.outside:
        return frozenset(names[: covers.lo] + names[covers.hi :])
    return frozenset(names[covers.lo : covers.hi])


_SOURCES = {
    "pre-existing": _PRE_EXISTING,
    **{f"alone:{name}": source for name, source in _NEW_FILES.items()},
    **{f"appended:{name}": _PRE_EXISTING + source for name, source in _NEW_FILES.items()},
}


@pytest.mark.parametrize("source", list(_SOURCES.values()), ids=list(_SOURCES))
def test_each_marker_covers_the_tests_the_reference_says(source: str) -> None:
    suite = _js_suite(source)
    reference = _reference_js_suite(source)
    assert suite.tests == reference.tests
    assert [(m.target, m.text, _covered_names(suite, m)) for m in suite.markers] == [
        (m.target, m.text, m.covers) for m in reference.markers
    ]
