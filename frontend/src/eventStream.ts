/**
 * The client side of the live event stream, without React or the DOM so that it is
 * unit-tested (`eventStream.test.ts`); `useStream.ts` runs it over a WebSocket.
 *
 * The server (`src/conclave/events/stream.py`) replays the events missed after the query's
 * `after_id` before the live ones. Where events were lost, it sends a gap frame,
 * `{"type": "stream.gap", "reason": ...}`, which has no `id` (every event has one). On a gap
 * frame, and on a reconnect that cannot resume (no event received yet, so no `after_id`), the
 * listeners refetch what they derive from events instead of trusting the stream.
 */
import type { EventRow } from "./types";

export const GAP_TYPE = "stream.gap";

/** What a consumer does with the stream. */
export interface StreamListener {
  /** Called once for each event, in arrival order, whatever the render timing. */
  onEvent?: (event: EventRow) => void;
  /** Events may have been missed (a gap frame, or a reconnect that could not resume):
   * refetch what you derive from events instead of trusting the stream. */
  onResync?: () => void;
}

/** One message of the stream: an event, or a gap where events were lost. */
export type StreamFrame =
  | { kind: "event"; event: EventRow }
  | { kind: "gap"; reason: string };

/** Read one message: null when it is neither an event nor a gap frame. */
export function parseFrame(data: unknown): StreamFrame | null {
  if (typeof data !== "string") return null;
  let value: unknown;
  try {
    value = JSON.parse(data);
  } catch {
    return null;
  }
  if (typeof value !== "object" || value === null) return null;
  const frame = value as Record<string, unknown>;
  if (!("id" in frame)) {
    if (frame.type !== GAP_TYPE) return null;
    return { kind: "gap", reason: typeof frame.reason === "string" ? frame.reason : "" };
  }
  if (typeof frame.id !== "number" || typeof frame.type !== "string") return null;
  return { kind: "event", event: value as EventRow };
}

export interface StreamLocation {
  protocol: string;
  host: string;
}

/** The `/ws/stream` URL of a project's events (every event when `projectId` is null),
 * resuming after event `afterId` when it is given. */
export function streamUrl(
  location: StreamLocation,
  projectId: string | null,
  afterId: number | null,
): string {
  const params = new URLSearchParams();
  if (projectId) params.set("project_id", projectId);
  if (afterId !== null) params.set("after_id", String(afterId));
  const query = params.toString();
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/stream${query ? `?${query}` : ""}`;
}

/** Where a stream stands across its connections: the last event received, after which the
 * next connection resumes. It passes what it receives on to the listeners. */
export class StreamCursor {
  private lastId: number | null = null;
  private attempts = 0;

  /** The URL of the next connection attempt (call it once per attempt). */
  nextUrl(location: StreamLocation, projectId: string | null): string {
    this.attempts += 1;
    return streamUrl(location, projectId, this.lastId);
  }

  /** A connection opened. A reconnect (any attempt after the first, which may have failed)
   * without an event to resume after cannot replay what was emitted meanwhile, so the
   * listeners resync. */
  open(listeners: Iterable<StreamListener>): void {
    if (this.attempts > 1 && this.lastId === null) {
      notify(listeners, (listener) => listener.onResync?.());
    }
  }

  /** Pass one message on: an event to each listener's `onEvent` (moving the cursor past it),
   * a gap frame to their `onResync`. Returns the event, else null. */
  receive(data: unknown, listeners: Iterable<StreamListener>): EventRow | null {
    const frame = parseFrame(data);
    if (frame === null) return null; // ignore malformed frame
    if (frame.kind === "gap") {
      notify(listeners, (listener) => listener.onResync?.());
      return null;
    }
    const event = frame.event;
    this.lastId = event.id;
    notify(listeners, (listener) => listener.onEvent?.(event));
    return event;
  }
}

function notify(
  listeners: Iterable<StreamListener>,
  call: (listener: StreamListener) => void,
): void {
  for (const listener of [...listeners]) {
    try {
      call(listener);
    } catch (e) {
      console.error(e); // one failing listener must not keep the message from the others
    }
  }
}

/**
 * Wrap a refetch so that a burst of triggers (an event each, up to a whole replay) runs it at
 * most twice: at once, and once more after that run when triggered meanwhile, so the last run
 * starts after the last trigger. Runs never overlap, so an older answer never lands last.
 */
export function coalesce(run: () => Promise<unknown>): () => void {
  let running = false;
  let again = false;
  const loop = async () => {
    running = true;
    do {
      again = false;
      try {
        await run();
      } catch {
        /* a failed refetch is left to the next trigger (or the view's own polling) */
      }
    } while (again);
    running = false;
  };
  return () => {
    if (running) again = true;
    else void loop();
  };
}
