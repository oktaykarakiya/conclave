"""Unit tests for engine/context_limits.py: fitting one bulk block, resolving the limit for a
dispatch, and a guard that prompt-building code keeps no literal cut. Pure and LLM-free."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

import conclave
from conclave.config import ConclaveConfig, load_project_config
from conclave.engine.context_limits import BulkLimit, fit_block, resolve_bulk_limit

_MARKER_RE = re.compile(
    r"\[\.\.\. (\d+) of (\d+) chars omitted \(limit: context\.(\w+) = (\d+)\)\. (.+?) \.\.\.\]"
)
_GATE_HINT = "Re-run `pytest -q` in your working directory for the full output."


def _split(fitted_text: str) -> tuple[str, re.Match[str], str]:
    """Split a fitted block into (head, marker match, tail) around its marker line."""
    match = _MARKER_RE.search(fitted_text)
    assert match is not None, fitted_text
    head = fitted_text[: match.start()]
    tail = fitted_text[match.end() :]
    assert tail.startswith("\n")
    return head, match, tail[1:]


# --- fit_block ----------------------------------------------------------------


@pytest.mark.parametrize("size", [0, 1, 999, 1_000])
def test_fit_block_returns_text_at_or_below_the_limit_unchanged(size: int) -> None:
    text = ("diff --git a/x b/x\n" * 100)[:size]
    fitted = fit_block(text, limit=1_000, field="bulk_max_chars", how_to_get_rest=_GATE_HINT)
    assert fitted.text == text
    assert fitted.truncated is False
    assert fitted.original_chars == fitted.kept_chars == size


def test_fit_block_keeps_head_and_tail_on_line_boundaries_around_the_marker() -> None:
    text = "".join(f"line {i:05d} {'x' * 24}\n" for i in range(3_000))
    limit = 10_000
    fitted = fit_block(text, limit=limit, field="bulk_max_chars", how_to_get_rest=_GATE_HINT)

    assert fitted.truncated is True
    head, match, tail = _split(fitted.text)
    assert text.startswith(head) and text.endswith(tail)
    # Both cuts fall on line boundaries: the head ends a line, the tail starts one.
    assert head.endswith("\n")
    assert text[len(text) - len(tail) - 1] == "\n"
    assert 0 < len(head) <= 0.4 * limit
    assert 0 < len(tail) <= 0.6 * limit
    assert fitted.kept_chars == len(head) + len(tail) <= limit
    assert fitted.original_chars == len(text)

    omitted, total, field, shown_limit, rest = match.groups()
    assert int(omitted) == fitted.original_chars - fitted.kept_chars
    assert int(total) == len(text)
    assert (field, int(shown_limit), rest) == ("bulk_max_chars", limit, _GATE_HINT)
    marker = match.group(0)
    assert marker == (
        f"[... {omitted} of {len(text)} chars omitted "
        f"(limit: context.bulk_max_chars = {limit}). {_GATE_HINT} ...]"
    )
    assert len(fitted.text) <= limit + len(marker)


def test_fit_block_bounds_a_single_line_without_newlines() -> None:
    text = "y" * 50_000
    hint = "Run `git diff --cached abc123` in your working directory for the full diff."
    fitted = fit_block(text, limit=1_000, field="compaction_max_chars", how_to_get_rest=hint)

    head, match, tail = _split(fitted.text)
    assert head == "y" * 400 + "\n"  # cut mid-line, so the marker gets its own line
    assert set(tail) == {"y"}
    assert fitted.kept_chars == 400 + len(tail) <= 1_000
    assert len(fitted.text) <= 1_000 + len(match.group(0))
    assert "(limit: context.compaction_max_chars = 1000)" in match.group(0)
    assert hint in match.group(0)


def test_fit_block_does_not_snap_away_more_than_half_of_a_side() -> None:
    # The only newline in the head budget is near its start: snapping back to it would drop
    # most of the head, so the head is cut mid-line instead.
    text = "a\n" + "b" * 20_000
    fitted = fit_block(text, limit=1_000, field="bulk_max_chars", how_to_get_rest=_GATE_HINT)
    head, _, _ = _split(fitted.text)
    assert head == text[:400] + "\n"


# --- resolve_bulk_limit -------------------------------------------------------

_LIMITS: dict[str, Any] = {"compaction_max_chars": 5_000, "bulk_max_chars": 50_000}
_COMPACT = BulkLimit("compaction_max_chars", 5_000)
_BULK = BulkLimit("bulk_max_chars", 50_000)


def _config(
    *,
    enabled: bool = False,
    defaults: dict[str, Any] | None = None,
    overrides: dict[str, Any] | None = None,
) -> ConclaveConfig:
    raw: dict[str, Any] = {"context": {**_LIMITS, "compaction_enabled": enabled}}
    if defaults is not None:
        raw["agent_defaults"] = defaults
    if overrides is not None:
        raw["agent_overrides"] = overrides
    return load_project_config(raw)


@pytest.mark.parametrize(
    ("config", "agent", "node_compact", "expected"),
    [
        pytest.param(_config(), "developer", None, _BULK, id="default-is-off"),
        pytest.param(_config(enabled=True), "developer", None, _COMPACT, id="context-on"),
        pytest.param(
            _config(defaults={"compact_before_start": True}), "developer", None, _COMPACT,
            id="agent-defaults-true-beats-context",
        ),
        pytest.param(
            _config(enabled=True, defaults={"compact_before_start": False}), "developer",
            None, _BULK, id="agent-defaults-false-beats-context",
        ),
        pytest.param(
            _config(
                defaults={"compact_before_start": True},
                overrides={"developer": {"compact_before_start": False}},
            ),
            "developer", None, _BULK, id="override-false-beats-defaults",
        ),
        pytest.param(
            _config(
                defaults={"compact_before_start": False},
                overrides={"developer": {"compact_before_start": True}},
            ),
            "developer", None, _COMPACT, id="override-true-beats-defaults",
        ),
        pytest.param(
            _config(overrides={"security": {"compact_before_start": True}}),
            "developer", None, _BULK, id="other-agents-override-does-not-apply",
        ),
        pytest.param(
            _config(overrides={"developer": {"compact_before_start": True}}),
            "developer", False, _BULK, id="node-false-beats-override",
        ),
        pytest.param(
            _config(overrides={"developer": {"compact_before_start": False}}),
            "developer", True, _COMPACT, id="node-true-beats-override",
        ),
        pytest.param(
            _config(overrides={"developer": {"compact_before_start": True}}),
            "developer", None, _COMPACT, id="node-none-falls-through",
        ),
        pytest.param(
            _config(
                defaults={"compact_before_start": True},
                overrides={"developer": {"compact_before_start": None}},
            ),
            "developer", None, _COMPACT, id="override-null-defers-to-defaults",
        ),
        pytest.param(
            _config(
                enabled=True,
                defaults={"compact_before_start": None},
                overrides={"developer": {"compact_before_start": None}},
            ),
            "developer", None, _COMPACT, id="all-null-inherits-context",
        ),
        pytest.param(
            _config(defaults={"compact_before_start": True}), None, None, _BULK,
            id="no-agent-uses-context-only",
        ),
        pytest.param(_config(enabled=True), None, None, _COMPACT, id="no-agent-context-on"),
    ],
)
def test_resolve_bulk_limit_precedence(
    config: ConclaveConfig, agent: str | None, node_compact: bool | None, expected: BulkLimit
) -> None:
    assert resolve_bulk_limit(config, agent, node_compact) == expected


def test_stored_compact_before_start_true_now_means_compaction_for_every_agent() -> None:
    config = load_project_config({"agent_defaults": {"compact_before_start": True}})
    for agent in ("planner", "developer", "tester", "security", "reviewer", "postmortem"):
        assert resolve_bulk_limit(config, agent) == BulkLimit("compaction_max_chars", 60_000)


def test_default_config_resolves_the_bulk_ceiling() -> None:
    config = ConclaveConfig()
    assert config.agent_defaults.compact_before_start is None
    assert resolve_bulk_limit(config, "developer") == BulkLimit("bulk_max_chars", 200_000)


# --- guard: no literal cuts in prompt-building code ---------------------------

_SRC = Path(conclave.__file__).parent
_PROMPT_BUILDERS = ("engine/runner.py", "engine/orchestrator.py", "planning/session.py")
_FORBIDDEN_CUTS = {6_000, 8_000, 40_000, 2_000, 1_000, 500}
_REMOVED_CUTTERS = ("compact_prompt_section", "_MAX_DIFF_CHARS", "_truncate_diff")


def _literal_slice_bounds(tree: ast.AST) -> set[int]:
    """Every integer literal used as a slice bound (``[:8000]``, ``[-2_000:]``, ``[500:]``)."""
    bounds: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Slice):
            continue
        for part in (node.lower, node.upper):
            if isinstance(part, ast.UnaryOp) and isinstance(part.op, ast.USub):
                part = part.operand
            if isinstance(part, ast.Constant) and isinstance(part.value, int):
                bounds.add(part.value)
    return bounds


@pytest.mark.parametrize("relpath", _PROMPT_BUILDERS)
def test_prompt_building_code_keeps_no_literal_cut(relpath: str) -> None:
    source = (_SRC / relpath).read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert _literal_slice_bounds(tree).isdisjoint(_FORBIDDEN_CUTS)
    if relpath != "planning/session.py":
        for name in _REMOVED_CUTTERS:
            assert name not in source, f"{relpath} still uses {name}"
        max_chars_kwargs = [
            kw
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg == "max_chars"
        ]
        assert max_chars_kwargs == [], f"{relpath} still passes max_chars="


def test_guard_detects_the_cut_forms_it_forbids() -> None:
    sample = ast.parse('x = a[:8000] + b[-2_000:] + c[500:] + f"{d[:1000]}"')
    assert _literal_slice_bounds(sample) == {8_000, 2_000, 500, 1_000}
