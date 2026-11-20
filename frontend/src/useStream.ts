import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import type { StreamListener } from "./eventStream";
import { StreamCursor, coalesce } from "./eventStream";
import type { EventRow } from "./types";

export type StreamStatus = "connecting" | "live" | "reconnecting";

/** Register a listener; returns its unsubscribe. */
export type StreamSubscribe = (listener: StreamListener) => () => void;

export interface StreamState {
  events: EventRow[];
  status: StreamStatus;
  /** Stable for the hook's lifetime: pass it to components that react to events. */
  subscribe: StreamSubscribe;
}

/**
 * Subscribe to the live event stream for a project (or the global stream when
 * `projectId` is null), keeping the last `max` events. Reconnects automatically
 * with exponential backoff + jitter (capped at ~10s, reset on a successful open),
 * resuming after the last event received, and guards against setState-after-unmount.
 */
export function useStream(projectId: string | null, max = 400): StreamState {
  const [events, setEvents] = useState<EventRow[]>([]);
  const [status, setStatus] = useState<StreamStatus>("connecting");
  const listeners = useRef(new Set<StreamListener>());
  const subscribe = useCallback<StreamSubscribe>((listener) => {
    listeners.current.add(listener);
    return () => {
      listeners.current.delete(listener);
    };
  }, []);

  useEffect(() => {
    let alive = true;
    let ws: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let delay = 500; // backoff start
    const MAX_DELAY = 10_000;
    const cursor = new StreamCursor();

    const connect = () => {
      if (!alive) return;
      ws = new WebSocket(cursor.nextUrl(location, projectId));

      ws.onopen = () => {
        if (!alive) return;
        delay = 500; // reset backoff on a successful connection
        setStatus("live");
        cursor.open(listeners.current);
      };

      // Listeners are called here, once per event: an effect over React state would see
      // only the last of the events that arrived before a render.
      ws.onmessage = (msg) => {
        if (!alive) return;
        const event = cursor.receive(msg.data, listeners.current);
        if (event) setEvents((prev) => [...prev.slice(-(max - 1)), event]);
      };

      const scheduleReconnect = () => {
        if (!alive) return;
        setStatus("reconnecting");
        // Exponential backoff with full jitter, capped at MAX_DELAY.
        const wait = Math.min(delay, MAX_DELAY);
        const jittered = Math.random() * wait;
        delay = Math.min(delay * 2, MAX_DELAY);
        retryTimer = setTimeout(connect, jittered);
      };

      ws.onclose = scheduleReconnect;
      ws.onerror = () => {
        // onerror is followed by onclose; close explicitly to be safe.
        ws?.close();
      };
    };

    connect();

    return () => {
      alive = false;
      if (retryTimer) clearTimeout(retryTimer);
      if (ws) {
        ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
        ws.close();
      }
    };
  }, [projectId, max]);

  return { events, status, subscribe };
}

/** Run `listener` (as of the latest render) for the stream that `subscribe` belongs to. */
export function useStreamListener(
  subscribe: StreamSubscribe | undefined,
  listener: StreamListener,
): void {
  const latest = useRef(listener);
  useLayoutEffect(() => {
    latest.current = listener;
  });
  useEffect(() => {
    if (!subscribe) return;
    return subscribe({
      onEvent: (event) => latest.current.onEvent?.(event),
      onResync: () => latest.current.onResync?.(),
    });
  }, [subscribe]);
}

/** A stable trigger for `run` (as of the latest render) that coalesces bursts: see
 * `coalesce` in `eventStream.ts`. */
export function useCoalesced(run: () => Promise<unknown>): () => void {
  const latest = useRef(run);
  useLayoutEffect(() => {
    latest.current = run;
  });
  const [trigger] = useState(() => coalesce(() => latest.current()));
  return trigger;
}
