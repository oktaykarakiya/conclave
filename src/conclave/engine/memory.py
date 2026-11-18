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

    def build_preamble(self, diff_shown_below: str = "") -> str:
        """The rejected attempts, oldest first. *diff_shown_below* is the diff the prompt shows
        after this history as YOUR IMMEDIATELY PRIOR DIFF: when it is the latest attempt's, that
        entry points there instead of repeating it, so every diff is sent once."""
        if not self._entries:
            return ""
        below = diff_shown_below.strip()
        latest = self._entries[-1]
        blocks = [
            f"--- Attempt {e.attempt} (REJECTED) ---\n"
            f"Why it was rejected:\n{e.feedback}\n"
            + (
                "Diff produced: shown below as YOUR IMMEDIATELY PRIOR DIFF"
                if e is latest and below and e.diff == below
                else f"Diff produced:\n{e.diff}"
            )
            for e in self._entries
        ]
        body = "\n\n".join(blocks)
        return (
            "\n\nPRIOR ATTEMPT HISTORY (these approaches were already REJECTED — do NOT "
            "regenerate them; choose a materially different approach):\n" + body + "\n"
        )
