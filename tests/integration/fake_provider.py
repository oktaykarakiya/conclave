"""A deterministic provider test-double so the full loop runs with zero LLM cost.

It inspects the assembled prompt to act as the right agent: the planner emits a JSON
plan; reviewers emit a verdict; the developer (optionally) edits a file in the worktree
cwd to produce a real diff.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from conclave.providers import AgentResult, OnChunk, ResolvedProfile

_PASS = '```json\n{"verdict": "pass", "reason": "looks correct", "evidence": []}\n```'
_PLAN = '```json\n{"approach": "create the file", "files_to_touch": ["FEATURE.txt"]}\n```'

# Deterministic planning discussion responses for agent-ception tests
_PLANNING_PLANNER_INITIAL = """Here is the initial task breakdown for this feature.

```json
{
  "message": "I've broken this down into implementation tasks.",
  "task_changes": [
    {"action": "add", "parent_id": null, "title": "Add auth middleware", "description": "Auth mw"},
    {"action": "add", "parent_id": null, "title": "Set up database schema", "description": "DB"},
    {"action": "add", "parent_id": null, "title": "Write integration tests", "description": "Tests"}
  ],
  "ready": false
}
```"""

_PLANNING_PLANNER_INITIAL_NESTED = """Here is the initial task breakdown with nested metadata.

```json
{
  "message": "I've broken this down into implementation tasks with metadata.",
  "task_changes": [
    {
      "action": "add",
      "parent_id": null,
      "title": "Add auth middleware",
      "description": "Auth mw",
      "metadata": {"priority": "high", "tags": ["security", "core"]}
    },
    {
      "action": "add",
      "parent_id": null,
      "title": "Set up database schema",
      "description": "DB",
      "metadata": {"priority": "medium", "tags": ["data"]}
    },
    {
      "action": "add",
      "parent_id": null,
      "title": "Write integration tests",
      "description": "Tests",
      "metadata": {"priority": "low", "tags": ["quality"]}
    }
  ],
  "ready": false
}
```"""

_PLANNING_PLANNER_REFINE = """Thank you for the feedback. I've refined the task list accordingly.

```json
{
  "message": "Refined based on reviewer feedback.",
  "task_changes": [],
  "ready": true
}
```"""

_PLANNING_APPROVED = (
    "The plan looks complete and well-structured.\n"
    '```json\n{"verdict": "pass", "reason": "complete and well-structured"}\n```'
)
_PLANNING_CHANGES = (
    "There are some issues that need addressing.\n"
    '```json\n{"verdict": "fail", "reason": "issues need addressing"}\n```'
)


class FakeProvider:
    """Configurable fake. ``developer_writes`` controls whether the developer makes a change.

    ``reviewer_tampers`` simulates a misbehaving reviewer: because reviewers run with
    ``--dangerously-skip-permissions`` they *can* write to the worktree, so the fake writes
    a stray file and clobbers the developer's file when reviewing. It lets a test assert the
    orchestrator restores the reviewed tree before committing (ENG-2).
    """

    def __init__(
        self,
        *,
        developer_writes: bool = True,
        filename: str = "FEATURE.txt",
        reviewer_tampers: bool = False,
        use_nested_plan: bool = False,
    ) -> None:
        self.developer_writes = developer_writes
        self.filename = filename
        self.reviewer_tampers = reviewer_tampers
        self.use_nested_plan = use_nested_plan
        self.prompts: list[str] = []
        # Count post-mortem dispatches so failure-path tests can assert the wiring.
        self.post_mortem_calls = 0

    async def run_agent(
        self,
        *,
        profile: ResolvedProfile,
        prompt: str,
        timeout_seconds: int,
        cwd: Path | None = None,
        on_chunk: OnChunk | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AgentResult:
        self.prompts.append(prompt)
        # --- orchestrator flows (matched FIRST) ---
        # These discriminators are unique to the orchestrator and never appear in planning
        # prompts. They must win over the planning-persona checks below, because the code
        # reviewers reuse the same persona names ("Architect Agent", "Security Agent", …):
        # a code-review dispatch is keyed on its unique instruction so the broad planning
        # persona-name checks below don't intercept it. (Both now return a JSON verdict.)
        if "Produce a structured plan" in prompt:
            return AgentResult(ok=True, text=_PLAN, model_reported="fake", cost_usd=0.0)
        if "Post-Mortem Agent" in prompt:
            # The orchestrator dispatches this on a terminal task failure when
            # post_mortem_enabled. Track it so a test can assert it ran (or didn't).
            self.post_mortem_calls += 1
            return AgentResult(
                ok=True,
                text="```yaml\nrequest: tighter, retry-friendly rewrite of the task\n```",
                model_reported="fake",
                cost_usd=0.0,
            )
        if "Review the changes made for this task" in prompt:
            if self.reviewer_tampers and cwd is not None:
                # A reviewer that writes despite only being asked to review: drop a stray
                # file and clobber the developer's output. The orchestrator must discard
                # both before gating/committing (ENG-2).
                (Path(cwd) / "STRAY_REVIEWER.txt").write_text("stray\n", encoding="utf-8")
                (Path(cwd) / self.filename).write_text("tampered\n", encoding="utf-8")
            return AgentResult(ok=True, text=_PASS, model_reported="fake", cost_usd=0.0)
        # --- agent-ception planning discussion ---
        if "Planning Facilitator Agent" in prompt:
            # Planner: return initial breakdown on first call, refinement later
            if "# Discussion So Far" in prompt or "## Discussion So Far" in prompt:
                return AgentResult(
                    ok=True, text=_PLANNING_PLANNER_REFINE, model_reported="fake", cost_usd=0.0,
                )
            initial = (
                _PLANNING_PLANNER_INITIAL_NESTED
                if self.use_nested_plan
                else _PLANNING_PLANNER_INITIAL
            )
            return AgentResult(
                ok=True, text=initial, model_reported="fake", cost_usd=0.0,
            )
        if "Architect Agent" in prompt or "Tester Agent" in prompt:
            return AgentResult(
                ok=True, text=_PLANNING_APPROVED, model_reported="fake", cost_usd=0.0,
            )
        if "Security Agent" in prompt:
            return AgentResult(
                ok=True, text=_PLANNING_APPROVED, model_reported="fake", cost_usd=0.0,
            )
        if "Senior Reviewer Agent" in prompt:
            return AgentResult(
                ok=True, text=_PLANNING_APPROVED, model_reported="fake", cost_usd=0.0,
            )
        if "Risk Agent" in prompt:
            return AgentResult(
                ok=True, text=_PLANNING_APPROVED, model_reported="fake", cost_usd=0.0,
            )
        # developer fallback
        if self.developer_writes and cwd is not None:
            (Path(cwd) / self.filename).write_text("done\n", encoding="utf-8")
        # Stream the result in chunks (with a newline) so on_chunk-driven live output is
        # exercised: this drives the runner's bounded agent_output emission.
        if on_chunk is not None:
            await on_chunk("Implemented the change.\n")
            await on_chunk("VERDICT: PASS")
        return AgentResult(
            ok=True,
            text="Implemented the change. VERDICT: PASS",
            model_reported="fake",
            cost_usd=0.01,
        )
