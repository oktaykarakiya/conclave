# W16 Interaction-log retention, no archive rewrite per dispatch, honest export

Interaction logs: settable retention, no full archive rewrite on every dispatch, an honest export.

MEASURED on this host: `interaction_logs` has 7,320 rows (38.5 MB of prompt and reply text) after 17 days and is never pruned.
- `observability/parquet_sync.py::log_agent_interaction` runs `export_interaction_logs_to_parquet(db)` after EVERY agent dispatch. That loads up to 10,000 rows into models, serialises them, and rewrites the whole archive: 0.9 s per dispatch on the daemon's event loop, measured on a copy of this database, and 46 MB written each time.
- `pyarrow` is not a dependency. Without it, the export silently writes JSON Lines into a file named `INTERACTION_LOGS.parquet` (`format: "jsonl-fallback"`).
- `default_parquet_paths()` also writes a second copy into the daemon's current working directory whenever it contains a `pyproject.toml` (the systemd unit runs in the repository checkout, so a 46 MB file sits in the repo; it is only gitignored by luck for this repository, not for others).
- The export silently stops at 10,000 rows (`list_interaction_logs(db, limit=10_000)`), so an "archive" of a larger log is incomplete without saying so.

REQUIRED BEHAVIOUR
1. Retention: new daemon-wide setting `system.interaction_log_retention_days: int`, built-in default 90, 0 = keep forever, bounds 0..3650, with a Field description, editable on the Settings page with the other `system` settings, and over MCP via `conclave_patch_config` (global). Rows whose `time_updated` is older are deleted at daemon start and then at most once an hour (after a task finishes is fine), in bounded batches (for example 1,000 rows per statement) so the shared connection is not held for long. The `usage` table (cost accounting) is NOT pruned by this.
2. No export on dispatch: `log_agent_interaction` only records the row. The archive is written only on explicit request (the existing REST export endpoint and `conclave_get_observability_stats` with `export_parquet: true`).
3. Honest export:
   - every row, read in pages, no silent cap;
   - the result reports the row count and the format;
   - the serialisation and file writing run in a worker thread (`asyncio.to_thread`), never on the event loop;
   - written atomically (temporary file plus rename), as now;
   - Parquet (`INTERACTION_LOGS.parquet`) when `pyarrow` is importable, otherwise JSON Lines in `INTERACTION_LOGS.jsonl`, never a `.parquet` name on non-Parquet content. Add `pyarrow` as an optional extra (`pip install -e ".[observability]"`) in `pyproject.toml`, documented; do not make it a hard dependency.
4. Targets: only `$CONCLAVE_HOME` by default, or the explicit `target_path`. Remove the current-working-directory target and `CONCLAVE_PARQUET_SYNC`; mention the removal in the pilot guide. Leave existing files where they are (they are the operator's data).
5. Update `src/conclave/mcp/pilot_guide.md` (retention setting, export behaviour) and the MCP tool descriptions that mention Parquet.

TESTS (deterministic, temporary home and database)
- Retention: rows older than the setting are deleted at start and on the periodic pass, newer rows kept; 0 keeps everything; batching deletes more than one batch's worth; `usage` rows untouched.
- `log_agent_interaction` performs no export (patch the export function and assert it is not called).
- Export: more than 10,000 rows are all exported (use small synthetic rows); `.jsonl` without pyarrow (simulate the import failing) and `.parquet` with it (skip that case if pyarrow is not installed in the test environment); nothing is written to the current working directory; the event loop stays responsive during a large export (for example a concurrent `asyncio.sleep(0)` ticker keeps ticking).
- The setting is in the config schema with its bounds; a value out of bounds is a 4xx over REST and an `isError` over MCP.

CONSTRAINTS
- DB migrations are append-only, if you need an index (for example on `time_updated`, which already exists): add a new migration, never edit an applied one.
- Never weaken or skip an existing test. Never call the live daemon or its MCP tools.
- Quality gate (must stay green): `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit && npm run build` (rebuild and commit `src/conclave/web/static/`).
