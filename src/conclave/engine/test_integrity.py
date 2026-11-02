"""Test-integrity guard — which PRE-EXISTING tests a change deleted, removed or switched off.

A task EARNS a green gate by making the existing suite pass. The cheap way to fake that green is to
take tests away: delete a failing test file, remove the failing test, or mark it skipped. After each
developer attempt the orchestrator asks :func:`collect_integrity_findings` what the change, compared
with the task's base checkpoint, did to the tests that were already there. Three kinds of finding:

* ``deleted_file`` — a pre-existing test file was deleted, or renamed to a path that is no longer a
  test path (which silently drops it from collection);
* ``removed_test`` — a test was removed from a pre-existing test file and is not defined again in
  any test file of the change, so a moved test is not flagged. A test is a Python ``def test*`` at
  module level or in a collected class (``Test*``, or one with bases such as a ``TestCase``
  subclass, so renaming a class out of collection removes its tests) or a JS/TS ``it`` / ``test``
  call, named by its title, in any layout: one line or many, plain or ``.each``;
* ``skip_marker`` — a skip, expected-failure, todo or focus marker was added to a pre-existing test
  file. Python: ``@pytest.mark.skip`` / ``skipif`` / ``xfail``, ``pytest.skip(`` / ``xfail(`` /
  ``importorskip(``, ``unittest.skip*`` / ``expectedFailure``, ``.skipTest(``, ``SkipTest``,
  ``collect_ignore``, and importing a skip function by name (``from pytest import skip``).
  JS/TS: ``it`` / ``test`` / ``describe`` (and mocha's ``specify`` / ``context`` / ``suite``) with
  ``.skip`` / ``.only`` / ``.todo`` / ``.failing`` / ``.fails`` / ``.fail`` / ``.fixme`` /
  ``.skipIf`` / ``.runIf``, called or used as a value; the ``x`` / ``f`` aliases (``xit(``,
  ``xdescribe(``, ``fit(`` ...); any ``.only(``; ``this.skip(``; a ``{skip | todo | only | fails:
  ...}`` options object; and a test whose callback was dropped (mocha runs it as pending). A
  marker added in a NEW file is flagged only when it switches off a test moved there from a
  pre-existing file, or when the file is a ``conftest.py`` (whose markers reach other files).

Plain edits (a changed assertion, a new test, a brand-new test file) take no coverage away and are
not flagged. Files are compared WHOLE against whole, never hunk by hunk: a definition or marker
split over several lines, reformatted, re-indented or moved is matched by what it is, not by where
its lines landed in a diff. The guard reads code, it does not run it: tests switched off by control
flow or by test configuration are left to the reviewers, who see the whole diff.

SECURITY — why the input comes from git in the worktree and NOT from the reviewer diff: the diff
placed in reviewer prompts is fitted to a per-block character limit (``context.bulk_max_chars``, or
``context.compaction_max_chars`` when compaction is on; see :mod:`conclave.engine.context_limits`)
by keeping only its head and tail, so a deletion whose ``diff --git`` header lands in the omitted
middle is INVISIBLE to anything reading that text. :func:`collect_integrity_findings` asks git for
the file list (``--name-status`` / ``--raw``: one short record per file, so its size is bounded by
the file COUNT, not by how large the edits are) and reads each test file's two versions whole.
Nothing here truncates: every parser scans its WHOLE input.

The split mirrors the rest of the bug-fixer surface (pure parsers + one thin async entry, as in
:mod:`conclave.engine.repro`): :func:`modified_or_deleted_tests` classifies a ``--name-status``
listing, :func:`integrity_findings` compares the before/after contents of a change's test files,
and :func:`collect_integrity_findings` sources both from git. All of it is deterministically
unit-testable on crafted input, including inputs larger than the prompt limit.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from .gitio import run_git

logger = logging.getLogger("conclave.engine.test_integrity")

# JS/TS module extensions: plain, ES module and CommonJS, each with or without JSX.
_JS_EXTENSIONS = frozenset((".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"))
# Directories whose every file belongs to the suite: pytest's ``tests/``, mocha's default ``test/``
# and jest's ``__tests__/``.
_TEST_DIRS = frozenset(("tests", "test", "__tests__"))


def is_test_path(path: str) -> bool:
    """Heuristically decide whether ``path`` names a test file.

    Matches the conventions a task could weaken to fake a green gate: ``test_*.py`` /
    ``*_test.py``, ``tests.py`` and ``conftest.py`` (pytest, unittest, Django); ``*.test.*`` /
    ``*.spec.*`` (and a bare ``test.*`` / ``spec.*``) and cypress's ``*.cy.*`` for every JS/TS
    extension (``.js .jsx .mjs .cjs .ts .tsx .mts .cts``: jest, vitest, mocha, playwright); and
    ANY file under a ``tests/``, ``test/`` or ``__tests__/`` directory (shared fixtures count —
    gutting one weakens every test that imports it). Paths are parsed as POSIX because ``git``
    always reports forward slashes.
    """
    pure = PurePosixPath(path)
    name = pure.name
    if not name:
        return False
    if name.endswith(".py") and (
        name.startswith("test_")
        or name.endswith("_test.py")
        or name in ("tests.py", "conftest.py")
    ):
        return True
    if pure.suffix in _JS_EXTENSIONS:
        stem = name[: -len(pure.suffix)]
        if stem in ("test", "spec") or stem.endswith((".test", ".spec", ".cy")):
            return True
    # "under a test directory" — a DIRECTORY component (the basename is excluded, so a file merely
    # named ``tests`` does not qualify) is exactly one of the names. Exact-component match, so
    # ``my_tests/`` and ``integration_tests/`` deliberately do not count.
    return not _TEST_DIRS.isdisjoint(pure.parts[:-1])


def modified_or_deleted_tests(name_status: str) -> set[str]:
    """The pre-existing test files MODIFIED or DELETED in ``git diff --name-status`` output.

    ``name_status`` is the raw stdout of ``git diff --name-status <checkpoint>`` — one
    ``<status>\\t<path>`` record per line (renames/copies carry ``<status>\\t<old>\\t<new>``).
    Returns the set of test paths the task changed in a way that REMOVES or ALTERS prior coverage:

    * ``M`` / ``T`` (modified / type-changed) and ``D`` (deleted) test files are reported.
    * ``A`` (added) test files are clean — a brand-new test takes nothing away.
    * ``R`` (renamed): the source path is reported only when the rename moves a test OUT of
      test-hood (the destination is no longer a test path), which silently drops it from
      collection. A test → test rename is a benign relocation. When git instead splits a rename
      into ``D`` + ``A`` records, those rules already cover it, so both representations agree.
    * ``C`` (copied) leaves the source intact, so it removes no coverage and is ignored.

    The WHOLE listing is scanned and nothing here truncates, so a deletion that the fitted reviewer
    diff omits is still found. Unrecognized status codes are ignored rather than guessed at.
    """
    touched: set[str] = set()
    for line in name_status.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) < 2:
            # A status code with no path — a malformed/empty record; nothing to classify.
            continue
        # Leading letter only: ``R100`` / ``C75`` carry a similarity score after the code letter.
        code = fields[0][:1]
        if code in ("M", "T", "D"):
            if is_test_path(fields[1]):
                touched.add(fields[1])
        elif code == "R":
            old = fields[1]
            new = fields[2] if len(fields) >= 3 else ""
            # A rename that keeps the file a collectable test preserves coverage; one that moves a
            # test to a non-test path silently removes it from the suite — flag the vanished source.
            if is_test_path(old) and not is_test_path(new):
                touched.add(old)
        # 'A' (added) and 'C' (copied) remove no existing coverage; any other code is ignored.
    return touched


IntegrityKind = Literal["deleted_file", "removed_test", "skip_marker"]


@dataclass(frozen=True)
class IntegrityFinding:
    """One way a change took pre-existing test coverage away.

    ``test`` names the test (Python ``Class.test_name``, JS ``describe title > test title``), the
    class or ``describe`` a marker was put on, or is ``""`` for the whole file.
    """

    file: str
    test: str
    kind: IntegrityKind

    def label(self) -> str:
        """``file · test · kind``, the form reviewers and the task summary show."""
        return f"{self.file} · {self.test or '(whole file)'} · {self.kind}"


@dataclass(frozen=True)
class FileChange:
    """One file of a change, as ``git diff -M`` against the base checkpoint reports it.

    ``status`` is git's letter (``A`` added, ``C`` copied, ``D`` deleted, ``M`` modified, ``R``
    renamed, ``T`` type changed); ``old`` / ``new`` are the paths and ``before`` / ``after`` the
    contents on each side, ``None`` where a side is absent or was not read.
    """

    status: str
    old: str | None
    new: str | None
    before: str | None = None
    after: str | None = None


# --- one file's tests and markers ---------------------------------------------


@dataclass(frozen=True)
class _Test:
    name: str  # what a moved test is matched by: the function name, or the JS title
    path: tuple[str, ...]  # its enclosing classes / describe titles, then its name


@dataclass(frozen=True)
class _Marker:
    target: str  # the test, class or describe it is put on; "" for the module
    text: str  # its normalized source: what a moved, kept or edited marker is matched by
    covers: frozenset[str]  # names of the tests it switches off


@dataclass
class _Suite:
    separator: str  # joins a test's path for the report
    tests: list[_Test] = field(default_factory=list)
    markers: list[_Marker] = field(default_factory=list)


def _parsed(path: str | None) -> bool:
    """Whether the tests in the file at *path* can be read (Python or JS/TS, by extension)."""
    return path is not None and PurePosixPath(path).suffix in (_JS_EXTENSIONS | {".py"})


def _suite(path: str, content: str | None) -> _Suite:
    """The tests and markers of one version of a file, by its language (none if unknown)."""
    suffix = PurePosixPath(path).suffix
    if suffix == ".py":
        return _python_suite(content or "")
    if suffix in _JS_EXTENSIONS:
        return _js_suite(content or "")
    return _Suite(" ")


# --- Python -------------------------------------------------------------------


@dataclass(frozen=True)
class _Line:
    """One logical Python line (continuations joined): its indentation, its code with every string
    literal emptied (so nothing quoted can match), and a key that keeps the strings but drops
    whitespace and trailing commas, so reformatting a line leaves its key unchanged."""

    indent: int
    code: str
    key: str


def _python_string(source: str, start: int) -> tuple[int, str]:
    """End (exclusive) and content of the string literal opening at *start*.

    A single-quoted string ends with its line when unterminated; a triple-quoted one may span lines.
    """
    quote = source[start]
    delimiter = quote * 3 if source.startswith(quote * 3, start) else quote
    i = start + len(delimiter)
    while i < len(source):
        if source[i] == "\\":
            i += 2
            continue
        if source.startswith(delimiter, i):
            return i + len(delimiter), source[start + len(delimiter) : i]
        if source[i] == "\n" and len(delimiter) == 1:
            break
        i += 1
    end = min(i, len(source))
    return end, source[start + len(delimiter) : end]


def _python_lines(source: str) -> list[_Line]:
    """Split *source* into logical lines; blank and comment-only lines are dropped."""
    source = source.removeprefix("\ufeff")
    lines: list[_Line] = []
    code: list[str] = []
    key: list[str] = []
    indent = -1  # -1 until the current logical line's indentation is measured
    depth = 0
    i, n = 0, len(source)
    while i < n:
        if indent < 0:
            start = i
            while i < n and source[i] in " \t\f":
                i += 1
            if i < n and source[i] in "#\r\n":
                newline = source.find("\n", i)
                i = n if newline < 0 else newline + 1
            elif i < n:
                indent = len(source[start:i].expandtabs())
            continue
        char = source[i]
        if char in "'\"":
            i, content = _python_string(source, i)
            code.append('""')
            key.append(f'"{content}"')
        elif char == "#":
            newline = source.find("\n", i)
            i = n if newline < 0 else newline
        elif char == "\\" and source.startswith(("\n", "\r\n"), i + 1):
            i = source.index("\n", i) + 1  # an explicit line continuation
            code.append(" ")
        elif char == "\n" and depth == 0:
            lines.append(_Line(indent, "".join(code), "".join(key)))
            code, key, indent = [], [], -1
            i += 1
        elif char.isspace():
            code.append(" ")
            i += 1
        else:
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth = max(0, depth - 1)
                if key and key[-1] == ",":
                    key.pop()  # a trailing comma is formatting, not meaning
            code.append(char)
            key.append(char)
            i += 1
    if code:
        lines.append(_Line(indent, "".join(code), "".join(key)))
    return lines


_PY_DEF = re.compile(r"(?:async\s+)?def\s+(\w+)")
_PY_CLASS = re.compile(r"class\s+(\w+)\s*(?:\((?P<bases>[^)]*)\))?")
_PY_MARKER = re.compile(
    r"\bmark\s*\.\s*(?:skipif|skip|xfail)\b"
    r"|\bpytest\s*\.\s*(?:skip|xfail|importorskip)\b"
    r"|\bunittest\s*\.\s*(?:skip\w*|expectedFailure)\b"
    r"|\.\s*skipTest\s*\("
    r"|\bSkipTest\b"
    r"|\bcollect_ignore(?:_glob)?\b"  # conftest.py: files pytest must not collect
    # Importing a skip function by name, so its calls need no ``pytest.`` / ``unittest.`` prefix.
    r"|\bfrom\s+(?:pytest|unittest)\b[\w.]*\s+import\b.*?"
    r"\b(?:skip\w*|xfail|importorskip|expectedFailure)\b"
)
# ``from unittest import skip`` and friends, recognised as decorators only.
_PY_MARKER_DECORATOR = re.compile(r"@\s*(?:skip|skipIf|skipUnless|expectedFailure)\b")


@dataclass(frozen=True)
class _Scope:
    """An open ``def`` or ``class`` block; *collects* marks a class whose ``test*`` methods run."""

    indent: int
    path: tuple[str, ...]
    is_class: bool
    is_test: bool
    collects: bool = False


def _python_marker_scope(stack: Sequence[_Scope]) -> tuple[str, tuple[str, ...]]:
    """The target and the covered scope of a marker statement inside the open blocks *stack*.

    The outermost function it is in names it: a test, or a helper that can skip any test beside it.
    Outside functions its innermost class names it, else the module.
    """
    function = next((scope for scope in stack if not scope.is_class), None)
    if function is not None:
        return function.path[-1], function.path if function.is_test else function.path[:-1]
    if stack:
        return stack[-1].path[-1], stack[-1].path
    return "", ()


def _python_suite(source: str) -> _Suite:
    """Tests (module functions and class methods named ``test*``) and markers of a Python file."""
    tests: list[_Test] = []
    found: list[tuple[str, str, tuple[str, ...]]] = []  # (target, text, scope it covers)
    stack: list[_Scope] = []
    decorators: list[str] = []  # keys of the marker decorators awaiting their def / class
    for line in _python_lines(source):
        while stack and stack[-1].indent >= line.indent:
            stack.pop()
        code = line.code
        if code.startswith("@"):
            if _PY_MARKER.search(code) or _PY_MARKER_DECORATOR.match(code):
                decorators.append(line.key)
            continue
        header = _PY_DEF.match(code) or _PY_CLASS.match(code)
        if header is None:
            decorators.clear()
            if _PY_MARKER.search(code):
                target, scope = _python_marker_scope(stack)
                found.append((target, line.key, scope))
            continue
        name = header.group(1)
        is_class = header.re is _PY_CLASS
        parent = stack[-1] if stack else None
        path = (*(parent.path if parent else ()), name)
        is_test = not is_class and name.startswith("test") and (parent is None or parent.collects)
        # pytest collects ``Test*`` classes, unittest any TestCase subclass (a class with bases),
        # so renaming ``class TestCart:`` to ``class Cart:`` removes its tests.
        bases = (header.groupdict().get("bases") or "").strip()
        collects = (
            is_class
            and (name.startswith("Test") or bases not in ("", "object"))
            and (parent is None or parent.collects)
        )
        if is_test:
            tests.append(_Test(name, path))
        found.extend((name, text, path) for text in decorators)
        decorators.clear()
        stack.append(_Scope(line.indent, path, is_class, is_test, collects))
        # A one-line body (``def test_x(): pytest.skip()``) sits on the header line.
        if _PY_MARKER.search(code, header.end()):
            target, scope = _python_marker_scope(stack)
            found.append((target, line.key, scope))
    under: dict[tuple[str, ...], set[str]] = {}  # every scope → the names of the tests in it
    for test in tests:
        for depth in range(len(test.path) + 1):
            under.setdefault(test.path[:depth], set()).add(test.name)
    markers = [
        _Marker(target, text, frozenset(under.get(scope, ()))) for target, text, scope in found
    ]
    return _Suite(".", tests, markers)


# --- JavaScript / TypeScript --------------------------------------------------


@dataclass(frozen=True)
class _Token:
    kind: Literal["id", "str", "tpl", "num", "re", "p"]  # p: punctuation
    value: str  # the identifier, number or punctuation; a string or template literal's content


# Keywords after which a ``/`` starts a regex literal rather than a division.
_JS_REGEX_AFTER = frozenset(
    ("return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case", "do",
     "else", "yield", "await")
)
_JS_OPENERS = {"(": ")", "[": "]", "{": "}"}
_JS_CLOSERS = frozenset(_JS_OPENERS.values())


def _js_line_literal_end(source: str, start: int, quote: str) -> int:
    """End (exclusive) of the string or regex literal opening at *start*, or -1 when its line ends
    first: JS strings and regex literals cannot span lines, so an apostrophe in JSX text is not a
    string."""
    in_class = False
    i = start + 1
    while i < len(source):
        char = source[i]
        if char == "\\":
            i += 2
            continue
        if char == "\n":
            return -1
        if quote != "/":
            if char == quote:
                return i + 1
        elif char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            return i + 1
        i += 1
    return -1


def _js_comment_end(source: str, start: int) -> int:
    """End (exclusive) of the ``//`` or ``/* */`` comment at *start*; *start* if there is none."""
    if source.startswith("//", start):
        newline = source.find("\n", start)
        return len(source) if newline < 0 else newline
    if source.startswith("/*", start):
        close = source.find("*/", start + 2)
        return len(source) if close < 0 else close + 2
    return start


