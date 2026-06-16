"""Unit tests for the bug-hunter persona and its single-candidate output parser."""

from __future__ import annotations

from conclave.agents import DEFAULT_PERSONAS
from conclave.config import AgentRole
from conclave.engine import HunterCandidate, parse_hunter_candidate

# --- persona ----------------------------------------------------------------


def test_hunter_persona_present() -> None:
    role, persona = DEFAULT_PERSONAS["hunter"]
    assert role is AgentRole.hunter
    # The prompt must spell out the contract the parser enforces.
    assert "claim" in persona
    assert "EXACTLY one JSON block" in persona


# --- parse_hunter_candidate -------------------------------------------------


def test_parses_single_valid_candidate() -> None:
    text = (
        "Scanned the region.\n"
        "```json\n"
        '{"file": "src/a.py", "symbol": "compute", '
        '"claim": "compute() returns n+1 instead of n for empty input", '
        '"severity": "high"}\n'
        "```"
    )
    cand = parse_hunter_candidate(text)
    assert isinstance(cand, HunterCandidate)
    assert cand.file == "src/a.py"
    assert cand.symbol == "compute"
    assert cand.severity == "high"
    assert "returns n+1" in cand.claim


def test_rejects_multi_candidate() -> None:
    text = (
        '```json\n{"file": "a.py", "symbol": "f", '
        '"claim": "f drops the last element of the list", "severity": "high"}\n```\n'
        '```json\n{"file": "b.py", "symbol": "g", '
        '"claim": "g divides by zero on empty input", "severity": "medium"}\n```'
    )
    assert parse_hunter_candidate(text) is None


def test_rejects_array_of_candidates() -> None:
    text = (
        '```json\n[{"file": "a.py", "symbol": "f", "claim": "f is off by one", '
        '"severity": "high"}, {"file": "b.py", "symbol": "g", '
        '"claim": "g leaks the file handle", "severity": "low"}]\n```'
    )
    assert parse_hunter_candidate(text) is None


def test_rejects_hedged_claim() -> None:
    text = (
        '```json\n{"file": "a.py", "symbol": "f", '
        '"claim": "f might return the wrong index in some cases", '
        '"severity": "low"}\n```'
    )
    assert parse_hunter_candidate(text) is None


def test_rejects_empty_claim() -> None:
    text = '```json\n{"file": "a.py", "symbol": "f", "claim": "  ", "severity": "high"}\n```'
    assert parse_hunter_candidate(text) is None


def test_returns_none_when_no_block() -> None:
    assert parse_hunter_candidate("I scanned the region and found nothing actionable.") is None


def test_optional_fields_default_to_none() -> None:
    text = '```json\n{"claim": "the tokenizer drops the final token at EOF"}\n```'
    cand = parse_hunter_candidate(text)
    assert cand is not None
    assert cand.file is None
    assert cand.symbol is None
    assert cand.severity is None
    assert cand.claim.startswith("the tokenizer")
