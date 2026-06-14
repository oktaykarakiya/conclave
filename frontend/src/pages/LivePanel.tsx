import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { EventRow } from "../types";
import { useStream } from "../useStream";

// Local mirror of the status union returned by useStream.
type StreamStatus = "connecting" | "live" | "reconnecting";

// Max rows kept in the DOM at once. useStream already caps the buffer (~400); we
// render a sensible slice of the tail so very chatty runs stay fast & scannable.
const MAX_VISIBLE_ROWS = 300;
// Within this many px of the bottom we consider the user "pinned" to latest.
const STICK_THRESHOLD_PX = 48;

// --- Per-event presentation -------------------------------------------------

interface EventStyle {
  /** Tailwind text color for the event-type token. */
  type: string;
  /** Tailwind text color for the summary line. */
  body: string;
  /** Optional left accent border color (used for high-signal rows). */
  accent?: string;
}

const DEFAULT_STYLE: EventStyle = { type: "text-zinc-400", body: "text-zinc-300" };

/**
 * Decide row coloring from the event type + payload. Meaningful color only:
 * rose = failure/error, emerald = success/merge, amber = warning/decline,
 * violet = planning, sky = onboarding, indigo = agent activity, zinc = neutral.
 */
function styleFor(ev: EventRow): EventStyle {
  const t = ev.type;
  const p = ev.payload ?? {};

  // Verdicts: color by the actual outcome.
  if (t === "agent.verdict") {
    const v = String(p.verdict ?? "").toLowerCase();
    if (v === "pass") return { type: "text-emerald-400", body: "text-emerald-300/90" };
    if (v === "fail" || v === "block")
      return { type: "text-rose-400", body: "text-rose-300/90", accent: "border-rose-500/60" };
    if (v === "decline") return { type: "text-amber-400", body: "text-amber-300/90" };
    return { type: "text-zinc-300", body: "text-zinc-300" };
  }

  // agent.result: ok vs failed.
  if (t === "agent.result") {
    return p.ok === false
      ? { type: "text-rose-400", body: "text-rose-300/90", accent: "border-rose-500/60" }
      : { type: "text-indigo-300", body: "text-zinc-300" };
  }

  // Hard failures / errors anywhere.
  if (
    t === "task.failed" ||
    t === "attempt.failed" ||
    t === "planning.error" ||
    t.endsWith(".error")
  ) {
    return { type: "text-rose-400", body: "text-rose-300/90", accent: "border-rose-500/70" };
  }

  // Warnings.
  if (t === "grounding.warning" || t.endsWith(".warning")) {
    return { type: "text-amber-400", body: "text-amber-300/90", accent: "border-amber-500/50" };
  }

  // Successful completions / merges.
  if (t === "task.done" || t === "task.merged" || t === "task.committed") {
    return { type: "text-emerald-400", body: "text-emerald-300/90", accent: "border-emerald-500/50" };
  }

  // Bug lifecycle.
  if (t.startsWith("bug.")) {
    if (t === "bug.discovered" || t === "bug.reproduced")
      return { type: "text-rose-300", body: "text-rose-200/80" };
    return { type: "text-zinc-400", body: "text-zinc-400" }; // dismissed/declined: muted
  }

  // Family-based fallbacks (low signal → muted, structural → tinted).
  if (t.startsWith("agent.")) return { type: "text-indigo-300/80", body: "text-zinc-400" };
  if (t.startsWith("attempt.")) return { type: "text-sky-300/80", body: "text-zinc-300" };
  if (t === "pipeline.derived") return { type: "text-violet-300", body: "text-zinc-300" };
  if (t.startsWith("planning.")) return { type: "text-violet-300/90", body: "text-zinc-300" };
  if (t.startsWith("onboarding.")) return { type: "text-sky-300/90", body: "text-zinc-300" };
  if (t.startsWith("plan.")) return { type: "text-violet-300/80", body: "text-zinc-300" };
  if (t === "consensus.round") return { type: "text-violet-300/80", body: "text-zinc-300" };
  if (t === "task.created" || t === "task.approved" || t === "task.started")
    return { type: "text-zinc-200", body: "text-zinc-300" };
  if (t === "task.cancelled") return { type: "text-zinc-500", body: "text-zinc-500" };
  if (t === "operator.steer") return { type: "text-amber-300", body: "text-zinc-300" };
  if (t === "usage.recorded") return { type: "text-zinc-500", body: "text-zinc-500" };
  if (t === "log") return { type: "text-zinc-500", body: "text-zinc-400" };

  return DEFAULT_STYLE;
}