def _js_template_end(source: str, start: int) -> int:
    """End (exclusive) of the template literal opening at *start*, nested ``${...}`` included.

    Iterative (a stack instead of recursion), so no nesting depth can exhaust the call stack.
    """
    stack = [0]  # 0: inside template text; >0: brace depth inside a ``${...}`` expression
    i = start + 1
    while i < len(source):
        char = source[i]
        if stack[-1] == 0:
            if char == "\\":
                i += 2
                continue
            if char == "`":
                stack.pop()
                if not stack:
                    return i + 1
            elif source.startswith("${", i):
                stack.append(1)
                i += 1
            i += 1
            continue
        comment_end = _js_comment_end(source, i)
        if comment_end > i:
            i = comment_end
        elif char in "'\"":
            end = _js_line_literal_end(source, i, char)
            i = i + 1 if end < 0 else end
        elif char == "`":
            stack.append(0)
            i += 1
        else:
            if char == "{":
                stack[-1] += 1
            elif char == "}":
                stack[-1] -= 1
                if stack[-1] == 0:
                    stack.pop()  # back in the enclosing template's text
            i += 1
    return len(source)


def _js_regex_allowed(tokens: Sequence[_Token]) -> bool:
    """Whether a ``/`` after *tokens* starts a regex literal rather than a division."""
    if not tokens:
        return True
    last = tokens[-1]
    if last.kind == "p":
        return last.value not in _JS_CLOSERS
    return last.kind == "id" and last.value in _JS_REGEX_AFTER


