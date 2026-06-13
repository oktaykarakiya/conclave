"""Cross-attempt memory (ported from team-ai).

Feeds a rolling log of prior *rejected* attempts into the developer prompt so the
model does not regenerate an already-rejected approach.
"""

from __future__ import annotations

from dataclasses import dataclass

_MAX_DIFF_EXCERPT = 1500


@dataclass(frozen=True)
class _Entry:
    attempt: int
    feedback: str
    diff_excerpt: str


class AttemptMemory:
    def __init__(self, max_entries: int = 5) -> None:
        self._entries: list[_Entry] = []
        self._max = max_entries

    def add(self, attempt: int, diff: str, feedback: str) -> None:
        excerpt = diff.strip()
        if len(excerpt) > _MAX_DIFF_EXCERPT:
            excerpt = excerpt[:_MAX_DIFF_EXCERPT] + "\n…(truncated)"
        self._entries.append(
            _Entry(attempt=attempt, feedback=feedback.strip(), diff_excerpt=excerpt)
        )
        if len(self._entries) > self._max:
            self._entries = self._entries[-self._max :]

    def build_preamble(self) -> str:
        if not self._entries:
            return ""
        blocks = [
            f"--- Attempt {e.attempt} (REJECTED) ---\n"
            f"Why it was rejected:\n{e.feedback}\n"
            f"Diff produced (excerpt):\n{e.diff_excerpt}"
            for e in self._entries
        ]
        body = "\n\n".join(blocks)
        return (
            "\n\nPRIOR ATTEMPT HISTORY (these approaches were already REJECTED — do NOT "
            "regenerate them; choose a materially different approach):\n" + body + "\n"
        )
