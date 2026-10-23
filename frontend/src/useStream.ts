import { useEffect, useState } from "react";

import type { EventRow } from "./types";

export type StreamStatus = "connecting" | "live" | "reconnecting";

export interface StreamState {
  events: EventRow[];
  lastEvent: EventRow | null;
  status: StreamStatus;
}

/**
 * Subscribe to the live event stream for a project (or the global stream when
 * `projectId` is null), keeping the last `max` events. Reconnects automatically
 * with exponential backoff + jitter (capped at ~10s, reset on a successful open)
 * and guards against setState-after-unmount.
 */
export function useStream(projectId: string | null, max = 400): StreamState {
  const [events, setEvents] = useState<EventRow[]>([]);
  const [lastEvent, setLastEvent] = useState<EventRow | null>(null);
  const [status, setStatus] = useState<StreamStatus>("connecting");

  useEffect(() => {
    let alive = true;
    let ws: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let delay = 500; // backoff start
    const MAX_DELAY = 10_000;

    const connect = () => {
      if (!alive) return;
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
      ws = new WebSocket(`${proto}://${location.host}/ws/stream${query}`);

      ws.onopen = () => {
        if (!alive) return;
        delay = 500; // reset backoff on a successful connection
        setStatus("live");
      };

      ws.onmessage = (msg) => {
        if (!alive) return;
        try {
          const event = JSON.parse(msg.data) as EventRow;
          setLastEvent(event);
          setEvents((prev) => [...prev.slice(-(max - 1)), event]);
        } catch {
          /* ignore malformed frame */
        }
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

  return { events, lastEvent, status };
}