def _js_tokens(source: str) -> list[_Token]:
    """Tokenize JS/TS: comments dropped, string, template and regex literals kept whole."""
    tokens: list[_Token] = []
    i, n = 0, len(source)
    while i < n:
        char = source[i]
        comment_end = _js_comment_end(source, i)
        if comment_end > i:
            i = comment_end
        elif char.isspace() or char == "\ufeff":
            i += 1
        elif char in "'\"" or (char == "/" and _js_regex_allowed(tokens)):
            end = _js_line_literal_end(source, i, char)
            if end < 0:
                tokens.append(_Token("p", char))
                i += 1
            elif char == "/":
                while end < n and (source[end].isalnum() or source[end] in "_$"):
                    end += 1  # regex flags
                tokens.append(_Token("re", source[i:end]))
                i = end
            else:
                tokens.append(_Token("str", source[i + 1 : end - 1]))
                i = end
        elif char == "`":
            end = _js_template_end(source, i)
            tokens.append(_Token("tpl", source[i + 1 : max(i + 1, end - 1)]))
            i = end
        elif char.isalnum() or char in "_$" or ord(char) > 127:
            start = i
            while i < n and (source[i].isalnum() or source[i] in "_$" or ord(source[i]) > 127):
                i += 1
            kind: Literal["id", "num"] = "num" if char.isdigit() else "id"
            tokens.append(_Token(kind, source[start:i]))
        else:
            punct = next((p for p in ("...", "=>", "?.") if source.startswith(p, i)), char)
            tokens.append(_Token("p", punct))
            i += len(punct)
    return tokens


