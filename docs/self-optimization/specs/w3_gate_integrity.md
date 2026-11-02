Gate integrity: a green gate must mean that tests actually ran and that no pre-existing test was removed or switched off to get there. Two holes today:
- When no test command resolves (`execution.baseline_test_command` is empty and repo knowledge has no `commands.test`; see `_test_command` / `_check_commands` in `engine/orchestrator.py`), `gate_commands` is empty and the gate passes silently. On one install 3 of 4 projects had no test command.
- `engine/test_integrity.py` can detect pre-existing test files a change modified or deleted, but nothing calls it (`BugFixer.check_fix_test_integrity` in `engine/bug_fixer.py` is dead code). An agent can delete a failing test or mark it skipped and still get a green gate.

1. No test command means no silent pass. New field `execution.allow_no_test_command: bool = False` in `config/models.py`. When `execution.require_full_green` is true, no test command resolves, and `allow_no_test_command` is false, the task fails before any agent is dispatched (as early as the resolved test command is known, so no planner or developer cost is spent). Use `task.failed` reason `no_test_command` and a result summary telling the operator exactly what to do: set `execution.baseline_test_command` (and `setup_command` if needed), or set `execution.allow_no_test_command: true` to run deliberately without a gate. With `allow_no_test_command: true` today's behaviour is kept, but emit one `log` event (stage `gate_skipped_no_tests`) so the Live tab shows the gate did not run. `require_full_green: false` keeps its current meaning (an explicit choice, no failure). Tasks that never reach the gate (for example a review-only flow without a developer, see `is_pass_through_review`) are not affected; state in your final message which task kinds you checked.

2. Test-integrity guard in the normal gate. New field `execution.test_integrity: Literal["block", "warn", "off"] = "block"`. After each developer attempt (before reviewers run), compute against the task's base checkpoint, using `git diff` in the worktree (never the prompt diff):
   a. pre-existing test files deleted (`test_integrity.is_test_path`);
   b. test functions/cases removed from pre-existing test files and not re-added elsewhere in the change (Python `def test_*` / `async def test_*`, including methods of `Test*` classes; JS/TS `it(` / `test(` blocks);
   c. skip/disable markers added to pre-existing test files: `@pytest.mark.skip`, `@pytest.mark.skipif`, `@pytest.mark.xfail`, `pytest.skip(`, `unittest.skip`, `it.skip`, `test.skip`, `describe.skip`, `xit(`, `xdescribe(`, `.only(`.
   Plain edits of existing tests (changed assertions, new tests) are NOT flagged. Put the detection in `engine/test_integrity.py` as pure, unit-testable parsers plus one async entry point. Reuse the existing helpers; delete `check_fix_test_integrity` or route it through the new entry point, so no dead code remains.
   When findings exist and the mode is not `off`:
   - append a `TEST INTEGRITY` section to every reviewer prompt for that attempt, listing each finding (file, test name, kind). It tells reviewers to fail the change unless the task request explicitly asks for that removal or skip;
   - emit one event per attempt (new `EventType` `test_integrity.flagged`, payload: findings list, attempt, mode);
   - record the findings in the task's result summary.
   With `block`, a flagged change is never merged automatically, even with `execution.auto_merge: true`: the task ends `done` on its committed branch, unmerged, with the findings in the summary, so a human reviews it and merges it with `conclave_merge_task` / the UI. With `warn`, only the reviewer section, event and summary apply.

3. Expose both new fields in the project Settings page next to the other execution fields (follow the existing curated-field pattern in `frontend/src/pages/ConfigPanel.tsx` / `SettingsView.tsx`), with one-line help texts. Rebuild the SPA (`cd frontend && npm run build`) and commit `src/conclave/web/static/`. Both fields must be settable over MCP `conclave_patch_config` (via `config={"execution": {...}}`; add explicit tool parameters only if the tool already lists comparable execution fields explicitly).

4. Docs in the same change: `src/conclave/mcp/pilot_guide.md` (golden rule 2 and section 3: what happens without a test command and how to opt out; section 8: what a test-integrity flag means and how to handle it; section 11 pitfalls) and `pilot_brief.md` golden rule on the test command. One line in AGENTS.md / README.md if they describe the gate.

Tests (deterministic, fake provider, no LLM):
- a project with no test command and default config: the task fails with reason `no_test_command` and the fake provider received zero dispatches; with `allow_no_test_command: true` the task completes and the `gate_skipped_no_tests` event is emitted; with a repo-knowledge `commands.test` and no `baseline_test_command` the task runs normally;
- parsers: deleted test file; removed `def test_x`; removed and re-added (moved) test not flagged; added `@pytest.mark.skip`, `pytest.skip(`, `it.skip(`, `.only(` flagged; changed assertion not flagged; a new test file not flagged; a listing larger than 200k characters still parsed whole;
- integration: a developer change that deletes a pre-existing test: reviewers' prompts contain the `TEST INTEGRITY` section; the `test_integrity.flagged` event is emitted; with `auto_merge: true` and `block` the task ends `done` but is not merged into the target branch; with `warn` it merges; with `off` there is no section and no event;
- schema / MCP round trip for both fields.

Existing tests that build projects without a test command will now fail with `no_test_command`. Fix that once in the shared fixtures/helpers (give them a test command, or `allow_no_test_command: true` where the test is about something else), never by weakening assertions.

Constraints: config stays SQLite-backed (no YAML); no test skipped or weakened; keep every behaviour not listed here.

Quality gate (must stay green): `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit && npm run build`.
