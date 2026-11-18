# W14 MCP stdio safety, hot-reload-safe enum comparisons, response shapes, GET /mcp 405

Make the MCP stdio bridge safe when the daemon is unreachable, make enum comparisons hot-reload safe, document MCP response shapes in the tool descriptions, and stop answering GET /mcp with a 200 that clients re-poll every second.

1. `conclave-mcp` stdio fallback (`src/conclave/mcp/__main__.py`). When the HTTP daemon at `CONCLAVE_URL` is unreachable (connection refused, for example during a deploy restart), `_run_stdio` creates an in-process `Daemon` on the live `$CONCLAVE_HOME/conclave.db`:
   - It does not take the daemon's data-dir lock (`main.py::acquire_daemon_lock`, `daemon.lock`).
   - It runs migrations and `seed_global_defaults` against the live database.
   - It executes every tool in-process, including mutating ones: `conclave_merge_task` (git merges), `conclave_delete_task`, `conclave_patch_config`, `conclave_reload_system`.
   - So a second process mutates the daemon's data and repositories without the lock that exists to prevent exactly that, and a stdio client started while the daemon restarts can race it.
   Required:
   - Remove the in-process database fallback.
   - When the daemon is unreachable, `initialize`, `ping`, `tools/list` and `resources/list` / `resources/read` (the pilot guide resources) are answered locally from the same static definitions the server uses, without opening the database.
   - `tools/call` returns a tool result with `isError: true` and the text "Conclave daemon unreachable at <url>: <reason>. Start it (./conclave or systemctl --user start conclave) and retry."
   - Notifications (no `id`) get no response, as before.
   - Batch messages keep working.
   - Keep the HTTP forwarding path unchanged.
   - Tests: an unreachable URL (a closed local port) → `initialize`/`tools/list` succeed with the full tool list, `tools/call` is an error, and the database file in a temporary `CONCLAVE_HOME` is never created or opened (assert it does not exist); a reachable fake server path still forwards.
   - If anything in the codebase or docs relied on the fallback, update it.
2. Hot-reload enum safety. `conclave_reload_system` re-imports modules, which re-creates `Enum`/`StrEnum` classes while long-lived objects (router, runtime workers, cached tasks and config) still hold members of the old classes. An identity comparison (`x is SomeEnum.member`) between an old member and a new class is then False although the values match; `providers/router.py` already compares by value for this reason. There are about 29 such comparisons in `src/conclave` (for example in `mcp/tools.py`, `engine/bug_fixer.py`, `web/api.py`, `engine/orchestrator.py`, `providers/profiles.py`, `db/repositories.py`, `runtime.py`).
   Required:
   - replace every identity comparison against an enum member with an equality comparison (`==`, `!=`, `in`), which StrEnum makes value-based;
   - add a ruff-independent regression test that scans `src/conclave` with `ast` and fails on any `is` / `is not` whose right operand is an attribute of a class deriving from `Enum`, with a tiny allowlist mechanism for genuine identity needs (none expected);
   - add a test reproducing the bug: reload the module defining an enum (`importlib.reload` on a scratch module in `tmp_path`, not on conclave modules) and show that `==` holds where `is` does not.
3. MCP response shapes in tool descriptions (`src/conclave/mcp/tools.py`):
   - `conclave_get_task` returns `{"task": {...}, ...}` (say what else it returns);
   - `conclave_get_task_events` returns at most `limit` events per call, oldest first after `after_id`, so a client must loop with `after_id` = last id until fewer than `limit` come back.
   Put that in each tool's description, and check every other list tool for an undocumented page bound, adding the same sentence where it applies. The MCP drift tests (`tests/unit/test_mcp_pilot_guide.py`, `test_mcp_completeness.py`) must keep passing; update `pilot_guide.md` if it states something different.

4. `GET /mcp` (`web/mcp_routes.py`). MEASURED on the live daemon: Claude Code's MCP client (the pilot's own session) sent `GET /mcp` once per second from 2026-11-16 18:52 to the 2026-11-18 reboot, 392,577 requests, each one a journal line, and it started again right after the restart. The Streamable HTTP transport lets a client open an SSE stream with `GET`; the server MUST answer with `text/event-stream` or with `405 Method Not Allowed`. Conclave answers `200` with the tool-catalog JSON, which the client reads as an SSE stream that ended at once, so it reconnects after its 1 s back-off, forever.
   Required:
   - `GET /mcp` returns `405` with an `Allow: POST` header, whatever the `Accept` header (Conclave offers no server-to-client stream).
   - The metadata stays available at `GET /api/mcp/info` (unchanged); `POST /mcp` is unchanged.
   - Check the frontend, docs, the pilot guide and `conclave-mcp` for anything that reads `GET /mcp` and point it at `/api/mcp/info`.
   - Tests: `GET /mcp` with and without `Accept: text/event-stream` → 405 with `Allow` containing `POST`; `/api/mcp/info` and a `POST /mcp` `ping` unchanged.

CONSTRAINTS
- Never weaken or skip an existing test. Never call the live daemon or its MCP tools; tests use temporary homes and ports.
- Quality gate (must stay green): `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit && npm run build`.