def _js_matches(tokens: Sequence[_Token]) -> dict[int, int]:
    """The index of the closing bracket of every opening one.

    A closer pops back to the nearest opener of its kind (closing any left open on the way); a
    closer with no such opener is ignored; an opener never closed closes at the end.
    """
    matches: dict[int, int] = {}
    stack: list[int] = []
    for index, token in enumerate(tokens):
        if token.kind != "p":
            continue
        if token.value in _JS_OPENERS:
            stack.append(index)
        elif token.value in _JS_CLOSERS and any(
            _JS_OPENERS[tokens[opener].value] == token.value for opener in stack
        ):
            while stack:
                opener = stack.pop()
                matches[opener] = index
                if _JS_OPENERS[tokens[opener].value] == token.value:
                    break
    for opener in stack:
        matches[opener] = len(tokens)
    return matches


def _js_text(tokens: Sequence[_Token]) -> str:
    """Normalized source of *tokens*: layout, quote style and trailing commas do not show."""
    parts: list[str] = []
    for token in tokens:
        if token.kind == "p" and token.value in _JS_CLOSERS and parts and parts[-1] == ",":
            parts.pop()
        if token.kind == "str":
            parts.append(f'"{token.value}"')
        elif token.kind == "tpl":
            parts.append(f"`{token.value}`")
        else:
            parts.append(token.value)
    return " ".join(parts)


