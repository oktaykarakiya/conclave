import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { EventRow } from "../types";
import { Button, Spinner, fmtTime } from "../ui";
import type { StreamStatus } from "../useStream";
import { useStream } from "../useStream";

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

// Pretty-printed full payload, shown when a row is expanded.
function fullPayload(ev: EventRow): string {
  const p = ev.payload ?? {};
  try {
    return Object.keys(p).length ? JSON.stringify(p, null, 2) : "(no payload)";
  } catch {
    return String(p);
  }
}

// Whether a row is worth expanding: it has payload keys to inspect.
function isExpandable(ev: EventRow): boolean {
  return Object.keys(ev.payload ?? {}).length > 0;
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
  const expandable = isExpandable(ev);
  const [expanded, setExpanded] = useState(false);

  return (
    <div
      className={`group border-l-2 py-0.5 pl-2 leading-relaxed transition-colors hover:bg-zinc-800/40 ${
        style.accent ?? "border-transparent"
      }`}
    >
      {/* Row 1 on mobile (time + type + chevron); single line on >=sm. */}
      <div className="flex items-start gap-2">
        <span
          className="w-[58px] shrink-0 tabular-nums text-zinc-600 sm:w-[68px]"
          title={ev.ts}
        >
          {fmtTime(ev.ts)}
        </span>
        <span
          className={`max-w-[42%] shrink truncate font-medium sm:w-[150px] sm:max-w-none sm:shrink-0 ${style.type}`}
          title={ev.type}
        >
          {ev.type}
        </span>
        {/* Agent: hidden on mobile to protect the summary column. */}
        <span
          className="hidden w-[88px] shrink-0 truncate text-zinc-500 sm:inline-block"
          title={ev.agent ?? ""}
        >
          {ev.agent ? `[${ev.agent}]` : ""}
        </span>
        {/* Summary: single-line clamp by default; full text on expand. */}
        <span
          className={`min-w-0 flex-1 ${expanded ? "whitespace-pre-wrap break-words" : "truncate"} ${style.body}`}
          title={summary}
        >
          {summary || (expandable ? "(payload)" : "")}
        </span>
        {expandable && (
          <button
            type="button"
            onClick={() => setExpanded((e) => !e)}
            aria-label={expanded ? "Collapse payload" : "Expand payload"}
            aria-expanded={expanded}
            title={expanded ? "Collapse payload" : "Expand full payload"}
            className="ml-auto flex h-6 w-6 shrink-0 items-center justify-center rounded text-zinc-600 transition-colors hover:bg-zinc-800 hover:text-zinc-300 focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-indigo-400 focus-visible:outline-none sm:opacity-0 sm:group-hover:opacity-100"
          >
            <svg
              viewBox="0 0 20 20"
              fill="currentColor"
              aria-hidden="true"
              className={`h-4 w-4 transition-transform duration-200 ${expanded ? "rotate-180" : ""}`}
            >
              <path
                fillRule="evenodd"
                d="M5.23 7.21a.75.75 0 0 1 1.06.02L10 11.168l3.71-3.938a.75.75 0 1 1 1.08 1.04l-4.25 4.5a.75.75 0 0 1-1.08 0l-4.25-4.5a.75.75 0 0 1 .02-1.06Z"
                clipRule="evenodd"
              />
            </svg>
          </button>
        )}
      </div>
      {/* Mobile agent line + expanded payload. */}
      {expanded && (
        <pre className="mt-1 ml-[58px] max-h-48 overflow-auto rounded-md border border-zinc-800 bg-zinc-900/70 p-2 text-[11px] whitespace-pre-wrap text-zinc-400 sm:ml-[68px]">
          {ev.agent ? <span className="mb-1 block text-zinc-500 sm:hidden">[{ev.agent}]</span> : null}
          {fullPayload(ev)}
        </pre>
      )}
    </div>
  );
}

// --- Loading skeleton (initial socket connect) ------------------------------

