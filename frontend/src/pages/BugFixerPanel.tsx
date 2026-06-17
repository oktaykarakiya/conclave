import type React from "react";
import { useCallback, useEffect, useState } from "react";

import { api } from "../api";
import type { BugCandidate, BugStatus, Project, ProjectMode } from "../types";
import { Spinner } from "../ui";

// The ledger polls on this cadence (ms) — same rhythm as the Tasks list.
const POLL_MS = 4000;

// Claims/notes longer than this collapse to a preview with a "Show more" toggle,
// matching the global collapsible-long-text mandate used across the app.
const TEXT_COLLAPSE_THRESHOLD = 140;

/* ------------------------------------------------------------------------ */
/* Status + severity styling                                                */
/* ------------------------------------------------------------------------ */

// Tinted status pills (ring style), calmer than the solid shared Badge — one
// entry per BugStatus value so the ledger reads at a glance.
const STATUS_TONE: Record<BugStatus, string> = {
  discovered: "bg-zinc-700/40 text-zinc-300 ring-zinc-600/50",
  reproduced: "bg-sky-500/10 text-sky-300 ring-sky-500/30",
  fixing: "bg-amber-500/10 text-amber-300 ring-amber-500/30",
  fixed: "bg-emerald-500/10 text-emerald-300 ring-emerald-500/30",
  dismissed_false_positive: "bg-zinc-700/40 text-zinc-400 ring-zinc-600/40",
  declined_needs_human: "bg-rose-500/10 text-rose-300 ring-rose-500/30",
  deferred: "bg-violet-500/10 text-violet-300 ring-violet-500/30",
};

// Human-friendly status labels (the raw enum values are snake_case + verbose).
const STATUS_LABEL: Record<BugStatus, string> = {
  discovered: "discovered",
  reproduced: "reproduced",
  fixing: "fixing",
  fixed: "fixed",
  dismissed_false_positive: "false positive",
  declined_needs_human: "needs human",
  deferred: "deferred",
};

function StatusPill({ status }: { status: BugStatus }) {
  const tone = STATUS_TONE[status] ?? STATUS_TONE.discovered;
  const label = STATUS_LABEL[status] ?? status;
  const pulse = status === "fixing" ? "animate-pulse" : "";
  return (
    <span
      title={status}
      className={`inline-flex shrink-0 items-center gap-1.5 rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${tone}`}
    >
      {status === "fixing" && (
        <span className={`h-1.5 w-1.5 rounded-full bg-amber-400 ${pulse}`} aria-hidden="true" />
      )}
      {label}
    </span>
  );
}

// Severity is free-text from the model, but the common levels get a color.
function severityClass(sev: string): string {
  switch (sev.toLowerCase()) {
    case "critical":
    case "high":
      return "text-rose-300";
    case "medium":
      return "text-amber-300";
    case "low":
      return "text-zinc-400";
    default:
      return "text-zinc-400";
  }
}

/** file::symbol, with sensible fallbacks when one side is missing. */
function locationLabel(c: BugCandidate): string {
  if (c.file && c.symbol) return `${c.file}::${c.symbol}`;
  return c.file ?? c.symbol ?? "—";
}

/* ------------------------------------------------------------------------ */
/* Panel                                                                    */
/* ------------------------------------------------------------------------ */