# Test and suite functions of jest / vitest / mocha / jasmine / playwright, with the ``x`` (skip)
# and ``f`` (focus) aliases, and the modifiers a test definition may chain.
_JS_TEST_HEADS = frozenset(("it", "test", "specify", "xit", "xtest", "xspecify", "fit"))
_JS_SUITE_HEADS = frozenset(("describe", "context", "suite", "xdescribe", "xcontext", "fdescribe"))
_JS_HEADS = _JS_TEST_HEADS | _JS_SUITE_HEADS
_JS_SKIP_HEADS = frozenset(("xit", "xtest", "xspecify", "xdescribe", "xcontext"))
_JS_FOCUS_HEADS = frozenset(("fit", "fdescribe"))
_JS_SKIP_MODIFIERS = frozenset(
    ("skip", "todo", "failing", "fails", "fail", "fixme", "skipIf", "runIf")
)
# Modifiers that take arguments and return the function the test is then defined with.
_JS_CALL_MODIFIERS = frozenset(("each", "for", "skipIf", "runIf"))
_JS_MODIFIERS = _JS_SKIP_MODIFIERS | _JS_CALL_MODIFIERS | frozenset(
    ("only", "concurrent", "sequential", "serial", "parallel", "describe")
)
# Options-object keys that switch a test off (node:test, vitest): ``test(title, {skip: true}, fn)``.
_JS_OPTION_MARKERS = frozenset(("skip", "todo", "only", "fails"))


@dataclass(frozen=True)
class _JsDefinition:
    """A test or suite definition: ``it('title', fn)``, ``describe.each(t)('title', fn)``..."""

    open: int  # token index of the ``(`` of its call
    close: int  # token index of the matching ``)``
    title: str
    is_suite: bool


@dataclass(frozen=True)
class _JsMark:
    """A marker found at token *index*; *definition* is set when it marks that definition."""

    index: int
    text: str
    focus: bool
    definition: _JsDefinition | None = None


