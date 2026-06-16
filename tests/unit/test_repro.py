"""Unit tests for the reproduction-gate persona, its parser, and the bf-repro-pathguard."""

from __future__ import annotations

from conclave.agents import DEFAULT_PERSONAS
from conclave.config import AgentRole
from conclave.engine import ReproTest, parse_repro_test, repro_pathguard

# A well-formed reproduction reply: narration, then exactly one fenced ``repro`` block whose
# first line is the ``path:`` directive and whose remaining lines are the verbatim test body.
_VALID = """\
The claim says compute([]) should be 0 but the code returns 1. Here is a failing test.

```repro
path: tests/repro/test_widget.py
import pytest

from app.widget import compute


def test_compute_empty_returns_zero() -> None:
    assert compute([]) == 0
```
"""


# --- persona ----------------------------------------------------------------


def test_repro_persona_present() -> None:
    role, persona = DEFAULT_PERSONAS["repro"]
    assert role is AgentRole.repro
    # The prompt must spell out the contract the parser enforces.
    assert "path:" in persona
    assert "EXACTLY one" in persona
    assert "repro" in persona
    # The whole point of the gate: the test must fail on current code.
    assert "FAIL" in persona


# --- parse_repro_test -------------------------------------------------------


def test_parses_valid_repro() -> None:
    repro = parse_repro_test(_VALID)
    assert isinstance(repro, ReproTest)
    assert repro.path == "tests/repro/test_widget.py"
    # Body is the verbatim test, with the ``path:`` directive stripped out.
    assert "import pytest" in repro.body
    assert "compute([]) == 0" in repro.body
    assert "path:" not in repro.body
    assert repro.raw  # the raw block is retained for auditing


def test_returns_none_when_no_block() -> None:
    assert parse_repro_test("I could not construct a failing test for this claim.") is None


def test_rejects_multiple_blocks() -> None:
    text = (
        "```repro\npath: tests/repro/test_a.py\nassert one() == 1\n```\n"
        "```repro\npath: tests/repro/test_b.py\nassert two() == 2\n```\n"
    )
    # Two repro tests means the agent hedged instead of committing to one — reject.
    assert parse_repro_test(text) is None


def test_rejects_missing_path_directive() -> None:
    text = "```repro\nimport pytest\n\ndef test_x() -> None:\n    assert f() == 0\n```\n"
    assert parse_repro_test(text) is None


def test_rejects_empty_body() -> None:
    text = "```repro\npath: tests/repro/test_x.py\n\n   \n```\n"
    # A path with no test body is not a reproduction.
    assert parse_repro_test(text) is None


def test_path_directive_is_case_insensitive_and_tolerates_leading_blank() -> None:
    text = "```repro\n\nPATH:   tests/repro/test_case.py\nassert g() == 2\n```\n"
    repro = parse_repro_test(text)
    assert repro is not None
    assert repro.path == "tests/repro/test_case.py"
    assert "assert g() == 2" in repro.body


def test_parser_routes_path_through_pathguard() -> None:
    # A traversal path inside an otherwise-valid block must sink the whole parse — proving the
    # parser never hands back an unvetted model path.
    text = (
        "```repro\npath: ../../etc/test_evil.py\n"
        "def test_x() -> None:\n    assert h() == 0\n```\n"
    )
    assert parse_repro_test(text) is None


# --- repro_pathguard (the bf-repro-pathguard validator) ---------------------


def test_pathguard_accepts_and_normalizes_a_test_path() -> None:
    # ``.`` segments and redundant slashes are folded; the result is a clean relative path.
    assert repro_pathguard("tests/repro/./test_norm.py") == "tests/repro/test_norm.py"
    assert repro_pathguard("test_top.py") == "test_top.py"
    # The trailing ``_test.py`` convention is accepted too.
    assert repro_pathguard("pkg/widget_test.py") == "pkg/widget_test.py"
    # Surrounding whitespace is trimmed.
    assert repro_pathguard("  tests/test_ws.py  ") == "tests/test_ws.py"


def test_pathguard_rejects_non_string_and_blank() -> None:
    assert repro_pathguard(None) is None
    assert repro_pathguard(123) is None
    assert repro_pathguard("") is None
    assert repro_pathguard("   ") is None


def test_pathguard_rejects_absolute_and_home() -> None:
    assert repro_pathguard("/etc/test_passwd.py") is None
    assert repro_pathguard("~/test_home.py") is None
    assert repro_pathguard("~user/test_home.py") is None


def test_pathguard_rejects_traversal_and_windows_separators() -> None:
    assert repro_pathguard("../test_escape.py") is None
    assert repro_pathguard("tests/../../test_escape.py") is None
    assert repro_pathguard("tests\\repro\\test_win.py") is None


def test_pathguard_rejects_embedded_control_chars() -> None:
    assert repro_pathguard("tests/test_x.py\x00.txt") is None
    assert repro_pathguard("tests/test_x.py\nrm -rf") is None


def test_pathguard_rejects_non_test_files() -> None:
    # Right name shape but not a Python file.
    assert repro_pathguard("tests/test_x.txt") is None
    # A real ``.py`` file pytest would NOT collect — guards against clobbering source.
    assert repro_pathguard("src/conclave/engine/repro.py") is None
    assert repro_pathguard("conftest.py") is None
