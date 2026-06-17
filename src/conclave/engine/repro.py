"""Reproduction-gate output parsing — the bug-fixer's "prove it first" counterpart to hunter.py.

The ``repro`` persona is handed one candidate ``{file, symbol, claim}`` and writes a single,
focused test that asserts the CORRECT behavior — one that FAILS on the current buggy code and
passes once the bug is fixed. It emits EXACTLY ONE fenced ``repro`` block whose first line is
``path: <relative test path>`` and whose remaining lines are the verbatim test body.
:func:`parse_repro_test` extracts that into a :class:`ReproTest`, mirroring the single-block,
refuse-to-guess discipline of :func:`conclave.engine.hunter.parse_hunter_candidate`: a reply that
breaks the contract yields ``None`` rather than a guess, so the gate never writes a junk test.

SECURITY: ``path`` is a MODEL-PROVIDED string that a later stage uses to WRITE a file into the
worktree, so it is never trusted raw. :func:`repro_pathguard` — the *bf-repro-pathguard* validator
— vets it lexically first (must be relative, no ``..``/absolute/``~``/Windows escapes, and a file
name pytest actually collects). A path that fails the guard fails the whole parse: the parser
hands back ``None``, never an unvetted path. The body itself feeds
:func:`conclave.db.repositories.set_repro_artifacts`, a local-only sink — discovery's secrets
hygiene carries over unchanged.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from pydantic import BaseModel

# EXACTLY one fenced ``repro`` block; capture its inner body (everything between the fences).
# ``findall`` (not ``search``) lets us COUNT blocks so a hedged reply carrying two repro tests is
# rejected outright rather than silently picking one — the same "commit to one" discipline
# ``parse_hunter_candidate`` enforces. ``[^\S\n]*`` swallows any trailing spaces on the open fence
# without crossing the newline that begins the body.
_REPRO_BLOCK = re.compile(r"```repro[^\S\n]*\n(.*?)```", re.DOTALL | re.IGNORECASE)

# The block's first non-blank line names the target path: ``path: <relative test path>``.
_PATH_LINE = re.compile(r"^[ \t]*path[ \t]*:[ \t]*(?P<path>.+?)[ \t]*$", re.IGNORECASE)


class ReproTest(BaseModel):
    """A parsed reproduction test: a guarded relative ``path`` and the verbatim test ``body``.

    ``path`` has already passed :func:`repro_pathguard`, so a constructed ``ReproTest`` always
    carries a vetted, POSIX-normalized relative test path — never the raw model string. ``body``
    is the test source with surrounding blank lines trimmed.
    """

    path: str
    body: str
    raw: str = ""


def repro_pathguard(raw_path: object) -> str | None:
    """The *bf-repro-pathguard* validator: vet a model-proposed test path, or reject it.

    Returns a POSIX-normalized RELATIVE path safe to join onto the worktree, or ``None`` for any
    path we refuse to write through. The value comes straight from the model, so this is a hard
    gate, not a hint — a later writer still resolves the result inside the worktree (defense in
    depth, exactly as :func:`conclave.engine.verdict.check_grounding` re-resolves evidence paths),
    but nothing downstream ever sees the raw string.

    Rejected: non-strings and blanks; absolute paths and ``~``/``~user`` home refs; any ``..``
    component or Windows ``\\`` separators (directory traversal / escape); embedded NUL or
    newlines (a path is one line); and any file pytest would not collect as a test (must end in
    ``.py`` with a ``test_*`` / ``*_test`` name). The test-name rule also stops the model from
    clobbering an arbitrary source file under the guise of a "reproduction".
    """
    if not isinstance(raw_path, str):
        return None
    path = raw_path.strip()
    if not path:
        return None
    # Screen out characters that make the path ambiguous or are classic escape vectors before any
    # structural parse: NUL / CR / LF (a path occupies one line) and ``\`` (Windows separators we
    # deliberately do not normalize — keeping everything POSIX makes the traversal checks total).
    if any(c in path for c in ("\x00", "\n", "\r", "\\")):
        return None

    pure = PurePosixPath(path)
    if pure.is_absolute():
        return None
    if pure.parts and pure.parts[0].startswith("~"):
        # ``~`` / ``~user`` expands to a home directory outside the worktree.
        return None
    if ".." in pure.parts:
        # Any upward component escapes the worktree — reject rather than try to bound it.
        return None

    name = pure.name
    if not name.endswith(".py"):
        return None
    if not (name.startswith("test_") or name.endswith("_test.py")):
        # Must be a file pytest collects, so the repro actually runs — and so a crafted path can
        # never overwrite a non-test source file.
        return None

    # ``PurePosixPath`` has already folded ``.`` segments and redundant slashes; with ``..`` and
    # absolutes rejected above, the normalized string is a clean, contained relative path.
    normalized = str(pure)
    if normalized in (".", ""):
        return None
    return normalized


def parse_repro_test(text: str) -> ReproTest | None:
    """Extract the single reproduction test (guarded path + body) from repro output, or ``None``.

    Returns ``None`` whenever the output breaks the one-block contract (no block, or more than one
    — the agent must commit to a single test rather than hedge), omits the leading ``path:``
    directive, carries an empty body, or names a path the bf-repro-pathguard rejects. The gate
    refuses to guess so the bug-fixer never writes an unvetted path or a bodyless "test".
    """
    blocks = _REPRO_BLOCK.findall(text)
    if len(blocks) != 1:
        # Zero blocks → nothing to run; two-or-more → the agent hedged across multiple tests
        # instead of committing to one. Either way, reject.
        return None
    inner = blocks[0]

    # The first non-blank line carries the path directive; the body is every line after it.
    lines = inner.splitlines()
    idx = _first_nonblank(lines)
    if idx is None:
        return None
    match = _PATH_LINE.match(lines[idx])
    if match is None:
        return None
    safe_path = repro_pathguard(match.group("path"))
    if safe_path is None:
        return None

    body = "\n".join(lines[idx + 1 :]).strip("\n")
    if not body.strip():
        # A path with no test body is not a reproduction.
        return None

    return ReproTest(path=safe_path, body=body, raw=inner)


# --- module helpers ---


def _first_nonblank(lines: list[str]) -> int | None:
    """Index of the first line with non-whitespace content, or ``None`` when all are blank."""
    for i, line in enumerate(lines):
        if line.strip():
            return i
    return None