class _JsScan:
    """One pass over a JS/TS file's tokens collecting its definitions and markers."""

    def __init__(self, source: str) -> None:
        self.tokens = _js_tokens(source)
        self.matches = _js_matches(self.tokens)
        self.definitions: list[_JsDefinition] = []
        self.marks: list[_JsMark] = []
        self.chain_dots: set[int] = set()  # ``.`` tokens of parsed test chains

    def is_punct(self, index: int, *values: str) -> bool:
        return (
            0 <= index < len(self.tokens)
            and self.tokens[index].kind == "p"
            and self.tokens[index].value in values
        )

    def is_id(self, index: int, *values: str) -> bool:
        return (
            0 <= index < len(self.tokens)
            and self.tokens[index].kind == "id"
            and self.tokens[index].value in values
        )

    def arguments(self, open_index: int) -> list[tuple[int, int]]:
        """Token ranges ``[start, end)`` of the top-level arguments of the call at *open_index*."""
        close = self.matches.get(open_index, len(self.tokens))
        ranges: list[tuple[int, int]] = []
        start = i = open_index + 1
        while i < close:
            if self.is_punct(i, *_JS_OPENERS):
                i = self.matches.get(i, close) + 1
                continue
            if self.is_punct(i, ","):
                ranges.append((start, i))
                start = i + 1
            i += 1
        ranges.append((start, close))
        return [(begin, end) for begin, end in ranges if begin < end]

    def is_function(self, begin: int, end: int) -> bool:
        """Whether the argument ``[begin, end)`` is a function or an arrow function."""
        first = begin + 1 if self.is_id(begin, "async") else begin
        if self.is_id(first, "function"):
            return True
        i = begin
        while i < end:
            if self.is_punct(i, *_JS_OPENERS):
                i = self.matches.get(i, end) + 1
                continue
            if self.is_punct(i, "=>"):
                return True
            i += 1
        return False

    def scan(self) -> None:
        for index, token in enumerate(self.tokens):
            if token.kind != "id":
                continue
            if token.value in _JS_HEADS and not (
                self.is_punct(index - 1, ".", "?.") or self.is_id(index - 1, "function")
            ):
                self.test_call(index)
            elif token.value == "only" and self.is_punct(index - 1, ".", "?."):
                if index - 1 not in self.chain_dots and self.is_punct(index + 1, "("):
                    owner = self.tokens[index - 2].value if index >= 2 else ""
                    self.marks.append(_JsMark(index, f"{owner}.only", focus=True))
            elif (
                token.value == "skip"
                and self.is_punct(index - 1, ".", "?.")
                and self.is_id(index - 2, "this")
                and self.is_punct(index + 1, "(")
            ):
                arguments = self.tokens[index + 1 : self.matches.get(index + 1, index + 1) + 1]
                self.marks.append(_JsMark(index, f"this.skip{_js_text(arguments)}", focus=False))

    def type_arguments_end(self, start: int) -> int | None:
        """The index after TypeScript type arguments opening at *start* (``it<Context>(``)."""
        depth = 0
        for i in range(start, min(len(self.tokens), start + 64)):
            if self.is_punct(i, "<"):
                depth += 1
            elif self.is_punct(i, ">"):
                depth -= 1
                if depth == 0:
                    return i + 1 if self.is_punct(i + 1, "(") else None
        return None

    def test_call(self, head: int) -> None:
        """Parse the test or suite call whose function name is at *head*, if it is one."""
        name = self.tokens[head].value
        modifiers: list[str] = []
        conditions: list[str] = []  # the arguments of skipIf / runIf
        dots: list[int] = []
        awaiting = False  # a call modifier still awaits its arguments
        i = head + 1
        while True:
            if self.is_punct(i, ".", "?.") and self.is_id(i + 1, *_JS_MODIFIERS):
                modifiers.append(self.tokens[i + 1].value)
                dots.append(i)
                awaiting = modifiers[-1] in _JS_CALL_MODIFIERS
                i += 2
            elif self.is_punct(i, "<") and (after := self.type_arguments_end(i)) is not None:
                i = after
            elif awaiting and i < len(self.tokens) and self.tokens[i].kind == "tpl":
                awaiting = False  # a tagged-template table: test.each`...`
                i += 1
            elif self.is_punct(i, "(") and awaiting:
                close = self.matches.get(i, len(self.tokens))
                if modifiers[-1] in ("skipIf", "runIf"):
                    conditions.append(_js_text(self.tokens[i : close + 1]))
                awaiting = False
                i = close + 1
            else:
                break
        self.chain_dots.update(dots)
        chain = ".".join((name, *modifiers)) + "".join(conditions)
        focus = name in _JS_FOCUS_HEADS or "only" in modifiers
        is_marker = focus or name in _JS_SKIP_HEADS or not _JS_SKIP_MODIFIERS.isdisjoint(modifiers)
        if not self.is_punct(i, "("):
            # Not a call, but a marker used as a value (``const t = it.skip``) still switches
            # off whatever test it is later called for.
            if is_marker:
                self.marks.append(_JsMark(head, chain, focus))
            return
        open_index, close = i, self.matches.get(i, len(self.tokens))
        arguments = self.arguments(open_index)
        first = arguments[0] if arguments else None
        title: str | None = None
        if first is not None and first[1] - first[0] == 1 and self.tokens[first[0]].kind in (
            "str",
            "tpl",
        ):
            title = self.tokens[first[0]].value
        elif first is not None and not self.is_function(*first) and any(
            self.is_function(*argument) for argument in arguments[1:]
        ):
            title = _js_text(self.tokens[first[0] : first[1]])  # e.g. it(name, fn) in a loop
        is_suite = name in _JS_SUITE_HEADS or "describe" in modifiers
        definition = (
            None if title is None else _JsDefinition(open_index, close, title, is_suite)
        )
        if definition is not None:
            self.definitions.append(definition)
        if is_marker:
            if definition is None:  # a runtime skip, e.g. playwright's test.skip(cond, why)
                chain += _js_text(self.tokens[open_index : close + 1])
            self.marks.append(_JsMark(head, chain, focus, definition))
        if definition is None:
            return
        if len(arguments) >= 2 and self.is_punct(arguments[1][0], "{"):
            self.option_marks(arguments[1][0], definition)
        if not is_suite and len(arguments) == 1:
            # No test function: mocha runs it as pending, jest's todo has no body either.
            self.marks.append(_JsMark(head, "(no test function)", False, definition))

    def option_marks(self, brace: int, definition: _JsDefinition) -> None:
        """Markers in the options object at *brace*: ``{skip: true}``, ``{todo: 'why'}``..."""
        close = self.matches.get(brace, len(self.tokens))
        i = brace + 1
        while i < close:
            if self.is_punct(i, *_JS_OPENERS):
                i = self.matches.get(i, close) + 1
                continue
            key_at_top = self.is_punct(i - 1, "{", ",") and self.is_id(i, *_JS_OPTION_MARKERS)
            if key_at_top and self.is_punct(i + 1, ":", ",", "}"):
                end = i + 1
                while end < close and not self.is_punct(end, ","):
                    opener = self.is_punct(end, *_JS_OPENERS)
                    end = self.matches.get(end, close) + 1 if opener else end + 1
                value = self.tokens[i + 2 : end] if self.is_punct(i + 1, ":") else []
                if _js_text(value) != "false":
                    key = self.tokens[i].value
                    self.marks.append(
                        _JsMark(i, f"{{{key}: {_js_text(value)}}}", key == "only", definition)
                    )
                i = end
                continue
            i += 1


