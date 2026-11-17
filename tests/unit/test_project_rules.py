"""Project rule files reach each dispatch once: chosen per engine, CLAUDE.md imports resolved.

``read_project_rules`` reads a checkout's rule files and ``rules_for_dispatch`` picks the ones the
dispatch's engine does not load itself. opencode loads the first of AGENTS.md, CLAUDE.md and
CONTEXT.md at the root; Claude Code loads CLAUDE.md (with its imports) and AGENTS.md unless
CLAUDE_CODE_DISABLE_CLAUDE_MDS is set, which worker isolation does. Deterministic: temporary
directories, no CLI.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from conclave.config import ArgMode
from conclave.engine import project_rules
from conclave.engine.project_rules import ProjectRules, read_project_rules, rules_for_dispatch
from conclave.providers import Engine, Provider, ResolvedProfile

_OPENCODE = "deepseek/deepseek-flash"
_CLAUDE = "claude-code/sonnet"
_CLAUDE_MDS_OFF = "CLAUDE_CODE_DISABLE_CLAUDE_MDS"
_OPENCODE_CLAUDE_MD_OFF = "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT"
# A provider without ``default``: the daemon's default engine is opencode.
_PROVIDER = cast(Provider, SimpleNamespace())

_WORKSPACE = "workspace rules"
_TEXTS = {
    "AGENTS.md": "agents rules\n",
    "CONCLAVE.md": "conclave rules\n",
    "CLAUDE.md": "claude rules\n",
    "CONTEXT.md": "context rules\n",
}
_A, _X, _C = _TEXTS["AGENTS.md"], _TEXTS["CONCLAVE.md"], _TEXTS["CLAUDE.md"]


@pytest.fixture(autouse=True)
def _no_rule_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    # This suite also runs inside isolated workers, whose environment carries the first of these.
    for var in (
        _CLAUDE_MDS_OFF,
        "OPENCODE_DISABLE_CLAUDE_CODE",
        _OPENCODE_CLAUDE_MD_OFF,
        "OPENCODE_DISABLE_PROJECT_CONFIG",
    ):
        monkeypatch.delenv(var, raising=False)


def _write(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _checkout(tmp_path: Path, files: dict[str, str]) -> Path:
    """A checkout (tmp_path/ws/repo) holding *files*, beside the workspace-level AGENTS.md."""
    _write(tmp_path / "ws", {"AGENTS.md": f"{_WORKSPACE}\n"})
    root = tmp_path / "ws" / "repo"
    root.mkdir()
    return _write(root, files)


def _profile(model: str, *, isolated: bool = True, **extra_env: str) -> ResolvedProfile:
    return ResolvedProfile(
        name="opencode",
        arg_mode=ArgMode.inherit,
        model=model,
        isolated=isolated,
        extra_env=extra_env,
    )


def _rules(root: Path, profile: ResolvedProfile, provider: Provider = _PROVIDER) -> str:
    return rules_for_dispatch(read_project_rules(root, repo_path=root), provider, profile)


def _joined(*sections: str) -> str:
    return "\n\n".join(sections)


# Dispatch modes: (model, isolated, daemon environment).
_MODES: dict[str, tuple[str, bool, dict[str, str]]] = {
    "opencode": (_OPENCODE, True, {}),
    "opencode-no-claude-md": (_OPENCODE, True, {_OPENCODE_CLAUDE_MD_OFF: "1"}),
    "claude-isolated": (_CLAUDE, True, {}),
    "claude-not-isolated": (_CLAUDE, False, {}),
}
# The root files present -> the root files each mode gets after the workspace file, in _MODES
# order. Every row is checked with and without a CONTEXT.md, which changes nothing.
_Files = tuple[str, ...]
_TABLE: list[tuple[_Files, _Files, _Files, _Files, _Files]] = [
    ((), (), (), (), ()),
    (("CLAUDE.md",), (), ("CLAUDE.md",), ("CLAUDE.md",), ()),
    (("CONCLAVE.md",), ("CONCLAVE.md",), ("CONCLAVE.md",), ("CONCLAVE.md",), ("CONCLAVE.md",)),
    (
        ("CONCLAVE.md", "CLAUDE.md"),
        ("CONCLAVE.md",),
        ("CONCLAVE.md",),
        ("CONCLAVE.md", "CLAUDE.md"),
        ("CONCLAVE.md",),
    ),
    (("AGENTS.md",), (), (), ("AGENTS.md",), ("AGENTS.md",)),
    (("AGENTS.md", "CLAUDE.md"), (), (), ("AGENTS.md", "CLAUDE.md"), ("AGENTS.md",)),
    (("AGENTS.md", "CONCLAVE.md"), (), (), ("AGENTS.md",), ("AGENTS.md",)),
    (
        ("AGENTS.md", "CONCLAVE.md", "CLAUDE.md"),
        (),
        (),
        ("AGENTS.md", "CLAUDE.md"),
        ("AGENTS.md",),
    ),
]


def _table_cases() -> Iterator[Any]:
    for present, *expected in _TABLE:
        for files in (present, (*present, "CONTEXT.md")):
            for mode, sections in zip(_MODES, expected, strict=True):
                yield pytest.param(files, mode, sections, id=f"{mode}:{'+'.join(files) or '-'}")


@pytest.mark.parametrize(("files", "mode", "sections"), list(_table_cases()))
def test_each_dispatch_gets_the_rule_files_its_engine_does_not_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    files: tuple[str, ...],
    mode: str,
    sections: tuple[str, ...],
) -> None:
    model, isolated, env = _MODES[mode]
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    root = _checkout(tmp_path, {name: _TEXTS[name] for name in files})

    rules = _rules(root, _profile(model, isolated=isolated))

    assert rules == _joined(_WORKSPACE, *(_TEXTS[name] for name in sections))


@pytest.mark.parametrize("var", ["OPENCODE_DISABLE_CLAUDE_CODE", _OPENCODE_CLAUDE_MD_OFF])
@pytest.mark.parametrize(
    ("value", "sent"),
    [("1", True), ("true", True), (" TRUE ", True), ("yes", True), ("on", True), ("y", True),
     ("0", False), ("false", False), ("no", False), ("off", False), ("", False)],
)
def test_opencode_gets_claude_md_when_it_does_not_read_claude_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, var: str, value: str, sent: bool
) -> None:
    monkeypatch.setenv(var, value)
    root = _checkout(tmp_path, {"CLAUDE.md": _C})

    assert _rules(root, _profile(_OPENCODE)) == (_joined(_WORKSPACE, _C) if sent else _WORKSPACE)


def test_opencode_without_project_files_gets_the_selected_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_DISABLE_PROJECT_CONFIG", "true")
    root = _checkout(tmp_path, {"AGENTS.md": _A, "CLAUDE.md": _C})

    assert _rules(root, _profile(_OPENCODE)) == _joined(_WORKSPACE, _A)


def test_the_profile_extra_env_counts_for_both_engines(tmp_path: Path) -> None:
    root = _checkout(tmp_path, {"CLAUDE.md": _C})

    opencode = _profile(_OPENCODE, **{_OPENCODE_CLAUDE_MD_OFF: "1"})
    assert _rules(root, opencode) == _joined(_WORKSPACE, _C)
    # Isolated, but native loading back on: Claude Code loads CLAUDE.md itself.
    assert _rules(root, _profile(_CLAUDE, **{_CLAUDE_MDS_OFF: "0"})) == _WORKSPACE
    # Not isolated, but native loading off: Conclave is the only source.
    not_isolated = _profile(_CLAUDE, isolated=False, **{_CLAUDE_MDS_OFF: "1"})
    assert _rules(root, not_isolated) == _joined(_WORKSPACE, _C)


def test_the_daemon_environment_counts_for_claude_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_CLAUDE_MDS_OFF, "1")
    root = _checkout(tmp_path, {"AGENTS.md": _A, "CLAUDE.md": _C})

    assert _rules(root, _profile(_CLAUDE, isolated=False)) == _joined(_WORKSPACE, _A, _C)


def test_the_engine_is_the_one_the_dispatch_runs_on(tmp_path: Path) -> None:
    root = _checkout(tmp_path, {"AGENTS.md": _A, "CLAUDE.md": _C})
    claude_daemon = cast(Provider, SimpleNamespace(default=Engine.claude))

    # An un-prefixed model runs on the daemon's default engine.
    assert _rules(root, _profile("opus"), claude_daemon) == _joined(_WORKSPACE, _A, _C)
    assert _rules(root, _profile("opus")) == _WORKSPACE


@pytest.mark.parametrize("link", [False, True], ids=["copy", "symlink"])
def test_identical_root_files_are_sent_once(tmp_path: Path, link: bool) -> None:
    root = _checkout(tmp_path, {"AGENTS.md": _A})
    if link:
        (root / "CLAUDE.md").symlink_to("AGENTS.md")
    else:
        (root / "CLAUDE.md").write_text(_A, encoding="utf-8")

    assert _rules(root, _profile(_CLAUDE)) == _joined(_WORKSPACE, _A)


def test_a_workspace_file_equal_to_the_root_file_is_sent_once(tmp_path: Path) -> None:
    root = _checkout(tmp_path, {"AGENTS.md": f"\n{_WORKSPACE}\n\n"})

    assert _rules(root, _profile(_CLAUDE)) == _WORKSPACE
    assert _rules(root, _profile(_CLAUDE, isolated=False)) == _WORKSPACE


def test_an_empty_selected_file_adds_nothing_and_still_hides_the_next(tmp_path: Path) -> None:
    root = _checkout(tmp_path, {"AGENTS.md": " \n", "CONCLAVE.md": _X})

    for model, isolated in [(_OPENCODE, True), (_CLAUDE, True), (_CLAUDE, False)]:
        assert _rules(root, _profile(model, isolated=isolated)) == _WORKSPACE


def test_extra_follows_the_files_for_every_engine(tmp_path: Path) -> None:
    root = _checkout(tmp_path, {"AGENTS.md": _A, "CLAUDE.md": _C})
    notes = "\n\n## Worktree environment (MANDATORY)\nUse .venv/bin/."
    rules = replace(read_project_rules(root, repo_path=root), extra=notes)

    assert rules_for_dispatch(rules, _PROVIDER, _profile(_OPENCODE)) == _WORKSPACE + notes
    assert rules_for_dispatch(rules, _PROVIDER, _profile(_CLAUDE)) == (
        _joined(_WORKSPACE, _A, _C) + notes
    )
    assert rules_for_dispatch(ProjectRules(extra=notes), _PROVIDER, _profile(_CLAUDE)) == notes


def test_no_rule_files_means_no_rules(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()

    assert ProjectRules().select(Engine.claude, {_CLAUDE_MDS_OFF: "1"}) == ""
    assert read_project_rules(root, repo_path=root) == ProjectRules()
    # Without repo_path there is no workspace file to read.
    _write(tmp_path, {"AGENTS.md": _WORKSPACE})
    assert read_project_rules(root) == ProjectRules()


# --- CLAUDE.md imports: resolved for Claude Code dispatches without native loading ---


def _isolated_claude(root: Path) -> str:
    """The PROJECT RULES of an isolated Claude Code worker on *root* (no workspace file)."""
    return rules_for_dispatch(read_project_rules(root), _PROVIDER, _profile(_CLAUDE))


def test_nested_imports_follow_the_file_importing_them(tmp_path: Path) -> None:
    claude = "Rules.\nSee @docs/a.md and @docs/c.md\n"
    root = _write(
        tmp_path / "repo",
        {
            "CLAUDE.md": claude,
            "docs/a.md": "A, see @b.md\n",  # relative to the importing file
            "docs/b.md": "B\n",
            "docs/c.md": "C\n",
        },
    )

    assert _isolated_claude(root) == _joined(
        claude, "# docs/a.md\nA, see @b.md\n", "# docs/b.md\nB\n", "# docs/c.md\nC\n"
    )


def test_imports_stop_four_hops_below_claude_md(tmp_path: Path) -> None:
    files = {"CLAUDE.md": "@i1.md\n"}
    files |= {f"i{n}.md": f"level {n} @i{n + 1}.md\n" for n in range(1, 6)}
    root = _write(tmp_path / "repo", files)

    assert _isolated_claude(root) == _joined(
        "@i1.md\n", *(f"# i{n}.md\nlevel {n} @i{n + 1}.md\n" for n in range(1, 5))
    )


def test_cycles_and_self_imports_send_and_read_each_file_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _write(
        tmp_path / "repo",
        {"CLAUDE.md": "@a.md @CLAUDE.md\n", "a.md": "A @b.md @a.md @a.md\n", "b.md": "B @a.md\n"},
    )
    reads: list[str] = []
    read = project_rules._read

    def counting_read(path: Path) -> str:
        reads.append(path.name)
        return read(path)

    monkeypatch.setattr(project_rules, "_read", counting_read)

    assert _isolated_claude(root) == _joined(
        "@a.md @CLAUDE.md\n", "# a.md\nA @b.md @a.md @a.md\n", "# b.md\nB @a.md\n"
    )
    assert sorted(reads) == ["CLAUDE.md", "a.md", "b.md"]


def test_a_file_imported_twice_is_sent_once(tmp_path: Path) -> None:
    claude = "@docs/a.md @docs/b.md @./docs/a.md\n"
    root = _write(
        tmp_path / "repo", {"CLAUDE.md": claude, "docs/a.md": "A\n", "docs/b.md": "B @a.md\n"}
    )

    assert _isolated_claude(root) == _joined(claude, "# docs/a.md\nA\n", "# docs/b.md\nB @a.md\n")


def test_a_missing_file_stays_literal(tmp_path: Path) -> None:
    claude = "See @docs/missing.md and @docs/\n"
    root = _write(tmp_path / "repo", {"CLAUDE.md": claude, "docs/other.md": "other\n"})

    assert _isolated_claude(root) == claude


def test_imports_in_code_are_not_resolved(tmp_path: Path) -> None:
    claude = (
        "Inline `@docs/inline.md` and `` @docs/double.md ``.\n"
        "```sh\n@docs/fenced.md\n```\n"
        "~~~\n@docs/tilde.md\n```\n@docs/tilde.md\n~~~\n"
        "After the fences: @docs/after.md\n"
    )
    names = ("inline", "double", "fenced", "tilde", "after")
    root = _write(
        tmp_path / "repo",
        {"CLAUDE.md": claude} | {f"docs/{name}.md": f"{name}\n" for name in names},
    )

    assert _isolated_claude(root) == _joined(claude, "# docs/after.md\nafter\n")


def test_paths_outside_the_worktree_stay_literal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _write(tmp_path / "home", {"notes.md": "home notes\n"})
    monkeypatch.setenv("HOME", str(home))
    _write(tmp_path, {"outside.md": "outside\n"})
    root = tmp_path / "repo"
    claude = (
        f"@{root / 'docs/in.md'} @~/notes.md @../outside.md @docs/../../outside.md "
        "@docs/link.md\n"
    )
    _write(root, {"CLAUDE.md": claude, "docs/in.md": "in\n"})
    (root / "docs" / "link.md").symlink_to(tmp_path / "outside.md")

    assert _isolated_claude(root) == claude


def test_a_path_leaving_the_worktree_stays_literal_even_if_it_leads_back(tmp_path: Path) -> None:
    root = _write(tmp_path / "repo", {"docs/in.md": "in\n"})
    (tmp_path / "back").symlink_to(root / "docs")
    claude = "@../back/in.md\n"
    _write(root, {"CLAUDE.md": claude})

    assert _isolated_claude(root) == claude


def test_claude_code_import_forms(tmp_path: Path) -> None:
    # A #fragment is dropped, "\ " is a space, only text files (by extension) are imported.
    claude = "@docs/a.md#usage @docs/my\\ notes.md @logo.png @Makefile\n"
    root = _write(
        tmp_path / "repo",
        {
            "CLAUDE.md": claude,
            "docs/a.md": "A\n",
            "docs/my notes.md": "N\n",
            "logo.png": "not a text file\n",
            "Makefile": "all:\n",
        },
    )

    assert _isolated_claude(root) == _joined(
        claude, "# docs/a.md\nA\n", "# docs/my notes.md\nN\n", "# Makefile\nall:\n"
    )


def test_an_import_of_text_sent_already_adds_only_its_own_imports(tmp_path: Path) -> None:
    # CLAUDE.md importing the selected AGENTS.md (or a copy) repeats nothing, but the files that
    # AGENTS.md imports still arrive, as Claude Code's own loading would have followed them.
    agents = "agents rules, see @docs/x.md\n"
    claude = "@AGENTS.md @docs/copy.md\nClaude only.\n"
    root = _write(
        tmp_path / "repo",
        {"AGENTS.md": agents, "CLAUDE.md": claude, "docs/copy.md": agents, "docs/x.md": "x\n"},
    )

    assert _isolated_claude(root) == _joined(agents, claude, "# docs/x.md\nx\n")


def test_imports_reach_only_claude_code_without_native_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = "@docs/a.md\n"
    root = _write(tmp_path / "repo", {"CLAUDE.md": claude, "docs/a.md": "A doc\n"})
    rules = read_project_rules(root)

    assert rules_for_dispatch(rules, _PROVIDER, _profile(_CLAUDE)) == _joined(
        claude, "# docs/a.md\nA doc\n"
    )
    assert rules_for_dispatch(rules, _PROVIDER, _profile(_CLAUDE, isolated=False)) == ""
    assert rules_for_dispatch(rules, _PROVIDER, _profile(_OPENCODE)) == ""
    monkeypatch.setenv(_OPENCODE_CLAUDE_MD_OFF, "1")  # opencode gets CLAUDE.md as it is
    assert rules_for_dispatch(rules, _PROVIDER, _profile(_OPENCODE)) == claude
