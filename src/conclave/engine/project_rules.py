"""Project rule files: read once, sent to an agent only when its engine does not load them.

A dispatch's PROJECT RULES (:func:`~conclave.engine.runner.assemble_prompt`) come from the
workspace-level ``AGENTS.md`` beside the attached checkout (``repo_path.parent``) and from the
rule file of the checkout's root: the first of ``AGENTS.md``, ``CONCLAVE.md`` and ``CLAUDE.md``
that exists. No engine reaches the workspace file (a task worktree is its own git root, where
native loading stops), but both engines load root rule files themselves, and a file the prompt
repeats reaches the model twice. :func:`read_project_rules` reads the files once (per task, or
per planning turn) and :func:`rules_for_dispatch` picks, for each dispatch, the files its engine
does not load; a task can mix engines between agents. Nothing is cut: a file is left out only
for an engine that receives it already.

- opencode (1.19.5) loads the first of ``AGENTS.md``, ``CLAUDE.md`` and ``CONTEXT.md`` at the
  root: not ``CLAUDE.md`` under ``OPENCODE_DISABLE_CLAUDE_CODE`` or
  ``OPENCODE_DISABLE_CLAUDE_CODE_PROMPT``, and none of them under
  ``OPENCODE_DISABLE_PROJECT_CONFIG``.
- Claude Code loads ``CLAUDE.md`` with the files it imports (``@path``) and, through a built-in
  plugin, ``AGENTS.md``; ``CLAUDE_CODE_DISABLE_CLAUDE_MDS`` turns both off. Worker isolation sets
  it, so an isolated worker gets the selected file, the root ``CLAUDE.md`` and its imports from
  Conclave, resolved as Claude Code resolves them. A worker that is not isolated loads
  ``CLAUDE.md`` itself and still gets ``AGENTS.md`` from Conclave: whether Claude Code loads that
  depends on the CLI version, and the duplicate is accepted when isolation is off.
- ``CONCLAVE.md`` is read by Conclave only, so it reaches every engine.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..providers import Engine, Provider, ResolvedProfile, build_invocation, resolve_engine

# The root rule file Conclave sends is the first of these that exists.
_ROOT_RULE_FILES = ("AGENTS.md", "CONCLAVE.md", "CLAUDE.md")
_AGENTS_MD = "AGENTS.md"
_CLAUDE_MD = "CLAUDE.md"

# Variables that turn an engine's own rule-file loading off.
_CLAUDE_MDS_OFF = "CLAUDE_CODE_DISABLE_CLAUDE_MDS"
_OPENCODE_CLAUDE_MD_OFF = ("OPENCODE_DISABLE_CLAUDE_CODE", "OPENCODE_DISABLE_CLAUDE_CODE_PROMPT")
_OPENCODE_PROJECT_FILES_OFF = "OPENCODE_DISABLE_PROJECT_CONFIG"
# Every value either CLI reads as true. A value only one of them takes keeps the file in the
# prompt, so a doubt costs a duplicate, never a file.
_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "y"})

# Claude Code's imports (2.1.326): ``@path`` at the start of a line or after whitespace, ``\ `` for
# a space, a ``#fragment`` dropped. A path starting with ``/`` or ``~`` stays literal text here
# (it is outside the worktree), as does any start Claude Code does not take for a path.
_IMPORT_RE = re.compile(r"(?:^|\s)@((?:[^\s\\]|\\ )+)")
_IMPORT_START_RE = re.compile(r"[A-Za-z0-9._-]")
# Imports nest at most four hops below CLAUDE.md: five files deep, Claude Code's limit.
_IMPORT_MAX_DEPTH = 4
# Claude Code imports only a file with one of these extensions, or with none.
_IMPORT_SUFFIXES = frozenset(
    ".md .txt .text .json .yaml .yml .toml .xml .csv .html .htm .css .scss .sass .less .js .ts "
    ".tsx .jsx .mjs .cjs .mts .cts .py .pyi .pyw .rb .erb .rake .go .rs .java .kt .kts .scala .c "
    ".cpp .cc .cxx .h .hpp .hxx .cs .swift .sh .bash .zsh .fish .ps1 .bat .cmd .env .ini .cfg "
    ".conf .config .properties .sql .graphql .gql .proto .vue .svelte .astro .ejs .hbs .pug .jade "
    ".php .pl .pm .lua .r .dart .ex .exs .erl .hrl .clj .cljs .cljc .edn .hs .lhs .elm .ml .mli "
    ".f .f90 .f95 .for .cmake .make .makefile .gradle .sbt .rst .adoc .asciidoc .org .tex .latex "
    ".lock .log .diff .patch".split()
)
# Never scanned for imports: fenced code blocks (``` or ~~~, closed by a run of the same character
# at least as long) and code spans (a backtick run closed by one of the same length, on one line).
_FENCE_OPEN_RE = re.compile(r" {0,3}(`{3,}(?=[^`]*$)|~{3,})")
_FENCE_CLOSE_RE = re.compile(r" {0,3}(`{3,}|~{3,})[ \t]*")
_CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)")


@dataclass(frozen=True)
class ProjectRules:
    """The rule files of one checkout (:func:`read_project_rules`), from which :meth:`select`
    picks the PROJECT RULES of each dispatch. ``ProjectRules()`` holds none."""

    workspace: str = ""  # the workspace-level AGENTS.md, stripped
    selected: str = ""  # the root rule file Conclave sends ("" when the root has none)
    selected_text: str = ""
    claude_md: str = ""  # the root CLAUDE.md, when it exists and is not the selected file
    imports: tuple[tuple[str, str], ...] = ()  # CLAUDE.md's imports: (root-relative path, text)
    extra: str = ""  # appended for every engine (the orchestrator's worktree environment notes)

    def select(self, engine: Engine, env: Mapping[str, str]) -> str:
        """The PROJECT RULES of a dispatch on *engine* whose environment is *env* (pure): the
        workspace file, the root files *engine* does not load itself, each import under a
        ``# <path>`` heading, then :attr:`extra`. A section that is empty, or whose stripped
        text (for an import, its file's) was sent already, is dropped."""
        sections = [self.workspace]
        imports: tuple[tuple[str, str], ...] = ()
        if engine == Engine.claude:
            if _truthy(env.get(_CLAUDE_MDS_OFF)):  # no native loading: Conclave is the only source
                sections += [self.selected_text, self.claude_md]
                imports = self.imports
            elif self.selected != _CLAUDE_MD:  # Claude Code loads CLAUDE.md itself
                sections.append(self.selected_text)
        elif not _opencode_loads(self.selected, env):
            sections.append(self.selected_text)
        parts = [(text, text) for text in sections]
        parts += [(text, f"# {path}\n{text}") for path, text in imports]
        kept: list[str] = []
        sent: set[str] = set()
        for text, section in parts:
            key = text.strip()
            if key and key not in sent:
                sent.add(key)
                kept.append(section)
        return "\n\n".join(kept) + self.extra


def read_project_rules(root: Path, repo_path: Path | None = None) -> ProjectRules:
    """Read the rule files of the checkout *root* (a task worktree, or the project checkout for a
    planning turn) and the workspace-level ``AGENTS.md`` beside *repo_path*, the attached
    checkout. The root file is selected by existence: an empty one still hides the next."""
    workspace = ""
    if repo_path is not None and repo_path.parent != repo_path:
        ws_agents = repo_path.parent / _AGENTS_MD
        if ws_agents.is_file():
            workspace = _read(ws_agents).strip()
    selected = next((name for name in _ROOT_RULE_FILES if (root / name).is_file()), "")
    if not selected:
        return ProjectRules(workspace=workspace)
    selected_text = _read(root / selected)
    claude_path = root / _CLAUDE_MD
    if not claude_path.is_file():
        return ProjectRules(workspace=workspace, selected=selected, selected_text=selected_text)
    claude_text = selected_text if selected == _CLAUDE_MD else _read(claude_path)
    return ProjectRules(
        workspace=workspace,
        selected=selected,
        selected_text=selected_text,
        claude_md="" if selected == _CLAUDE_MD else claude_text,
        imports=_imports(root, claude_path, claude_text),
    )


def rules_for_dispatch(rules: ProjectRules, provider: Provider, profile: ResolvedProfile) -> str:
    """The PROJECT RULES of one dispatch of *profile* on *provider*. The engine is picked as for
    the dispatch itself (:func:`~conclave.providers.resolve_engine`), and the environment is what
    that engine's provider passes for these variables: the daemon's own, then the overrides of
    :func:`~conclave.providers.build_invocation` for Claude Code (isolation, then ``extra_env``)
    or the profile's ``extra_env`` for opencode, whose isolation sets none of them."""
    engine, _ = resolve_engine(profile.model, getattr(provider, "default", Engine.opencode))
    overrides = build_invocation(profile).env if engine == Engine.claude else profile.extra_env
    return rules.select(engine, {**os.environ, **overrides})


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _TRUE_VALUES


def _opencode_loads(name: str, env: Mapping[str, str]) -> bool:
    """Whether opencode loads the selected root rule file *name* itself. It loads the first of
    AGENTS.md, CLAUDE.md and CONTEXT.md, and Conclave selects CLAUDE.md only when the root has no
    AGENTS.md: the two match for AGENTS.md, and for CLAUDE.md while opencode reads that."""
    if _truthy(env.get(_OPENCODE_PROJECT_FILES_OFF)):
        return False
    if name == _AGENTS_MD:
        return True
    return name == _CLAUDE_MD and not any(_truthy(env.get(var)) for var in _OPENCODE_CLAUDE_MD_OFF)


def _imports(root: Path, claude_path: Path, claude_md: str) -> tuple[tuple[str, str], ...]:
    """The files the root CLAUDE.md (*claude_path*, text *claude_md*) imports, as Claude Code
    resolves them: relative to the importing file, depth first with each file after the one
    importing it, and each file once, so cycles add nothing. An imported root rule file is
    followed into its own imports; :meth:`ProjectRules.select` then drops its repeated text."""
    top = Path(os.path.normpath(root.absolute()))
    real_top = top.resolve()
    seen = {claude_path.resolve()}
    found: list[tuple[str, str]] = []

    def visit(directory: Path, text: str, depth: int) -> None:
        if depth == _IMPORT_MAX_DEPTH:
            return
        for name in _import_paths(text):
            loaded = _load_import(top, real_top, directory, name, seen)
            if loaded is not None:
                path, body = loaded
                found.append((path.relative_to(top).as_posix(), body))
                visit(path.parent, body, depth + 1)

    visit(top, claude_md, 0)
    return tuple(found)


def _import_paths(text: str) -> list[str]:
    """The paths *text* imports (``@path``), in order: outside fenced code blocks and code spans,
    with a ``#fragment`` dropped and ``\\ `` read as a space."""
    paths: list[str] = []
    fence = ""  # the opening run of the fenced code block being skipped
    for line in text.splitlines():
        if fence:
            closing = _FENCE_CLOSE_RE.fullmatch(line)
            if closing and closing[1][0] == fence[0] and len(closing[1]) >= len(fence):
                fence = ""
            continue
        opening = _FENCE_OPEN_RE.match(line)
        if opening:
            fence = opening[1]
            continue
        for match in _IMPORT_RE.finditer(_CODE_SPAN_RE.sub(" ", line)):
            path = match[1].partition("#")[0].replace("\\ ", " ")
            if path:
                paths.append(path)
    return paths


def _load_import(
    top: Path, real_top: Path, directory: Path, name: str, seen: set[Path]
) -> tuple[Path, str] | None:
    """The path and text of the file that ``@name`` in a file of *directory* imports, recorded in
    *seen*. ``None``, the ``@name`` staying literal text, when it imports nothing Conclave sends:
    an absolute or ``~`` path, a path leaving the worktree *top* (also through a symlink), no
    text file, or a file sent already."""
    if not _IMPORT_START_RE.match(name):
        return None
    path = Path(os.path.normpath(directory / name))
    suffix = path.suffix.lower()
    if not path.is_relative_to(top) or (suffix and suffix not in _IMPORT_SUFFIXES):
        return None
    try:
        real = path.resolve()
        if real in seen or not real.is_relative_to(real_top) or not real.is_file():
            return None
        body = _read(real)
    except (OSError, RuntimeError):  # unreadable, or a symlink loop (RuntimeError on Python 3.12)
        return None
    seen.add(real)
    return path, body
