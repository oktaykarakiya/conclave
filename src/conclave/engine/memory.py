"""Cross-attempt memory (ported from team-ai).

Feeds a rolling log of prior *rejected* attempts into the developer prompt so the
model does not regenerate an already-rejected approach.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class _Entry:
    attempt: int
    feedback: str
    diff: str


class AttemptMemory:
    def __init__(self, max_entries: int = 5) -> None:
        self._entries: list[_Entry] = []
        self._max = max_entries

    def add(self, attempt: int, diff: str, feedback: str) -> None:
        """Remember rejected *attempt*: its *diff* (kept as given, so a caller that fits it to a
        context limit passes the fitted text, marker included) and why it was rejected."""
        self._entries.append(_Entry(attempt=attempt, feedback=feedback.strip(), diff=diff.strip()))
        if len(self._entries) > self._max:
            self._entries = self._entries[-self._max :]

    def build_preamble(self) -> str:
        if not self._entries:
            return ""
        blocks = [
            f"--- Attempt {e.attempt} (REJECTED) ---\n"
            f"Why it was rejected:\n{e.feedback}\n"
            f"Diff produced:\n{e.diff}"
            for e in self._entries
        ]
        body = "\n\n".join(blocks)
        return (
            "\n\nPRIOR ATTEMPT HISTORY (these approaches were already REJECTED — do NOT "
            "regenerate them; choose a materially different approach):\n" + body + "\n"
        )
