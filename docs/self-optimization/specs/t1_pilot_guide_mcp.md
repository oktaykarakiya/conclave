Serve the pilot guide over MCP so any model that connects to Conclave's MCP server knows, out of the box, how to operate it (multi-agent flows, review/merge, model strategy, pitfalls). The guide already exists at `src/conclave/mcp/pilot_guide.md` — do not rewrite its existing sections; you only add the tool index (item 5) and the plumbing.

1. Packaging: the guide (and the brief from item 2) must ship inside the installed package and be read with `importlib.resources` (never a path relative to the CWD or the repo root). Verify the hatch wheel includes them; add explicit include configuration only if needed.

2. `initialize` result: add the MCP `instructions` field (part of the MCP InitializeResult; clients show it to the model). Content: a new packaged file `src/conclave/mcp/pilot_brief.md`, at most 2500 characters, condensed from the guide: what Conclave is (one paragraph), the golden rules (short bullets), and exactly how to get the full guide — resource `conclave://guide/pilot` and tool `conclave_get_pilot_guide`. Model-agnostic wording (no vendor or model names).

3. Resources: `resources/list` includes `{"uri": "conclave://guide/pilot", "name": "Conclave Pilot Guide", "mimeType": "text/markdown", ...}` listed before the per-project resources; `resources/read` for that URI returns the guide markdown with `mimeType: "text/markdown"`.

4. New MCP tool `conclave_get_pilot_guide` with optional argument `section` (string). No `section` → the whole guide. A `section` matches a `## ` heading case-insensitively either by its number (e.g. `"6"`) or by a substring of its title (e.g. `"monitoring"`), returning just that section. An unknown section → `isError: true` with a message listing the available section headings. The tool's text output is markdown (not JSON), followed by a short "Live environment" footer generated at call time: Conclave version, the default engine and which engines are available on this host (reuse the existing engine-status logic behind `GET /api/models`; do not duplicate it), and the number of MCP tools.

5. Add a final section `## 12. Tool index` to the guide listing EVERY MCP tool exactly by name, grouped by purpose (projects & config, tasks, review & merge, human-in-the-loop, workflow, agents, planning, quarantine, store & swarms, observability, system), each with a one-line description of when a pilot would use it.

6. Drift guards (tests): (a) every `conclave_*` tool name mentioned anywhere in the guide or the brief exists in the MCP tool list; (b) every tool in the MCP tool list appears in the guide's tool index. A future tool added without documenting it must fail the test suite.

7. AGENTS.md: add a convention line: when MCP tools or pilot-relevant behaviour/defaults change, update `src/conclave/mcp/pilot_guide.md` (and the brief) in the same change; the drift tests enforce tool coverage.

8. Tests: `initialize` returns `instructions` (≤ 2500 chars, mentions `conclave://guide/pilot` and `conclave_get_pilot_guide`); `resources/list`/`resources/read` for the guide; the tool for whole guide, a section by number, a section by title, an unknown section (isError), and the live footer; files load via `importlib.resources`; the drift guards above.

Keep the quality gate green: `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit`.