// --- Summary line -----------------------------------------------------------

const num = (v: unknown): string => {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n.toLocaleString() : String(v);
};

/**
 * Render a short, human-readable summary for an event. Covers the full event
 * vocabulary (events/types.py); unknown types fall back to a trimmed JSON dump.
 */
function summarize(ev: EventRow): string {
  const p = ev.payload ?? {};
  switch (ev.type) {
    // task lifecycle
    case "task.created":
      return `created${p.title ? ` "${String(p.title)}"` : ""}${p.auto_approve ? " (auto-approve)" : ""}`;
    case "task.approved":
      return p.cascade ? `approved (cascade from ${String(p.root ?? "?").slice(0, 8)})` : "approved";
    case "task.started":
      return `started${p.target_branch ? ` → ${String(p.target_branch)}` : ""}`;
    case "task.cancelled":
      return "cancelled";
    case "task.committed":
      return `committed${p.branch ? ` ${String(p.branch)}` : ""}`;
    case "task.merged":
      return `merged → ${String(p.target ?? "?")}`;
    case "task.done":
      return `done · merged=${String(p.merged)}${p.attempts != null ? ` · ${num(p.attempts)} attempt(s)` : ""}`;
    case "task.failed":
      return `failed: ${String(p.reason ?? "unknown")}${p.attempts != null ? ` (after ${num(p.attempts)})` : ""}`;

    // planning (high-level)
    case "plan.level_selected":
      return `level ${String(p.level ?? p.value ?? "?")}`;
    case "plan.artifact":
      return `artifact ${String(p.name ?? p.kind ?? "")}`.trim();

    // agents
    case "agent.dispatched":
      return `dispatched · ${String(p.profile ?? "?")}${p.model ? ` (${String(p.model)})` : ""}`;
    case "agent.result": {
      const bits = [`ok=${String(p.ok)}`];
      if (p.model_reported) bits.push(`model=${String(p.model_reported)}`);
      if (typeof p.cost_usd === "number") bits.push(`$${(p.cost_usd as number).toFixed(4)}`);
      if (p.ok === false && p.error) bits.push(`— ${String(p.error).slice(0, 100)}`);
      return bits.join(" · ");
    }
    case "agent.output_chunk":
      return String(p.text ?? p.chunk ?? "").slice(0, 160);
    case "agent.verdict":
      return `${String(p.verdict)}${p.reason ? ` — ${String(p.reason).slice(0, 140)}` : ""}`;
    case "grounding.warning":
      return String(p.warning ?? "grounding warning");
    case "pipeline.derived":
      return `reviewers: ${(p.pipeline as string[] | undefined)?.join(", ") || "(none)"}`;

    // attempts / gate
    case "attempt.started":
      return `attempt #${num(p.n)}`;
    case "attempt.failed": {
      const where = String(p.stage ?? "?");
      let s = `attempt #${num(p.n)} failed @ ${where}`;
      if (p.exit_code != null) s += ` (exit ${num(p.exit_code)})`;
      if (p.error) s += ` — ${String(p.error).slice(0, 100)}`;
      return s;
    }
    case "baseline.snapshot":
      return `baseline captured${p.sha ? ` @ ${String(p.sha).slice(0, 10)}` : ""}`;

    // repo intelligence
    case "onboarding.started":
      return `onboarding${p.force ? " (forced)" : ""}`;
    case "onboarding.complete": {
      const langs = (p.languages as string[] | undefined)?.join(", ");
      return `onboarding complete${langs ? ` · ${langs}` : ""}`;
    }
    case "onboarding.ai_started":
      return `AI analysis${p.sha ? ` @ ${String(p.sha).slice(0, 10)}` : ""}`;
    case "onboarding.ai_complete":
      return p.ok === false
        ? `AI analysis failed: ${String(p.error ?? "?")}`
        : "AI analysis complete";

    // bug fixer
    case "bug.discovered":
      return `bug: ${String(p.title ?? p.summary ?? "?").slice(0, 140)}`;
    case "bug.reproduced":
      return `reproduced${p.title ? ` ${String(p.title).slice(0, 120)}` : ""}`;
    case "bug.dismissed":
      return `dismissed${p.reason ? `: ${String(p.reason).slice(0, 120)}` : ""}`;
    case "bug.declined":
      return `declined${p.reason ? `: ${String(p.reason).slice(0, 120)}` : ""}`;
    case "consensus.round":
      return `consensus round ${num(p.round ?? p.n)}${p.result ? ` → ${String(p.result)}` : ""}`;

    // planning sessions (agent-ception)
    case "planning.session_created":
      return `session created${p.title ? ` "${String(p.title)}"` : ""}`;
    case "planning.session_started":
      return "session started";
    case "planning.agent_turn":
      return `${String(p.agent ?? ev.agent ?? "agent")} · turn ${num(p.turn_number ?? p.turn ?? "?")}`;
    case "planning.human_interject":
      return `you: ${String(p.content ?? "").slice(0, 140)}`;
    case "planning.task_proposed":
      return `proposed: ${String(p.title ?? "?").slice(0, 120)}`;
    case "planning.task_refined":
      return `refined: ${String(p.title ?? "?").slice(0, 120)}`;
    case "planning.session_stable":
      return "session stable — ready to approve";
    case "planning.tasks_approved":
      return `tasks approved${p.count != null ? ` (${num(p.count)})` : ""}`;
    case "planning.session_completed":
      return "session completed";
    case "planning.session_cancelled":
      return "session cancelled";
    case "planning.error":
      return `error: ${String(p.error ?? p.message ?? "?").slice(0, 140)}`;

    // misc
    case "postmortem.draft":
      return "postmortem drafted";
    case "usage.recorded": {
      const inT = p.input_tokens ?? p.in;
      const outT = p.output_tokens ?? p.out;
      if (inT != null || outT != null) return `usage · ↓${num(inT ?? 0)} ↑${num(outT ?? 0)}`;
      return "usage recorded";
    }
    case "operator.steer":
      return `steer: ${String(p.message ?? p.text ?? "").slice(0, 140)}`;
    case "log":
      return `${p.stage ? `[${String(p.stage)}] ` : ""}${String(p.message ?? "")}`.trim();

    default:
      return Object.keys(p).length ? JSON.stringify(p).slice(0, 160) : "";
  }
}