def _js_suite(source: str) -> _Suite:
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


# --- comparing a change -------------------------------------------------------

# Statuses whose old side is a pre-existing file, and those whose new side exists afterwards.
_OLD_SIDE = frozenset(("M", "T", "D", "R"))
_NEW_SIDE = frozenset(("M", "T", "R", "A", "C"))


def _sides(status: str, old: str | None, new: str | None) -> tuple[bool, bool]:
    """Whether a change's file was a test before (pre-existing) and is one after."""
    was_test = status in _OLD_SIDE and old is not None and is_test_path(old)
    is_test = status in _NEW_SIDE and new is not None and is_test_path(new)
    return was_test, is_test


def _names(suite: _Suite | None) -> Counter[str]:
    return Counter(t.name for t in suite.tests) if suite else Counter()


def _marks(suite: _Suite | None) -> Counter[tuple[str, str]]:
    return Counter((m.target, m.text) for m in suite.markers) if suite else Counter()


@dataclass(frozen=True)
class _Compared:
    """One file of a change with the suites read from its two versions."""

    change: FileChange
    was_test: bool
    is_test: bool
    before: _Suite | None  # set for a pre-existing test file that is still a test
    after: _Suite | None  # set for every test file that exists after the change


@dataclass
class _Totals:
    """Tests and markers counted over every file of a change, so a moved one is matched."""

    tests_before: Counter[str] = field(default_factory=Counter)
    tests_after: Counter[str] = field(default_factory=Counter)
    marks_before: Counter[tuple[str, str]] = field(default_factory=Counter)
    marks_after: Counter[tuple[str, str]] = field(default_factory=Counter)
    moved: set[str] = field(default_factory=set)  # tests that left a pre-existing file


def _removed_tests(
    file: str, before: _Suite, after: _Suite, totals: _Totals
) -> Iterator[IntegrityFinding]:
    """Tests that left this file and are defined nowhere else in the change."""
    left = _names(before) - _names(after)
    for name in dict.fromkeys(t.name for t in before.tests):
        if name not in left or totals.tests_after[name] >= totals.tests_before[name]:
            continue
        gone = Counter(t.path for t in before.tests if t.name == name) - Counter(
            t.path for t in after.tests if t.name == name
        )
        for path in list(gone.elements()) or [(name,)]:
            yield IntegrityFinding(file, before.separator.join(path), "removed_test")


def _added_markers(
    file: str, before: _Suite | None, after: _Suite, reaches_old_tests: bool, totals: _Totals
) -> Iterator[IntegrityFinding]:
    """Markers new to this file and to the change as a whole (a moved marker is not new).

    In a new file only a marker that switches off a moved test counts, unless the file's markers
    reach older tests anyway (*reaches_old_tests*).
    """
    added = _marks(after) - _marks(before)
    for marker in after.markers:
        key = (marker.target, marker.text)
        if key not in added or totals.marks_after[key] <= totals.marks_before[key]:
            continue
        if reaches_old_tests or not marker.covers.isdisjoint(totals.moved):
            yield IntegrityFinding(file, marker.target, "skip_marker")


def integrity_findings(changes: Sequence[FileChange]) -> list[IntegrityFinding]:
    """What *changes* took away from the pre-existing tests, deduplicated, in change order.

    Pure and WHOLE: every change and all of every content is read; nothing is truncated. Tests and
    markers are totalled over the whole change, so one that moved to another file is matched there.
    """
    compared: list[_Compared] = []
    totals = _Totals()
    for change in changes:
        was_test, is_test = _sides(change.status, change.old, change.new)
        before = _suite(change.old or "", change.before) if was_test and is_test else None
        after = _suite(change.new or "", change.after) if is_test else None
        compared.append(_Compared(change, was_test, is_test, before, after))
        totals.tests_before.update(_names(before))
        totals.tests_after.update(_names(after))
        totals.marks_before.update(_marks(before))
        totals.marks_after.update(_marks(after))
        if before is not None and after is not None:
            totals.moved.update(_names(before) - _names(after))

    findings: list[IntegrityFinding] = []
    for item in compared:
        old, new = item.change.old or "", item.change.new or ""
        if item.was_test and not item.is_test:
            findings.append(IntegrityFinding(old, "", "deleted_file"))
        elif item.after is not None:
            if item.before is not None:
                findings.extend(_removed_tests(old, item.before, item.after, totals))
            # A marker in a pre-existing file can reach its old tests; one in a conftest.py
            # reaches the tests of other files.
            reaches = item.was_test or PurePosixPath(new).name == "conftest.py"
            findings.extend(_added_markers(new, item.before, item.after, reaches, totals))
    return list(dict.fromkeys(findings))


