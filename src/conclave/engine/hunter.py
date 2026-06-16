"""Bug-hunter output parsing — the discovery counterpart to ``verdict.py``.

The ``hunter`` persona scans a single region and emits EXACTLY ONE JSON block describing
one suspected bug: ``{"file","symbol","claim","severity"}``. ``parse_hunter_candidate``
extracts that block into a :class:`HunterCandidate`, mirroring the fenced-``json``-block
style ``parse_verdict`` uses. It returns ``None`` — never a guess — when the output breaks
the contract: no candidate block, *more than one* (the hunter must commit to a single
claim rather than hedge across several), an unparseable block, a non-string/empty
``claim``, or a hedged ``claim`` that is not a falsifiable assertion about wrong behavior.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel

# A candidate block is a fenced ``json`` object that carries the required ``claim`` key.
# Keying the match on ``"claim"`` (rather than any ``{...}``) ignores incidental code
# fences in the hunter's narration, and — combined with ``findall`` below — lets us count
# candidate blocks so a multi-candidate reply can be rejected outright. The trailing-fence
# anchor (``\}\s*```` ``` ````) makes the otherwise non-greedy body extend to the final
# ``}`` before the close fence, so a ``claim`` value containing braces is captured whole.
_JSON_BLOCK = re.compile(
    r"```(?:json)?\s*(\{(?:[^`]|`(?!``))*?\"claim\"\s*:[^`]*?\})\s*```",
    re.DOTALL | re.IGNORECASE,
)

# Hedging words disqualify a claim: a falsifiable assertion about wrong behavior must be
# concrete enough that a reproduction test would pass or fail on it. "f might be wrong"
# is not testable; "f returns n+1 for empty input" is.
_HEDGE = re.compile(
    r"\b(?:might|may|maybe|could|possibly|perhaps|probably|likely|presumably|"
    r"potentially|seems?|appears?)\b",
    re.IGNORECASE,
)


class HunterCandidate(BaseModel):
    """One suspected bug emitted by the hunter — the discovery analog of ``ParsedVerdict``.

    ``claim`` is the required, falsifiable anchor of the candidate; ``file``/``symbol``/
    ``severity`` are optional, matching the nullable columns on
    :class:`~conclave.db.models.BugCandidate` that this feeds.
    """

    file: str | None = None
    symbol: str | None = None
    claim: str
    severity: str | None = None
    raw: str = ""


def parse_hunter_candidate(text: str) -> HunterCandidate | None:
    """Extract the single suspected-bug candidate from hunter output, or ``None``.

    Returns ``None`` whenever the output violates the one-candidate contract instead of
    guessing, so the bug-fixer never acts on a malformed or hedged discovery.
    """
    blocks = _JSON_BLOCK.findall(text)
    if len(blocks) != 1:
        # Zero blocks → nothing actionable; two-or-more → the hunter spread its bets across
        # multiple candidates instead of committing to one. Either way, reject.
        return None
    try:
        data = json.loads(blocks[0])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    claim_raw = data.get("claim")
    if not isinstance(claim_raw, str):
        return None
    claim = claim_raw.strip()
    if not claim or _HEDGE.search(claim):
        # An empty or hedged claim is not a falsifiable assertion about wrong behavior.
        return None

    return HunterCandidate(
        file=_clean(data.get("file")),
        symbol=_clean(data.get("symbol")),
        claim=claim,
        severity=_clean(data.get("severity")),
        raw=blocks[0],
    )


# --- module helpers ---


def _clean(value: Any) -> str | None:
    """Normalize an optional string field: stripped text, or ``None`` when absent/blank."""
    if not isinstance(value, str):
        return None
    return value.strip() or None