// --- Connection status pill -------------------------------------------------

function StatusPill({ status }: { status: StreamStatus }) {
  const map: Record<StreamStatus, { label: string; dot: string; text: string; pulse: boolean }> = {
    live: { label: "Live", dot: "bg-emerald-400", text: "text-emerald-300", pulse: false },
    connecting: { label: "Connecting", dot: "bg-amber-400", text: "text-amber-300", pulse: true },
    reconnecting: { label: "Reconnecting", dot: "bg-amber-400", text: "text-amber-300", pulse: true },
  };
  const s = map[status];
  return (
    <span
      className={`inline-flex items-center gap-1.5 text-xs font-medium ${s.text}`}
      title={`Event stream: ${s.label.toLowerCase()}`}
    >
      <span className="relative flex h-2 w-2">
        {s.pulse && (
          <span className={`absolute inline-flex h-full w-full animate-ping rounded-full ${s.dot} opacity-60`} />
        )}
        <span className={`relative inline-flex h-2 w-2 rounded-full ${s.dot}`} />
      </span>
      {s.label}
    </span>
  );
}

// --- Single log row ---------------------------------------------------------

function LogRow({ ev }: { ev: EventRow }) {
  const style = styleFor(ev);
  const summary = summarize(ev);
  return (
    <div
      className={`flex gap-2 border-l-2 py-0.5 pl-2 leading-relaxed transition-colors hover:bg-zinc-800/40 ${
        style.accent ?? "border-transparent"
      }`}
    >
      <span className="w-[68px] shrink-0 tabular-nums text-zinc-600" title={ev.ts}>
        {ev.ts.slice(11, 19)}
      </span>
      <span className={`w-[150px] shrink-0 truncate font-medium ${style.type}`} title={ev.type}>
        {ev.type}
      </span>
      <span className="w-[88px] shrink-0 truncate text-zinc-500" title={ev.agent ?? ""}>
        {ev.agent ? `[${ev.agent}]` : ""}
      </span>
      <span className={`min-w-0 flex-1 break-words ${style.body}`} title={summary}>
        {summary}
      </span>
    </div>
  );
}

