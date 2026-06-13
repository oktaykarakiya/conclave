"""Verdict parsing and evidence grounding (ported from team-ai).

``parse_verdict`` extracts a structured verdict from agent output (JSON block, with a
legacy ``VERDICT: …`` string fallback). ``check_grounding`` is the anti-hallucination
core: a non-PASS verdict whose cited evidence does not exist in the task's diff *and*
on disk (within the task worktree) is downgraded to ``unknown`` so the loop never
thrashes on a phantom finding. ``decline`` (the bug-fixer's "do not auto-fix") is
grounded exactly like ``fail``/``block``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

_JSON_BLOCK = re.compile(
    r"```(?:json)?\s*(\{(?:[^`]|`(?!``))*?\"verdict\"\s*:[^`]*?\})\s*```",
    re.DOTALL | re.IGNORECASE,
)
_VALID = {"pass", "fail", "block", "decline"}
_GROUNDABLE = {"fail", "block", "decline"}


class ParsedVerdict(BaseModel):
    verdict: str = "unknown"
    reason: str = ""
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    source: str = "none"  # "json" | "string" | "none"
    raw: str = ""


def parse_verdict(log_text: str) -> ParsedVerdict:
    """Extract a structured verdict from agent log output."""
    match = _JSON_BLOCK.search(log_text)
    if match:
        try:
            data = json.loads(match.group(1))
            verdict = str(data.get("verdict", "unknown")).strip().lower()
            if verdict not in _VALID:
                verdict = "unknown"
            evidence = data.get("evidence") or []
            if not isinstance(evidence, list):
                evidence = []
            return ParsedVerdict(
                verdict=verdict,
                reason=str(data.get("reason", "")).strip(),
                evidence=evidence,
                source="json",
                raw=match.group(0),
            )
        except (json.JSONDecodeError, ValueError):
            pass

    upper = log_text.upper()
    if "VERDICT: PASS" in upper:
        return ParsedVerdict(verdict="pass", source="string", raw="VERDICT: PASS")
    for token in ("DECLINE", "BLOCK", "FAIL"):
        if f"VERDICT: {token}" in upper:
            parts = re.split(r"VERDICT:", log_text, flags=re.IGNORECASE)
            tail = parts[-1].strip() if len(parts) > 1 else "Issues found"
            return ParsedVerdict(
                verdict=token.lower(), reason=tail, source="string", raw=f"VERDICT: {token}"
            )
    return ParsedVerdict()


def check_grounding(
    verdict: ParsedVerdict, current_diff: str, worktree: Path
) -> tuple[ParsedVerdict, list[str]]:
    """Verify a non-PASS verdict's evidence references code in this task's diff and on disk.

    Returns the (possibly downgraded) verdict plus a list of grounding warnings. When the
    agent supplied structured evidence but none of it is grounded, the verdict is demoted
    to ``unknown`` so the orchestrator does not loop on a hallucinated finding.
    """
    warnings: list[str] = []

    if verdict.verdict not in _GROUNDABLE:
        return verdict, warnings
    if verdict.source != "json":
        # String-source verdicts carry no structured evidence; nothing to ground.
        return verdict, warnings
    if not verdict.evidence:
        warnings.append("grounding: non-PASS verdict with no structured evidence — cannot verify")
        return verdict, warnings

    grounded = 0
    for item in verdict.evidence:
        if not isinstance(item, dict):
            warnings.append(f"grounding: malformed evidence entry: {item!r}")
            continue
        file = item.get("file")
        if not file or not isinstance(file, str):
            warnings.append(f"grounding: evidence missing file: {item}")
            continue
        if file not in current_diff:
            warnings.append(f"grounding: evidence file {file!r} not in this task's diff")
            continue
        if not (worktree / file).is_file():
            warnings.append(f"grounding: evidence file {file!r} does not exist on disk")
            continue
        grounded += 1

    if grounded == 0:
        warnings.append(
            f"grounding: 0/{len(verdict.evidence)} evidence items verified — "
            "downgrading verdict to 'unknown'"
        )
        reason = (verdict.reason + " [DOWNGRADED: no grounded evidence]").strip()
        return verdict.model_copy(update={"verdict": "unknown", "reason": reason}), warnings

    return verdict, warnings