function LogSkeleton() {
  // Varied widths so the placeholder reads as "lines of log" rather than bars.
  const widths = ["w-3/4", "w-1/2", "w-5/6", "w-2/3", "w-4/5", "w-1/3"];
  return (
    <div className="space-y-2 px-2 py-2" aria-busy="true" aria-label="Connecting to event stream">
      <div className="flex items-center gap-2 pb-1 text-[11px] text-zinc-500">
        <Spinner size={12} />
        Connecting to event stream…
      </div>
      {widths.map((w, i) => (
        <div key={i} className="flex items-center gap-2">
          <div className="h-3 w-[58px] shrink-0 animate-pulse rounded bg-zinc-800 sm:w-[68px]" />
          <div className="h-3 w-[110px] shrink-0 animate-pulse rounded bg-zinc-800/80" />
          <div className={`h-3 ${w} animate-pulse rounded bg-zinc-800/60`} />
        </div>
      ))}
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

  const hasFilter = filter.trim().length > 0;
  // Whether the visible list is a truncated tail of the (filtered or full) set.
  const capped = useMemo(() => {
    const q = filter.trim().toLowerCase();
    const matchedTotal = q
      ? events.filter(
          (e) =>
            e.type.toLowerCase().includes(q) ||
            (e.agent ?? "").toLowerCase().includes(q) ||
            summarize(e).toLowerCase().includes(q),
        ).length
      : events.length;
    return matchedTotal > filtered.length;
  }, [events, filter, filtered.length]);

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

  const connecting = status === "connecting" && events.length === 0;

  return (
    <div className="flex flex-col gap-2">
      {/* Header: status, filter, counts. Wraps to two rows on narrow screens. */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <StatusPill status={status} />
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter events…"
          title="Filter by type, agent, or summary text"
          aria-label="Filter events"
          className="h-9 min-w-0 flex-1 rounded-md border border-zinc-800 bg-zinc-900 px-3 text-sm text-zinc-100 outline-none transition-colors placeholder:text-zinc-600 focus-visible:border-indigo-500 focus-visible:ring-1 focus-visible:ring-indigo-500/40 sm:h-7 sm:text-xs"
        />
        <span className="shrink-0 tabular-nums text-xs text-zinc-500">
          {hasFilter
            ? `${filtered.length} / ${events.length}`
            : `${events.length} event${events.length === 1 ? "" : "s"}`}
        </span>
      </div>

      {/* Log body — a self-contained, capped-height scroller (no page-scroll trap). */}
      <div
        ref={scrollerRef}
        onScroll={onScroll}
        className="relative max-h-[60vh] overflow-y-auto rounded-lg border border-zinc-800 bg-zinc-950 px-1 py-1 font-mono text-xs sm:max-h-[480px]"
      >
        {connecting ? (
          <LogSkeleton />
        ) : events.length === 0 ? (
          <div className="flex flex-col items-center justify-center gap-2 px-4 py-12 text-center">
            <StatusPill status={status} />
            <div className="text-sm text-zinc-500">
              {status === "reconnecting" ? "Reconnecting to the event stream…" : "No events yet"}
            </div>
            <div className="text-xs text-zinc-600">
              Create or approve a task to see activity here.
            </div>
          </div>
        ) : filtered.length === 0 ? (
          <div className="flex items-center justify-center px-4 py-12 text-center text-sm text-zinc-500">
            No events match “{filter.trim()}”.
          </div>
        ) : (
          <>
            {capped && (
              <div className="px-2 pb-1 text-[11px] text-zinc-600">
                {hasFilter
                  ? `Showing last ${filtered.length} matches of ${events.length} events`
                  : `Showing last ${filtered.length} of ${events.length} events`}
              </div>
            )}
            {filtered.map((ev) => (
              <LogRow key={ev.id} ev={ev} />
            ))}
            <div ref={endRef} />

            {/* Jump-to-latest: sticky inside the scroller so it never escapes bounds. */}
            {!pinned && (
              <div className="pointer-events-none sticky bottom-2 flex justify-center">
                <span className="pointer-events-auto">
                  <Button
                    variant="primary"
                    onClick={jumpToLatest}
                    title="Scroll to newest events and resume autoscroll"
                  >
                    ↓ Jump to latest{unseen > 0 ? ` (${unseen})` : ""}
                  </Button>
                </span>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