// --- Live panel -------------------------------------------------------------

export function LivePanel({ projectId }: { projectId: string }) {
  const { events, status } = useStream(projectId);
  const [filter, setFilter] = useState("");
  const [pinned, setPinned] = useState(true); // autoscroll on while at bottom
  const scrollerRef = useRef<HTMLDivElement>(null);
  const endRef = useRef<HTMLDivElement>(null);

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    const base = q
      ? events.filter(
          (e) =>
            e.type.toLowerCase().includes(q) ||
            (e.agent ?? "").toLowerCase().includes(q) ||
            summarize(e).toLowerCase().includes(q),
        )
      : events;
    // Cap the mounted rows to the most recent slice for performance.
    return base.length > MAX_VISIBLE_ROWS ? base.slice(-MAX_VISIBLE_ROWS) : base;
  }, [events, filter]);

  const hiddenCount = events.length - filtered.length;
  // Track the newest event id we've actually scrolled to, to count "unseen".
  const latestId = events.length ? events[events.length - 1].id : null;
  const seenIdRef = useRef<number | null>(null);
  if (pinned) seenIdRef.current = latestId;
  const unseen = useMemo(() => {
    if (pinned || seenIdRef.current == null) return 0;
    let n = 0;
    for (let i = events.length - 1; i >= 0; i--) {
      if (events[i].id === seenIdRef.current) break;
      n++;
    }
    return n;
  }, [events, pinned]);

  // Autoscroll to newest only while pinned.
  useEffect(() => {
    if (pinned) endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [filtered, pinned]);

  // Watch scroll position: leaving the bottom unpins; returning re-pins.
  const onScroll = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    setPinned(distance <= STICK_THRESHOLD_PX);
  }, []);

  const jumpToLatest = useCallback(() => {
    setPinned(true);
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, []);

  return (
    <div className="relative flex h-[70vh] flex-col overflow-hidden rounded-xl border border-zinc-800 bg-zinc-950">
      {/* Header: status, filter, counts */}
      <div className="flex items-center gap-3 border-b border-zinc-800 bg-zinc-900/60 px-3 py-2">
        <StatusPill status={status} />
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter events…"
          title="Filter by type, agent, or summary text"
          className="h-7 w-56 rounded-md border border-zinc-800 bg-zinc-900 px-2 text-xs text-zinc-100 outline-none transition-colors placeholder:text-zinc-600 focus-visible:border-indigo-500 focus-visible:ring-1 focus-visible:ring-indigo-500/40"
        />
        <span className="ml-auto tabular-nums text-xs text-zinc-500">
          {filter.trim()
            ? `${filtered.length} / ${events.length} events`
            : `${events.length} event${events.length === 1 ? "" : "s"}`}
        </span>
      </div>

      {/* Log body */}
      <div
        ref={scrollerRef}
        onScroll={onScroll}
        className="flex-1 overflow-y-auto px-2 py-2 font-mono text-xs"
      >
        {events.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center gap-1 text-center">
            <div className="text-sm text-zinc-500">Waiting for events…</div>
            <div className="text-xs text-zinc-600">Create or approve a task to see the live stream.</div>
          </div>
        ) : filtered.length === 0 ? (
          <div className="flex h-full items-center justify-center text-sm text-zinc-500">
            No events match “{filter.trim()}”.
          </div>
        ) : (
          <>
            {hiddenCount > 0 && !filter.trim() && (
              <div className="px-2 pb-1 text-[11px] text-zinc-600">
                showing last {filtered.length} of {events.length}
              </div>
            )}
            {filtered.map((ev) => (
              <LogRow key={ev.id} ev={ev} />
            ))}
            <div ref={endRef} />
          </>
        )}
      </div>

      {/* Jump-to-latest affordance (only while scrolled up) */}
      {!pinned && events.length > 0 && (
        <button
          type="button"
          onClick={jumpToLatest}
          title="Scroll to newest events and resume autoscroll"
          className="absolute bottom-4 left-1/2 -translate-x-1/2 rounded-full border border-indigo-400/40 bg-indigo-600/90 px-3 py-1 text-xs font-medium text-white shadow-lg backdrop-blur transition-colors hover:bg-indigo-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-indigo-400"
        >
          ↓ Jump to latest{unseen > 0 ? ` (${unseen})` : ""}
        </button>
      )}
    </div>
  );
}