def integrity_prompt_section(findings: Sequence[IntegrityFinding]) -> str:
    """The ``TEST INTEGRITY`` section reviewers get after the diff (``""`` without findings).

    It lists every finding (never fitted) and tells reviewers to fail the change unless the task
    request explicitly asks for each removal or skip.
    """
    if not findings:
        return ""
    lines = "\n".join(f"- {finding.label()}" for finding in findings)
    return (
        "\n\nTEST INTEGRITY (computed by Conclave from git against the task's base commit; the "
        "list is complete even when the diff above was cut):\n"
        "This change takes away pre-existing test coverage (file · test · kind):\n"
        f"{lines}\n"
        "deleted_file: a test file was deleted or moved out of the test suite. removed_test: a "
        "test was removed and is not defined again elsewhere in the change. skip_marker: a skip, "
        "expected-failure, todo or focus (.only) marker was added.\n"
        "Return fail unless the task request explicitly asks for each of these removals or skips. "
        "A failing test must be fixed, not deleted or skipped."
    )


# --- reading a change from git ------------------------------------------------

_REGULAR_FILE_MODES = frozenset(("100644", "100755"))
_CONCURRENT_BLOB_READS = 8


@dataclass(frozen=True)
class _RawEntry:
    status: str
    old: str | None
    new: str | None
    old_blob: str | None  # None for an absent side or a non-regular file (symlink, submodule)
    new_blob: str | None


def _name_status_lines(listing: str) -> str:
    """``git diff --name-status -z --no-renames`` output (``status NUL path NUL``) as the lines
    :func:`modified_or_deleted_tests` reads. ``-z`` keeps every path verbatim, where the line
    format C-quotes a path containing a quote, a backslash or a control character."""
    fields = listing.split("\0")
    return "\n".join(
        f"{status}\t{path}" for status, path in zip(fields[::2], fields[1::2], strict=False)
    )


def _parse_raw_diff(raw: str) -> list[_RawEntry]:
    """Parse ``git diff --raw -z --no-abbrev`` output.

    Each record is ``:<old mode> <new mode> <old blob> <new blob> <status>`` NUL ``<path>`` NUL,
    with a second path for a rename or copy. The whole listing is parsed.
    """
    fields = raw.split("\0")
    entries: list[_RawEntry] = []
    i = 0
    while i < len(fields):
        meta = fields[i]
        i += 1
        # run_git merges stderr into the stream, so a git warning can precede the first record.
        if not meta.startswith(":"):
            cut = meta.rfind("\n:")
            if cut < 0:
                continue
            meta = meta[cut + 1 :]
        parts = meta[1:].split(" ")
        if len(parts) != 5:
            continue
        old_mode, new_mode, old_blob, new_blob, status = parts
        letter = status[:1]
        paths = fields[i : i + (2 if letter in ("R", "C") else 1)]
        i += len(paths)
        if not paths:
            break
        entries.append(
            _RawEntry(
                status=letter,
                old=None if letter == "A" else paths[0],
                new=None if letter == "D" else paths[-1],
                old_blob=old_blob if old_mode in _REGULAR_FILE_MODES else None,
                new_blob=new_blob if new_mode in _REGULAR_FILE_MODES else None,
            )
        )
    return entries


async def collect_integrity_findings(worktree: Path, checkpoint: str) -> list[IntegrityFinding]:
    """What the change staged in ``worktree`` took away from the tests at ``checkpoint``.

    The one async entry point, reading git itself (never a prompt diff; see the module docstring):

    1. ``git diff --cached --no-renames --name-status -z <checkpoint>``: when
       :func:`modified_or_deleted_tests` finds no pre-existing test file touched (a rename counts,
       as its ``D`` half), nothing can have been taken away and no further git call is made.
    2. ``git diff --cached -M --raw -z --no-abbrev <checkpoint>``: renames paired, blobs named. The
       blobs of the test files a parser understands are then read whole (``git cat-file blob``) and
       compared by :func:`integrity_findings`.

    The index holds the reviewed tree when the orchestrator calls this, hence ``--cached``. A git
    error (bad ref, not a repo) yields ``[]`` and a warning: the guard reports what it observed.
    """
    code, listing = await run_git(
        worktree, "diff", "--cached", "--no-renames", "--name-status", "-z", checkpoint
    )
    if code != 0:
        logger.warning("test integrity: git diff against %s failed: %s", checkpoint, listing[:500])
        return []
    if not modified_or_deleted_tests(_name_status_lines(listing)):
        return []
    code, raw = await run_git(
        worktree, "diff", "--cached", "-M", "--raw", "-z", "--no-abbrev", checkpoint
    )
    if code != 0:
        logger.warning("test integrity: git diff against %s failed: %s", checkpoint, raw[:500])
        return []
    entries = [e for e in _parse_raw_diff(raw) if any(_sides(e.status, e.old, e.new))]
    reads = asyncio.Semaphore(_CONCURRENT_BLOB_READS)

    async def read(blob: str | None) -> str | None:
        if blob is None:
            return None
        async with reads:
            code, text = await run_git(worktree, "cat-file", "blob", blob)
        return text if code == 0 else None

    befores: list[str | None] = []
    afters: list[str | None] = []
    for entry in entries:
        was_test, is_test = _sides(entry.status, entry.old, entry.new)
        befores.append(entry.old_blob if was_test and is_test and _parsed(entry.old) else None)
        afters.append(entry.new_blob if is_test and _parsed(entry.new) else None)
    contents = await asyncio.gather(*(read(blob) for blob in (*befores, *afters)))
    return integrity_findings(
        [
            FileChange(entry.status, entry.old, entry.new, before, after)
            for entry, before, after in zip(
                entries, contents[: len(entries)], contents[len(entries) :], strict=True
            )
        ]
    )
