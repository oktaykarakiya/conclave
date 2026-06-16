"""Bug-Fixer test-integrity guard — which PRE-EXISTING tests a task modified or deleted.

A bug-fixer task EARNS a green gate by ADDING a focused failing test and making it pass. The cheap
way to fake that green is to weaken or remove the tests that were already there: delete a failing
test, gut its assertions, or rename it out of pytest/jest collection. This module is the detector
for exactly that — it reports the set of pre-existing test files a task MODIFIED or DELETED
(purely-ADDED test files are clean, since a brand-new test takes no prior coverage away), so a
later gate can demand a human look before such a diff is trusted.

SECURITY — why the file set comes from ``git diff --name-status`` and NOT the reviewer diff: the
diff injected into reviewer prompts is byte-truncated at
:data:`conclave.engine.orchestrator._MAX_DIFF_CHARS` (40k) to protect the context window, so a
deletion whose ``diff --git`` header lands past the cap is INVISIBLE to anything reading that text.
``git diff --name-status <checkpoint>`` is a *content-independent* source: it emits exactly one
short ``<status>\\t<path>`` record per changed file, so its size is bounded by the file COUNT, not
by how large the edits are. A deletion is therefore listed no matter how much unrelated churn
precedes it, and :func:`modified_or_deleted_tests` scans the WHOLE, untruncated listing. Feeding
this detector the truncated prompt diff instead would reopen the very hole it exists to close.

The split mirrors the rest of the bug-fixer surface (parser + thin async entry, as in
:mod:`conclave.engine.repro`): :func:`modified_or_deleted_tests` is the pure parser
(deterministically unit-testable on crafted ``--name-status`` text, including inputs larger than
the 40k cap), and :func:`collect_modified_or_deleted_tests` is the async entry point that sources
that text from git in the task worktree.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from .gitio import run_git

# TS/TSX test-file suffixes (jest/vitest convention). A file is a test when its name ends in one of
# these; kept as a tuple so the membership check stays a single ``str.endswith`` call.
_TS_TEST_SUFFIXES = (".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")


def is_test_path(path: str) -> bool:
    """Heuristically decide whether ``path`` names a test file.

    Matches the conventions a bug-fixer could weaken to fake a green gate: ``test_*.py`` /
    ``*_test.py`` (pytest), ANY file under a ``tests/`` directory (shared fixtures and ``conftest``
    count — gutting one weakens every test that imports it), and ``*.test.ts(x)`` / ``*.spec.ts(x)``
    (jest/vitest). Paths are parsed as POSIX because ``git`` always reports forward slashes.
    """
    pure = PurePosixPath(path)
    name = pure.name
    if not name:
        return False
    if name.startswith("test_") and name.endswith(".py"):
        return True
    if name.endswith("_test.py"):
        return True
    if name.endswith(_TS_TEST_SUFFIXES):
        return True
    # "under tests/" — any DIRECTORY component is exactly ``tests`` (the basename is excluded so a
    # file merely named ``tests`` does not qualify). Exact-component match, so ``my_tests/`` and
    # ``integration_tests/`` deliberately do not count.
    return "tests" in pure.parts[:-1]


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

    The WHOLE listing is scanned and nothing here truncates, so a deletion buried past the reviewer
    diff's 40k cap is still found. Unrecognized status codes are ignored rather than guessed at.
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


async def collect_modified_or_deleted_tests(worktree: Path, checkpoint: str) -> set[str]:
    """Run ``git diff --name-status <checkpoint>`` in ``worktree`` and classify the result.

    The async entry point over :func:`modified_or_deleted_tests`: it sources the file list from the
    *content-independent* ``--name-status`` plumbing (see the module docstring) rather than the
    byte-truncated reviewer diff, so a modified/deleted test is never missed for being large or
    late in the diff. A non-zero git exit (bad ref / not a repo) yields an empty set — the detector
    reports "no modified/deleted tests *observed*"; whether an inability to compute should fail
    closed is the calling gate's decision, not this collector's.
    """
    code, output = await run_git(worktree, "diff", "--name-status", checkpoint)
    if code != 0:
        return set()
    return modified_or_deleted_tests(output)
