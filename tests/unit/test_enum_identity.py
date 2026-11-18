"""Enum members are compared by value, never identity, so a hot reload cannot break them.

``conclave_reload_system`` re-imports Conclave's modules, which re-creates every ``Enum`` class,
while long-lived objects (the router, runtime workers, cached tasks and config) still hold
members of the old classes. ``old_member is NewEnum.member`` is then ``False`` although the
values match; ``==``, ``!=`` and ``in`` compare StrEnum members by value and stay correct.
"""

from __future__ import annotations

import ast
import importlib
import sys
import uuid
from collections.abc import Iterable
from pathlib import Path

import pytest

import conclave

_PACKAGE = Path(conclave.__file__).resolve().parent
_ENUM_BASES = frozenset({"Enum", "StrEnum", "IntEnum", "Flag", "IntFlag", "ReprEnum"})

# (path relative to src/conclave, ast.unparse(comparison)) of an identity comparison that
# genuinely needs `is`. None is expected: add one only with a comment saying why.
ALLOWED: frozenset[tuple[str, str]] = frozenset()


def _last_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _enum_classes(trees: Iterable[ast.Module]) -> set[str]:
    """Names of the classes deriving from an Enum, directly or through another such class."""
    classes = [node for tree in trees for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
    enums: set[str] = set()
    changed = True
    while changed:
        changed = False
        for cls in classes:
            if cls.name not in enums and any(
                _last_name(base) in _ENUM_BASES | enums for base in cls.bases
            ):
                enums.add(cls.name)
                changed = True
    return enums


def _is_enum_member(node: ast.expr, enums: set[str]) -> bool:
    """``SomeEnum.member`` or ``module.SomeEnum.member``."""
    return isinstance(node, ast.Attribute) and _last_name(node.value) in enums


def _identity_comparisons(tree: ast.Module, enums: set[str]) -> list[ast.Compare]:
    """Every comparison with an ``is`` / ``is not`` whose operand is an enum member."""
    found: list[ast.Compare] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        left = node.left
        for op, right in zip(node.ops, node.comparators, strict=True):
            if isinstance(op, ast.Is | ast.IsNot) and (
                _is_enum_member(left, enums) or _is_enum_member(right, enums)
            ):
                found.append(node)
                break
            left = right
    return found


def test_no_identity_comparison_against_an_enum_member() -> None:
    trees = {
        path.relative_to(_PACKAGE).as_posix(): ast.parse(
            path.read_text(encoding="utf-8"), filename=str(path)
        )
        for path in sorted(_PACKAGE.rglob("*.py"))
    }
    enums = _enum_classes(trees.values())
    # The scan must see Conclave's enums, or it would pass by finding nothing.
    assert {"TaskState", "StoreItemKind", "Engine"} <= enums

    found: dict[tuple[str, str], int] = {}
    for rel, tree in trees.items():
        for compare in _identity_comparisons(tree, enums):
            found.setdefault((rel, ast.unparse(compare)), compare.lineno)
    offenders = sorted(set(found) - ALLOWED)
    assert not offenders, (
        "compare enum members with ==, != or in, never `is` / `is not`: a hot reload "
        "re-creates enum classes while live objects keep the old members, so identity fails "
        "although the values match.\n"
        + "\n".join(f"  src/conclave/{rel}:{found[(rel, code)]}: {code}" for rel, code in offenders)
    )
    assert ALLOWED <= set(found), f"stale ALLOWED entries: {sorted(ALLOWED - set(found))}"


_SAMPLE = '''
import enum
from enum import Enum, StrEnum

import mod


class Derived(Base):  # defined before its base: collection runs to a fixed point
    b = 1


class Base(enum.Enum):
    """No members: a base other enums derive from."""


class E(StrEnum):
    a = "a"


class NotAnEnum:
    a = "a"


flagged = [
    x is E.a,
    x is not E.a,
    E.a is x,
    x is mod.E.a,
    x is Derived.b,
]
clean = [
    x is None,
    x == E.a,
    x != E.a,
    x in (E.a,),
    x is NotAnEnum.a,
    x is not None and x == E.a,
]
'''


def test_the_scan_flags_identity_against_members_only() -> None:
    tree = ast.parse(_SAMPLE)
    enums = _enum_classes([tree])
    assert enums == {"Derived", "Base", "E"}
    assert {ast.unparse(c) for c in _identity_comparisons(tree, enums)} == {
        "x is E.a",
        "x is not E.a",
        "E.a is x",
        "x is mod.E.a",
        "x is Derived.b",
    }


def test_reloading_an_enum_module_breaks_identity_but_not_equality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = f"_scratch_enum_{uuid.uuid4().hex}"
    (tmp_path / f"{name}.py").write_text(
        "from enum import StrEnum\n\n\n"
        "class Color(StrEnum):\n"
        '    red = "red"\n'
        '    blue = "blue"\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        mod = importlib.import_module(name)
        stale = mod.Color.red  # held by a long-lived object across the reload
        importlib.reload(mod)

        # Identity sees two different members although the values match ...
        assert stale is not mod.Color.red
        # ... while ==, != and in compare the values.
        assert stale == mod.Color.red
        assert not (stale != mod.Color.red)
        assert stale != mod.Color.blue
        assert stale in (mod.Color.red,)
        assert stale in {mod.Color.red}
    finally:
        sys.modules.pop(name, None)
