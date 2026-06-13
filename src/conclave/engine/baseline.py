"""Baseline-failure helpers (ported from team-ai).

The orchestrator snapshots the test suite on the target branch BEFORE a task so
reviewers do not blame pre-existing failures on this task's diff. Persistence is via
the ``baselines`` table (keyed per project + SHA); these helpers are the pure parts.
"""

from __future__ import annotations

_MAX_BASELINE_LINES = 200


def trim_output(output: str, max_lines: int = _MAX_BASELINE_LINES) -> str:
    """Keep only the trailing ``max_lines`` of test output (where failures summarize)."""
    lines = output.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\n".join(lines)


def build_baseline_preamble(target_branch: str, baseline_failures: str) -> str:
    if not baseline_failures:
        return ""
    return (
        f"\n\nPRE-EXISTING TEST FAILURES on `{target_branch}` BEFORE this task started "
        "(NOT caused by your changes; do NOT block on these — only block if YOUR diff "
        "introduces NEW failures or breaks tests that were previously passing):\n"
        f"```\n{baseline_failures}\n```\n"
    )
