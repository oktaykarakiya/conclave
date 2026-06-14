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

# Distinct planning-persona artifacts so integration tests can prove the routing
# keys don't collide (especially important for 'Test-Architect Agent' which is a
# superstring of 'Architect Agent' — the '# ' prefix disambiguates).
_PM_PLAN = (
    '```json\n'
    '{"approach": "product-manager: define MVP scope", "files_to_touch": ["PRD.md"]}\n'
    '```'
)
_ARCH_PLAN = (
    '```json\n'
    '{"approach": "architect: design system components", "files_to_touch": ["ARCHITECTURE.md"]}\n'
    '```'
)
_TESTARCH_PLAN = (
    '```json\n'
    '{"approach": "test-architect: define test strategy", "files_to_touch": ["TEST_PLAN.md"]}\n'
    '```'
)

# L4 epic-decomposition artifact with a child_tasks array.
_DECOMPOSE = (
    '```json\n'
    '{"child_tasks": ['
    '{"title": "Task 1", "description": "First subtask"}, '
    '{"title": "Task 2", "description": "Second subtask"}, '
    '{"title": "Task 3", "description": "Third subtask"}'
    ']}\n'
    '```'
)


class FakeProvider:
    """Configurable fake. ``developer_writes`` toggles the developer's file write;
    ``plan_malformed`` makes the planner/decompose emit fence-less text so parse returns
    None; ``empty_decomposition`` makes the decompose branch return child_tasks: []."""

    def __init__(
        self,
        *,
        developer_writes: bool = True,
        filename: str = "FEATURE.txt",
        plan_malformed: bool = False,
        empty_decomposition: bool = False,
    ) -> None:
        self.developer_writes = developer_writes
        self.filename = filename
        self.plan_malformed = plan_malformed
        self.empty_decomposition = empty_decomposition
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

        # --- planning-persona checks (must precede the generic planner below) ---
        # Each persona returns a *distinct* artifact so integration tests can prove
        # the routing keys don't collide.  The '# ' prefix is critical: 'Test-Architect
        # Agent' is a superstring of 'Architect Agent', so substring-only matching
        # would route test-architect prompts to the architect branch.
        if "# Product Manager Agent" in prompt:
            return AgentResult(ok=True, text=_PM_PLAN, model_reported="fake", cost_usd=0.0)
        if "# Architect-as-Planner Agent" in prompt:
            return AgentResult(ok=True, text=_ARCH_PLAN, model_reported="fake", cost_usd=0.0)
        if "# Test-Architect Agent" in prompt:
            return AgentResult(ok=True, text=_TESTARCH_PLAN, model_reported="fake", cost_usd=0.0)

        # --- L4 epic-decomposition branch ---
        if "Decompose this epic into child tasks" in prompt:
            if self.plan_malformed:
                # No ```json fence => _dispatch_plan parses to None, exercising the
                # L4 decomposition-degradation path.
                return AgentResult(
                    ok=True,
                    text="No decomposition produced.",
                    model_reported="fake",
                    cost_usd=0.0,
                )
            if self.empty_decomposition:
                return AgentResult(
                    ok=True,
                    text='```json\n{"child_tasks": []}\n```',
                    model_reported="fake",
                    cost_usd=0.0,
                )
            return AgentResult(ok=True, text=_DECOMPOSE, model_reported="fake", cost_usd=0.0)

        if "Produce a structured plan" in prompt:
            if self.plan_malformed:
                # No ```json fence => _dispatch_plan's regex misses and it parses to None,
                # exercising the L2/L1 dispatch-degradation path (no plan, empty preamble).
                return AgentResult(
                    ok=True, text="No plan produced.", model_reported="fake", cost_usd=0.0
                )
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