export function BugFixerPanel({ project }: { project: Project }) {
  const projectId = project.id;

  // Optimistic local mode, seeded from the project. setMode returns the updated
  // project so we reconcile to the server's truth on success.
  const [mode, setMode] = useState<ProjectMode>(
    project.mode === "autonomous_bug_fixer" ? "autonomous_bug_fixer" : "task_queue",
  );
  const [switching, setSwitching] = useState(false);
  const [modeError, setModeError] = useState("");

  const [candidates, setCandidates] = useState<BugCandidate[]>([]);
  const [needsHuman, setNeedsHuman] = useState<BugCandidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");

  // Keep local mode in sync if the parent swaps in a fresh project object.
  useEffect(() => {
    setMode(
      project.mode === "autonomous_bug_fixer" ? "autonomous_bug_fixer" : "task_queue",
    );
  }, [project.mode]);

  const reload = useCallback(async () => {
    setLoadError("");
    try {
      const [ledger, human] = await Promise.all([
        api.bugCandidates(projectId),
        api.needsHuman(projectId),
      ]);
      setCandidates(ledger);
      setNeedsHuman(human);
    } catch (e) {
      setLoadError(String(e));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  // Reset + poll whenever the project changes.
  useEffect(() => {
    setLoading(true);
    setCandidates([]);
    setNeedsHuman([]);
    reload();
    const id = setInterval(reload, POLL_MS);
    return () => clearInterval(id);
  }, [reload]);

  async function switchMode(next: ProjectMode) {
    if (switching || next === mode) return;
    setSwitching(true);
    setModeError("");
    const prev = mode;
    setMode(next); // optimistic
    try {
      const updated = await api.setMode(projectId, next);
      const confirmed =
        updated.mode === "autonomous_bug_fixer" ? "autonomous_bug_fixer" : "task_queue";
      setMode(confirmed);
      // A switch into bug-fixer often starts populating the ledger — refresh now.
      void reload();
    } catch (e) {
      setMode(prev); // roll back the optimistic flip
      setModeError(String(e));
    } finally {
      setSwitching(false);
    }
  }

  const bugFixerActive = mode === "autonomous_bug_fixer";

  return (
    <div className="flex h-full min-h-0 flex-col gap-4">
      {/* Fixed top region: intro + mode toggle */}
      <div className="shrink-0 space-y-4">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-300">
              Autonomous Bug-Fixer
            </h2>
            <p className="mt-0.5 max-w-prose text-xs text-zinc-500">
              In bug-fixer mode the team continuously hunts, reproduces and repairs bugs on
              its own. The ledger below tracks every candidate through its lifecycle.
            </p>
          </div>
          <ModeToggle
            mode={mode}
            switching={switching}
            onSwitch={switchMode}
          />
        </div>

        {modeError && (
          <div
            role="alert"
            className="rounded-xl border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-300"
          >
            {modeError}
          </div>
        )}

        {!bugFixerActive && (
          <div className="rounded-xl border border-dashed border-zinc-800 bg-zinc-900/40 px-4 py-3 text-xs text-zinc-500">
            This project is in <span className="font-medium text-zinc-300">task-queue</span>{" "}
            mode. The ledger still shows any candidates found while the Bug-Fixer was last
            active — switch to{" "}
            <span className="font-medium text-zinc-300">bug-fixer</span> mode to resume the
            autonomous hunt.
          </div>
        )}
      </div>

      {/* Scroll region: needs-human queue + ledger */}
      <div className="-mr-1 min-h-0 flex-1 space-y-5 overflow-y-auto pr-1">
        <NeedsHumanSection items={needsHuman} loading={loading} />

        <section className="space-y-2">
          <div className="flex items-center justify-between gap-2">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-zinc-400">
              Ledger
            </h3>
            {!loading && (
              <span className="text-xs tabular-nums text-zinc-600">
                {candidates.length} candidate{candidates.length === 1 ? "" : "s"}
              </span>
            )}
          </div>

          {loading ? (
            <LedgerSkeleton />
          ) : loadError ? (
            <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-300">
              {loadError}
            </div>
          ) : candidates.length === 0 ? (
            <div className="rounded-xl border border-dashed border-zinc-800 bg-zinc-900/40 py-12 text-center">
              <div className="text-sm font-medium text-zinc-400">No bug candidates yet</div>
              <div className="mt-1 px-4 text-xs text-zinc-500">
                {bugFixerActive
                  ? "The Bug-Fixer hasn't surfaced anything yet — give it a moment."
                  : "Nothing has been discovered. Switch to bug-fixer mode to start hunting."}
              </div>
            </div>
          ) : (
            <LedgerTable rows={candidates} />
          )}
        </section>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Mode toggle                                                              */
/* ------------------------------------------------------------------------ */

function ModeToggle({
  mode,
  switching,
  onSwitch,
}: {
  mode: ProjectMode;
  switching: boolean;
  onSwitch: (next: ProjectMode) => void;
}) {
  const options: { key: ProjectMode; label: string; title: string }[] = [
    {
      key: "task_queue",
      label: "Task queue",
      title: "Execute operator-created tasks from the queue",
    },
    {
      key: "autonomous_bug_fixer",
      label: "Bug-fixer",
      title: "Continuously hunt, reproduce and fix bugs autonomously",
    },
  ];

  return (
    <div
      role="group"
      aria-label="Project mode"
      className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-zinc-800 bg-zinc-900 p-1"
    >
      {options.map((o) => {
        const active = mode === o.key;
        return (
          <button
            key={o.key}
            type="button"
            title={o.title}
            aria-pressed={active}
            disabled={switching}
            onClick={() => onSwitch(o.key)}
            className={`flex min-h-[36px] items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-indigo-500/50 disabled:opacity-50 ${
              active
                ? "bg-indigo-600 text-white"
                : "text-zinc-400 hover:bg-zinc-800 hover:text-zinc-200"
            }`}
          >
            {active && switching && <Spinner size={12} />}
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Needs-human section                                                      */
/* ------------------------------------------------------------------------ */

function NeedsHumanSection({
  items,
  loading,
}: {
  items: BugCandidate[];
  loading: boolean;
}) {
  // Hide entirely until we have data — an empty review queue is the happy path
  // and shouldn't take up space.
  if (loading || items.length === 0) return null;

  return (
    <section className="space-y-2 rounded-xl border border-rose-500/30 bg-rose-500/5 p-3 sm:p-4">
      <div className="flex items-center gap-2">
        <span className="h-1.5 w-1.5 rounded-full bg-rose-400" aria-hidden="true" />
        <h3 className="text-xs font-semibold uppercase tracking-wide text-rose-300">
          Needs human
        </h3>
        <span className="tabular-nums text-xs text-rose-400/70">{items.length}</span>
      </div>
      <p className="text-xs text-rose-200/70">
        The Bug-Fixer exhausted its auto-fix attempts on these — they're parked for an
        operator decision.
      </p>
      <div className="space-y-2">
        {items.map((c) => (
          <div
            key={c.id}
            className="rounded-lg border border-rose-500/20 bg-zinc-950/40 p-3"
          >
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <StatusPill status={c.status} />
              {c.severity && (
                <span className={`text-xs font-medium ${severityClass(c.severity)}`}>
                  {c.severity}
                </span>
              )}
              <span
                className="min-w-0 flex-1 truncate font-mono text-xs text-zinc-300"
                title={locationLabel(c)}
              >
                {locationLabel(c)}
              </span>
              {c.attempts > 0 && (
                <span className="shrink-0 text-xs tabular-nums text-zinc-500">
                  {c.attempts} attempt{c.attempts === 1 ? "" : "s"}
                </span>
              )}
            </div>
            <CollapsibleText text={c.claim} className="mt-1.5 text-xs text-zinc-300" />
            {c.decline_reason && (
              <CollapsibleText
                text={c.decline_reason}
                className="mt-1 text-xs text-rose-300/80"
                prefix="Reason: "
              />
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

/* ------------------------------------------------------------------------ */
/* Ledger table                                                             */
/* ------------------------------------------------------------------------ */

function LedgerTable({ rows }: { rows: BugCandidate[] }) {
  return (
    <div className="overflow-hidden rounded-xl border border-zinc-800">
      {/* Desktop: a real table. Mobile: stacked cards (table layout cramps on
          narrow viewports, so we hide it and render cards below). */}
      <table className="hidden w-full table-fixed border-collapse text-sm sm:table">
        <thead>
          <tr className="border-b border-zinc-800 bg-zinc-900/60 text-left text-xs uppercase tracking-wide text-zinc-500">
            <th className="w-32 px-3 py-2 font-medium">Status</th>
            <th className="w-20 px-3 py-2 font-medium">Severity</th>
            <th className="px-3 py-2 font-medium">Location · Claim</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((c) => (
            <tr
              key={c.id}
              className="border-b border-zinc-800/60 align-top last:border-b-0 hover:bg-zinc-800/30"
            >
              <td className="px-3 py-2.5">
                <StatusPill status={c.status} />
              </td>
              <td className="px-3 py-2.5">
                <span
                  className={`text-xs font-medium ${
                    c.severity ? severityClass(c.severity) : "text-zinc-600"
                  }`}
                >
                  {c.severity ?? "—"}
                </span>
              </td>
              <td className="px-3 py-2.5">
                <div
                  className="truncate font-mono text-xs text-zinc-300"
                  title={locationLabel(c)}
                >
                  {locationLabel(c)}
                </div>
                <CollapsibleText
                  text={c.claim}
                  className="mt-1 text-xs text-zinc-400"
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      {/* Mobile cards */}
      <div className="divide-y divide-zinc-800/60 sm:hidden">
        {rows.map((c) => (
          <div key={c.id} className="bg-zinc-900/40 p-3">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <StatusPill status={c.status} />
              {c.severity && (
                <span className={`text-xs font-medium ${severityClass(c.severity)}`}>
                  {c.severity}
                </span>
              )}
            </div>
            <div
              className="mt-1.5 truncate font-mono text-xs text-zinc-300"
              title={locationLabel(c)}
            >
              {locationLabel(c)}
            </div>
            <CollapsibleText text={c.claim} className="mt-1 text-xs text-zinc-400" />
          </div>
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Shared bits                                                              */
/* ------------------------------------------------------------------------ */

/**
 * Long free-text with a collapsed preview + "Show more" / "Show less" toggle.
 * Mirrors the pattern used in TasksPanel / AgentCeptionPanel.
 */
function CollapsibleText({
  text,
  className = "",
  prefix = "",
}: {
  text: string;
  className?: string;
  prefix?: string;
}) {
  const [open, setOpen] = useState(false);
  const long = text.length > TEXT_COLLAPSE_THRESHOLD;
  const shown = long && !open ? `${text.slice(0, TEXT_COLLAPSE_THRESHOLD).trimEnd()}…` : text;

  return (
    <div className={className}>
      {prefix && <span className="text-zinc-500">{prefix}</span>}
      <span className="break-words whitespace-pre-wrap">{shown}</span>
      {long && (
        <button
          type="button"
          aria-expanded={open}
          onClick={(e: React.MouseEvent) => {
            e.stopPropagation();
            setOpen((v) => !v);
          }}
          className="ml-1 inline-flex min-h-[28px] items-center font-medium text-indigo-400 hover:text-indigo-300 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-indigo-500/50"
        >
          {open ? "Show less" : "Show more"}
        </button>
      )}
    </div>
  );
}

function LedgerSkeleton() {
  return (
    <div
      className="space-y-1.5"
      aria-busy="true"
      aria-label="Loading bug candidates"
    >
      {Array.from({ length: 4 }).map((_, i) => (
        <div
          key={i}
          className="animate-pulse rounded-xl border border-zinc-800 bg-zinc-900 p-3"
        >
          <div className="flex items-center gap-2">
            <div className="h-4 w-20 rounded bg-zinc-800" />
            <div className="h-4 w-12 rounded bg-zinc-800/70" />
            <div className="h-4 flex-1 rounded bg-zinc-800/50" />
          </div>
          <div className="mt-2 h-3 w-2/3 rounded bg-zinc-800/50" />
        </div>
      ))}
    </div>
  );
}
