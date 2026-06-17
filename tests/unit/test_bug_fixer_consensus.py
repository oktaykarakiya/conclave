"""Unit tests for the Bug-Fixer decline-consensus threshold helper.

:func:`conclave.engine.bug_fixer._decline_threshold_met` is the pure core of the consensus round:
given each polled reviewer's verdict (``"pass"`` / ``"decline"`` / ``None``-abstain) and the team's
:class:`~conclave.config.models.DeclineConsensus` threshold, it decides whether the candidate must
be routed to a human instead of auto-fixed. These tests pin all three thresholds and the abstain
rules in isolation from the (async, dispatch-driven) round that feeds it.
"""

from __future__ import annotations

import pytest

from conclave.config.models import DeclineConsensus
from conclave.engine.bug_fixer import _decline_threshold_met

_ALL = DeclineConsensus.all_mandatory
_MAJ = DeclineConsensus.majority
_TWO = DeclineConsensus.any_two


# --- all_mandatory: every polled agent must cast a usable DECLINE -------------


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        ({"tester": "decline", "security": "decline", "reviewer": "decline"}, True),
        ({"tester": "decline", "security": "decline", "reviewer": "pass"}, False),
        # A single abstain (None) breaks unanimity even though the rest declined.
        ({"tester": "decline", "security": "decline", "reviewer": None}, False),
        # All abstain → never a decline (an empty/no-signal round can't trip a handoff).
        ({"tester": None, "security": None, "reviewer": None}, False),
        # No agents polled at all → not "all declined".
        ({}, False),
        ({"tester": "decline"}, True),  # a single-agent team that declines
    ],
)
def test_all_mandatory(verdicts: dict[str, str | None], expected: bool) -> None:
    assert _decline_threshold_met(verdicts, _ALL) is expected


# --- majority: strict majority of the agents that actually voted -------------


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        # 2 of 3 decline → strict majority.
        ({"tester": "decline", "security": "decline", "reviewer": "pass"}, True),
        # 1 of 3 decline → not a majority.
        ({"tester": "decline", "security": "pass", "reviewer": "pass"}, False),
        # Even split of usable votes (1 decline / 1 pass) is NOT a strict majority.
        ({"tester": "decline", "security": "pass"}, False),
        # Abstainers drop out of the denominator: 2 decline of 2 usable (one abstain) → majority.
        ({"tester": "decline", "security": "decline", "reviewer": None}, True),
        # One decline among one usable vote (others abstain) → 1/1 is a majority.
        ({"tester": "decline", "security": None, "reviewer": None}, True),
        # All abstain → no usable votes → no majority.
        ({"tester": None, "security": None}, False),
        ({}, False),
    ],
)
def test_majority(verdicts: dict[str, str | None], expected: bool) -> None:
    assert _decline_threshold_met(verdicts, _MAJ) is expected


# --- any_two: at least two declines ------------------------------------------


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        ({"tester": "decline", "security": "decline", "reviewer": "pass"}, True),
        ({"tester": "decline", "security": "decline", "reviewer": "decline"}, True),
        # Exactly one decline is not enough.
        ({"tester": "decline", "security": "pass", "reviewer": "pass"}, False),
        # Abstains never count toward the two: one decline + abstains → False.
        ({"tester": "decline", "security": None, "reviewer": None}, False),
        ({"tester": None, "security": None, "reviewer": None}, False),
        ({}, False),
    ],
)
def test_any_two(verdicts: dict[str, str | None], expected: bool) -> None:
    assert _decline_threshold_met(verdicts, _TWO) is expected


def test_abstain_never_forces_decline_across_thresholds() -> None:
    """An all-abstain round is safe under every threshold — a flaky panel can't hand off."""
    all_abstain: dict[str, str | None] = {"tester": None, "security": None, "reviewer": None}
    for threshold in DeclineConsensus:
        assert _decline_threshold_met(all_abstain, threshold) is False


def test_unknown_verdict_value_is_treated_as_abstain() -> None:
    """A non pass/decline value (e.g. a downgraded ``unknown``) counts as an abstain, not a vote."""
    verdicts: dict[str, str | None] = {
        "tester": "decline",
        "security": "decline",
        "reviewer": "unknown",
    }
    # all_mandatory: the unknown isn't a usable decline, so unanimity fails.
    assert _decline_threshold_met(verdicts, _ALL) is False
    # majority/any_two: the two real declines still carry.
    assert _decline_threshold_met(verdicts, _MAJ) is True
    assert _decline_threshold_met(verdicts, _TWO) is True
