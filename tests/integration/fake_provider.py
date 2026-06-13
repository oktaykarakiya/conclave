"""A deterministic provider test-double so the full loop runs with zero LLM cost.

It inspects the assembled prompt to act as the right agent: the planner emits a JSON
plan; reviewers emit a verdict; the developer (optionally) edits a file in the worktree
cwd to produce a real diff.
"""

from __future__ import annotations

from pathlib import Path

from conclave.providers import AgentResult, OnChunk, ResolvedProfile

_PASS = '```json\n{"verdict": "pass", "reason": "looks correct", "evidence": []}\n```'
_PLAN = '```json\n{"approach": "create the file", "files_to_touch": ["FEATURE.txt"]}\n```'


class FakeProvider:
    """Configurable fake. ``developer_writes`` controls whether the developer makes a change."""

    def __init__(self, *, developer_writes: bool = True, filename: str = "FEATURE.txt") -> None:
        self.developer_writes = developer_writes
        self.filename = filename
        self.prompts: list[str] = []

    async def run_agent(
        self,
        *,
        profile: ResolvedProfile,
        prompt: str,
        timeout_seconds: int,
        cwd: Path | None = None,
        on_chunk: OnChunk | None = None,
    ) -> AgentResult:
        self.prompts.append(prompt)
        if "Produce a structured plan" in prompt:
            return AgentResult(ok=True, text=_PLAN, model_reported="fake", cost_usd=0.0)
        if "Review the changes made for this task" in prompt:
            return AgentResult(ok=True, text=_PASS, model_reported="fake", cost_usd=0.0)
        # developer
        if self.developer_writes and cwd is not None:
            (Path(cwd) / self.filename).write_text("done\n", encoding="utf-8")
        return AgentResult(
            ok=True,
            text="Implemented the change. VERDICT: PASS",
            model_reported="fake",
            cost_usd=0.01,
        )
