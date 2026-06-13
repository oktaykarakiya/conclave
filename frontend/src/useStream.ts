import { useEffect, useState } from "react";

import type { EventRow } from "./types";

/** Subscribe to the live event stream for a project, keeping the last `max` events. */
export function useStream(projectId: string | null, max = 400): EventRow[] {
  const [events, setEvents] = useState<EventRow[]>([]);

  useEffect(() => {
    setEvents([]);
    if (!projectId) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/stream?project_id=${projectId}`);
    ws.onmessage = (msg) => {
      try {
        const event = JSON.parse(msg.data) as EventRow;
        setEvents((prev) => [...prev.slice(-(max - 1)), event]);
      } catch {
        /* ignore malformed frame */
      }
    };
    return () => ws.close();
  }, [projectId, max]);

  return events;
}
