# W23 Event stream completeness: replay after reconnect, gaps announced, every event handled

Make the live event stream complete: replay after a reconnect, announce gaps, and have the SPA handle every event (backlog Open 3, plus two SPA defects found while checking it).

Every bus event is persisted to the `events` table before it is published (`events/bus.py`), but the live stream loses events in three ways:
- (a) The SPA handles only the last event. `App.tsx` (and `pages/HistoryView.tsx`) react to `stream.lastEvent` in a `useEffect`. `useStream` sets `lastEvent` once per WebSocket message, and React batches state updates made before its next render. When several messages arrive before React renders (a burst: a task ending emits many events within milliseconds), the effect can run once with only the last event. A `project.created`, `project.deleted`, `project.updated`, `workflow.evolved`, `system.reloaded` or `node.*` event earlier in the batch is then never handled: the project list or the live node statuses stay stale until something else refreshes them.
- (b) A reconnect loses what was emitted while the socket was down (a daemon restart, a network blip, a laptop waking from sleep). `useStream` reconnects without saying what it last saw, and nothing refetches.
- (c) An overflow goes unannounced. The bus flags a subscriber that overflowed and dropped events (`Subscriber.gap_detected`), but neither `/ws/stream` (`web/ws.py`) nor `/api/events/stream` (SSE, `web/api.py`) tells the client (backlog Open 3, from A3).

REQUIRED
1. Server: `/ws/stream` and `/api/events/stream` accept `after_id`.
   - SSE also accepts the standard `Last-Event-ID` header, and each SSE event carries an `id:` line.
   - Replay: with `after_id`, the stream first sends the persisted events after that id that match the same filters (project, task), in id order, then continues live. No event may be duplicated or lost at the boundary: subscribe before reading the replay, then skip live events whose id is at most the last replayed id.
   - Bound: cap the replay (for example 1,000 events). When more were missed, send a gap frame and continue live.
   - Gap frame: send one when the replay was cut and when the live subscriber overflowed (`gap_detected`). Give it a shape no real event can have (for example `{"type": "stream.gap", "reason": ...}` with no `id`), and document it.
   - Validation: `after_id` must be a non-negative integer. Otherwise SSE answers 422 and the WebSocket closes with 1008. Never a 500.
   - Without `after_id`, behaviour is unchanged. Leave `/ws/planning` as it is.
2. SPA: every event is handled once, in arrival order, whatever the render timing.
   - Replace the `lastEvent`-driven effects in `App.tsx`, `HistoryView.tsx` and any other `lastEvent` consumer. Two options: a callback `useStream` calls for each event, or a cursor over `events` by id.
   - `useStream` reconnects with `after_id` set to the id of the last event it received.
   - On a gap frame, or on a reconnect without a known id, each consumer refetches what it derives from events (the project list, live node statuses, history) instead of trusting the stream.
   - Rebuild the SPA (`npm run build`) and commit `src/conclave/web/static/`.
3. Tests.
   - Python, deterministic:
     - replay order and filters;
     - the replay/live boundary, with an event emitted after the subscription and before the replay read ends: none duplicated, none lost;
     - the cap and its gap frame;
     - an overflowing live subscriber and its gap frame;
     - a bad `after_id`;
     - SSE `Last-Event-ID` and `id:` lines;
     - unchanged behaviour without `after_id`.
   - SPA: there is no frontend test runner. Extract the stream logic into a small pure module (the event cursor, the reconnect URL with `after_id`, gap handling). Either add only `vitest` as a dev dependency (no DOM environment, nothing else) and unit-test that module, or say in your final message how you verified it. Either way `npx tsc --noEmit && npm run build` stay green. If you add `vitest`, add `cd frontend && npx vitest run` to the quality gate in AGENTS.md.

CONSTRAINTS
- Never weaken or skip an existing test; wait with `tests/waits.py`, never a fixed sleep.
- Keep `src/conclave/mcp/pilot_guide.md` current if it describes the stream; in `docs/self-optimization/backlog.md`, move Open item 3 to the resolved list.
- Quality gate (must stay green): `ruff check src tests && mypy && pytest -q` and `cd frontend && npx tsc --noEmit && npm run build`.
