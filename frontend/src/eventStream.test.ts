import { describe, expect, it, vi } from "vitest";

import type { StreamListener } from "./eventStream";
import { GAP_TYPE, StreamCursor, coalesce, parseFrame, streamUrl } from "./eventStream";
import type { EventRow } from "./types";

const HTTP = { protocol: "http:", host: "127.0.0.1:8700" };

function event(id: number): EventRow {
  return {
    id,
    project_id: "p1",
    task_id: null,
    planning_session_id: null,
    agent: null,
    type: "node.started",
    payload: { node_id: "developer" },
    ts: "2026-11-20T12:00:00+00:00",
  };
}

const message = (frame: unknown): string => JSON.stringify(frame);
const gap = (reason = "overflow"): string => message({ type: GAP_TYPE, reason });

/** A listener that records what it is told, in order. */
function recorder(): { calls: string[]; listener: StreamListener } {
  const calls: string[] = [];
  return {
    calls,
    listener: {
      onEvent: (e) => calls.push(`event ${e.id}`),
      onResync: () => calls.push("resync"),
    },
  };
}

/** One connection attempt that opens (`useStream` opens the URL and calls `open` on success). */
function connect(cursor: StreamCursor, listeners: Iterable<StreamListener>): string {
  const url = cursor.nextUrl(HTTP, "p1");
  cursor.open(listeners);
  return url;
}

/** Let every pending promise callback run. */
const settle = (): Promise<void> => new Promise((resolve) => setTimeout(resolve, 0));

describe("streamUrl", () => {
  it("resumes after the given event", () => {
    expect(streamUrl(HTTP, "p1", 42)).toBe(
      "ws://127.0.0.1:8700/ws/stream?project_id=p1&after_id=42",
    );
    expect(streamUrl(HTTP, null, 0)).toBe("ws://127.0.0.1:8700/ws/stream?after_id=0");
  });

  it("is live only without an event, for every project without one", () => {
    expect(streamUrl(HTTP, null, null)).toBe("ws://127.0.0.1:8700/ws/stream");
    expect(streamUrl(HTTP, "p1", null)).toBe("ws://127.0.0.1:8700/ws/stream?project_id=p1");
  });

  it("uses wss under https and encodes the project id", () => {
    expect(streamUrl({ protocol: "https:", host: "h" }, "a&b", null)).toBe(
      "wss://h/ws/stream?project_id=a%26b",
    );
  });
});

describe("parseFrame", () => {
  it("reads an event, and a gap frame by its shape: no id", () => {
    expect(parseFrame(message(event(3)))).toEqual({ kind: "event", event: event(3) });
    expect(parseFrame(gap("replay_truncated"))).toEqual({
      kind: "gap",
      reason: "replay_truncated",
    });
  });

  it("ignores anything else", () => {
    for (const data of [
      "not json",
      "null",
      "42",
      message("text"),
      message({ type: "log" }),
      message({ id: "7", type: "log" }),
      new ArrayBuffer(4),
    ]) {
      expect(parseFrame(data)).toBeNull();
    }
  });
});

describe("StreamCursor", () => {
  it("passes each event of a burst once, in arrival order, to every listener", () => {
    const cursor = new StreamCursor();
    const first = recorder();
    const second = recorder();
    const listeners = new Set([first.listener, second.listener]);
    connect(cursor, listeners);
    // Several messages before React renders: each still reaches the listeners.
    for (const id of [3, 4, 5]) cursor.receive(message(event(id)), listeners);
    expect(first.calls).toEqual(["event 3", "event 4", "event 5"]);
    expect(second.calls).toEqual(first.calls);
  });

  it("returns the event to keep, nothing for a gap frame or a malformed one", () => {
    const cursor = new StreamCursor();
    expect(cursor.receive(message(event(1)), [])).toEqual(event(1));
    expect(cursor.receive(gap(), [])).toBeNull();
    expect(cursor.receive("{", [])).toBeNull();
  });

  it("reconnects after the last event received", () => {
    const cursor = new StreamCursor();
    expect(connect(cursor, [])).toBe("ws://127.0.0.1:8700/ws/stream?project_id=p1");
    cursor.receive(message(event(6)), []);
    cursor.receive(message(event(7)), []);
    cursor.receive(gap(), []); // no id: the cursor stays
    cursor.receive("garbage", []);
    expect(connect(cursor, [])).toBe(
      "ws://127.0.0.1:8700/ws/stream?project_id=p1&after_id=7",
    );
  });

  it("makes the listeners resync on a gap frame", () => {
    const cursor = new StreamCursor();
    const { calls, listener } = recorder();
    connect(cursor, [listener]);
    cursor.receive(message(event(1)), [listener]);
    cursor.receive(gap(), [listener]);
    cursor.receive(message(event(9)), [listener]);
    expect(calls).toEqual(["event 1", "resync", "event 9"]);
  });

  it("makes the listeners resync on a reconnect without an event to resume after", () => {
    const cursor = new StreamCursor();
    const { calls, listener } = recorder();
    connect(cursor, [listener]); // the first connection: the listeners have just loaded
    expect(calls).toEqual([]);
    connect(cursor, [listener]); // a reconnect that cannot replay what it missed
    expect(calls).toEqual(["resync"]);
  });

  it("makes them resync when the first attempt failed and a later one opens", () => {
    const cursor = new StreamCursor();
    const { calls, listener } = recorder();
    cursor.nextUrl(HTTP, "p1"); // refused: the daemon is restarting
    connect(cursor, [listener]); // events since the listeners loaded were not received
    expect(calls).toEqual(["resync"]);
  });

  it("does not make them resync on a reconnect that resumes", () => {
    const cursor = new StreamCursor();
    const { calls, listener } = recorder();
    connect(cursor, [listener]);
    cursor.receive(message(event(1)), [listener]);
    connect(cursor, [listener]); // the server replays what was missed
    expect(calls).toEqual(["event 1"]);
  });

  it("still passes a message on to the others when a listener throws", () => {
    const report = vi.spyOn(console, "error").mockImplementation(() => {});
    const cursor = new StreamCursor();
    const { calls, listener } = recorder();
    const failing: StreamListener = {
      onEvent: () => {
        throw new Error("boom");
      },
    };
    cursor.receive(message(event(1)), [failing, listener]);
    expect(calls).toEqual(["event 1"]);
    expect(report).toHaveBeenCalledOnce();
    report.mockRestore();
  });
});

describe("coalesce", () => {
  it("runs a burst of triggers twice: at once, and once more after that run", async () => {
    const started: Array<() => void> = []; // each run's resolver, in start order
    const trigger = coalesce(() => new Promise<void>((resolve) => started.push(resolve)));

    trigger();
    trigger();
    trigger();
    expect(started).toHaveLength(1); // runs never overlap

    started[0]();
    await settle();
    expect(started).toHaveLength(2); // the triggers that came during the first run

    started[1]();
    await settle();
    expect(started).toHaveLength(2);

    trigger(); // idle again: runs at once
    expect(started).toHaveLength(3);
  });

  it("runs again after a run failed", async () => {
    let runs = 0;
    const trigger = coalesce(async () => {
      runs += 1;
      if (runs === 1) throw new Error("daemon unreachable");
    });
    trigger();
    await settle();
    trigger();
    await settle();
    expect(runs).toBe(2);
  });
});
