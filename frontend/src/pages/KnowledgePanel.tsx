import type React from "react";
import { useCallback, useEffect, useState } from "react";

import { api } from "../api";
import type { RepoKnowledge } from "../types";
import { Button, Spinner } from "../ui";

export function KnowledgePanel({ projectId }: { projectId: string }) {
  const [knowledge, setKnowledge] = useState<RepoKnowledge | null>(null);
  const [loading, setLoading] = useState(true);
  const [analyzing, setAnalyzing] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const k = await api.getKnowledge(projectId);
      setKnowledge(k);
    } catch (e) {
      // F7: don't wipe previously-loaded content on a failed (re)load.
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    load();
  }, [load]);

  async function runAi() {
    setAnalyzing(true);
    setError("");
    try {
      const k = await api.aiAnalyze(projectId);
      setKnowledge(k);
    } catch (e) {
      setError(String(e));
    } finally {
      setAnalyzing(false);
    }
  }

  if (loading) return <KnowledgeSkeleton />;

  // FE-2 fix: normalize the payload. The API returns `{}` before onboarding,
  // which the old `if (!knowledge)` guard let through and then crashed on
  // `.languages.length`. Read every field defensively.
  const languages = knowledge?.languages ?? [];
  const frameworks = knowledge?.frameworks ?? [];
  const commands = knowledge?.commands ?? {};
  const conventions = knowledge?.conventions ?? [];
  const protectedGlobs = knowledge?.protected_globs ?? [];
  const layout = knowledge?.layout ?? {};
  const architecture = knowledge?.architecture_summary ?? "";
  const aiEnriched = knowledge?.ai_enriched ?? false;

  const layoutGroups = Object.entries(layout).filter(([, dirs]) => (dirs ?? []).length > 0);
  const commandEntries = Object.entries(commands);

  const isEmpty =
    !knowledge ||
    (languages.length === 0 &&
      frameworks.length === 0 &&
      commandEntries.length === 0 &&
      conventions.length === 0 &&
      protectedGlobs.length === 0 &&
      layoutGroups.length === 0 &&
      architecture.trim() === "");

  // F7: full-screen error layout only when there is nothing to show. If we have
  // stale knowledge, the error renders as a dismissible banner above the content.
  if (error && isEmpty) {
    return (
      <div className="flex h-full min-h-0 flex-col space-y-4 overflow-y-auto">
        <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-200">
          <div className="font-semibold text-rose-100">Failed to load repository knowledge</div>
          <div className="mt-1 break-words text-rose-200/90">{error}</div>
        </div>
        <Button variant="ghost" onClick={load} title="Retry loading repository knowledge">
          Retry
        </Button>
      </div>
    );
  }

  if (isEmpty) {
    return (
      <div className="flex h-full min-h-0 flex-col items-center justify-center overflow-y-auto rounded-xl border border-zinc-800 bg-zinc-900 px-6 py-12 text-center">
        <div className="mb-3 text-3xl text-zinc-700" aria-hidden="true">
          ◇
        </div>
        <div className="text-sm font-semibold text-zinc-200">No repository knowledge yet</div>
        <p className="mt-2 max-w-md text-sm text-zinc-400">
          Run AI analysis to scan the codebase and detect its languages, frameworks, commands
          and conventions.
        </p>
        <div className="mt-5 w-full sm:w-auto">
          <AnalyzeButton analyzing={analyzing} onClick={runAi} label="Run AI analysis" />
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Fixed header region: error banner, summary/actions, progress. */}
      <div className="shrink-0 space-y-4">
      {/* F7: non-blocking error banner — preserves the stale content below. */}
      {error && (
        <div className="flex items-start gap-3 rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-200">
          <div className="min-w-0 flex-1">
            <span className="font-semibold text-rose-100">Couldn’t refresh knowledge. </span>
            <span className="break-words text-rose-200/90">{error}</span>
          </div>
          <button
            type="button"
            onClick={() => setError("")}
            title="Dismiss"
            aria-label="Dismiss error"
            className="-m-1.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-md text-rose-300 transition-colors hover:bg-rose-500/15 hover:text-rose-100 focus-visible:ring-2 focus-visible:ring-rose-400 focus-visible:outline-none"
          >
            <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" className="h-4 w-4">
              <path d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z" />
            </svg>
          </button>
        </div>
      )}

      {/* F3: header wraps on narrow viewports; button goes full-width on mobile. */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <Pill
            tone={aiEnriched ? "emerald" : "amber"}
            label={aiEnriched ? "AI-enriched" : "Heuristic only"}
          />
          <span className="text-xs text-zinc-400">
            {languages.length} language{languages.length === 1 ? "" : "s"} ·{" "}
            {frameworks.length} framework{frameworks.length === 1 ? "" : "s"} ·{" "}
            {commandEntries.length} command{commandEntries.length === 1 ? "" : "s"}
          </span>
        </div>
        <div className="ml-auto w-full sm:w-auto">
          <AnalyzeButton
            analyzing={analyzing}
            onClick={runAi}
            label={aiEnriched ? "Re-run AI analysis" : "Run AI analysis"}
          />
        </div>
      </div>

      {/* F6: progress banner while the (slow) AI pass runs. */}
      {analyzing && (
        <div className="flex items-center gap-2 rounded-xl border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-200">
          <Spinner size={14} />
          <span>AI analysis running — this may take a moment…</span>
        </div>
      )}
      </div>

      {/* Scrollable knowledge sections — the page never grows past the viewport. */}
      <div className="mt-4 min-h-0 flex-1 space-y-4 overflow-y-auto">
      {/* Architecture summary (F2: collapsed by default with show more/less). */}
      {architecture.trim() !== "" && (
        <Section title="Architecture">
          <CollapsibleText text={architecture} />
        </Section>
      )}

      {/* Languages & Frameworks */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <Section title="Languages">
          <ChipList items={languages} tone="sky" empty="None detected" />
        </Section>
        <Section title="Frameworks">
          <ChipList items={frameworks} tone="violet" empty="None detected" />
        </Section>
      </div>

      {/* Commands (F10: copy affordance + mobile-friendly wrapping) */}
      <Section title="Commands">
        {commandEntries.length > 0 ? (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {commandEntries.map(([key, val]) => (
              <CommandRow key={key} label={key} value={val} />
            ))}
          </div>
        ) : (
          <EmptyNote>No commands detected</EmptyNote>
        )}
      </Section>

      {/* Conventions (F4: capped at 5 with show all/less) */}
      <Section title="Conventions">
        {conventions.length > 0 ? (
          <ConventionList items={conventions} />
        ) : (
          <EmptyNote>No conventions detected</EmptyNote>
        )}
      </Section>

      {/* Layout & Protected globs */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <Section title="Layout">
          {layoutGroups.length > 0 ? (
            <div className="space-y-3">
              {layoutGroups.map(([group, dirs]) => (
                <div key={group}>
                  {/* F12: zinc-400 meets WCAG AA on zinc-900. */}
                  <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-zinc-400">
                    {group}
                  </div>
                  <ChipList items={dirs ?? []} tone="zinc" empty="—" mono />
                </div>
              ))}
            </div>
          ) : (
            <EmptyNote>—</EmptyNote>
          )}
        </Section>
        <Section title="Protected globs">
          {protectedGlobs.length > 0 ? (
            <div className="flex flex-wrap gap-2">
              {protectedGlobs.map((g) => (
                // F14: wrap (break-all) on mobile so the full pattern is readable;
                // truncate only from sm: up, where the title tooltip is reachable.
                <code
                  key={g}
                  className="max-w-full rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1 font-mono text-xs break-all text-amber-300 sm:truncate sm:break-normal"
                  title={g}
                >
                  {g}
                </code>
              ))}
            </div>
          ) : (
            <EmptyNote>—</EmptyNote>
          )}
        </Section>
      </div>
      </div>
    </div>
  );
}

// --- Page-local helpers ------------------------------------------------------

function slugify(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/(^-|-$)/g, "");
}

const SECTION_CARD = "rounded-xl border border-zinc-800 bg-zinc-900 p-4";
const SECTION_TITLE = "mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-300";

// Inner titled card. Note: the page itself already renders inside the shared
// accordion `<Section>` (App.tsx), so these are flat cards, not nested
// accordions — that would be one collapsible too many.
function Section({ title, children }: { title: string; children: React.ReactNode }) {
  const id = `knowledge-${slugify(title)}`;
  return (
    <section className={SECTION_CARD} aria-labelledby={id}>
      <h3 id={id} className={SECTION_TITLE}>
        {title}
      </h3>
      {children}
    </section>
  );
}

function EmptyNote({ children }: { children: React.ReactNode }) {
  return <span className="text-sm text-zinc-400">{children}</span>;
}

/** Primary AI-analysis button with an inline spinner while in flight (F6). */
function AnalyzeButton({
  analyzing,
  onClick,
  label,
}: {
  analyzing: boolean;
  onClick: () => void;
  label: string;
}) {
  return (
    <Button
      variant="primary"
      onClick={onClick}
      disabled={analyzing}
      title={analyzing ? "AI analysis in progress" : label}
    >
      <span className="flex w-full items-center justify-center gap-2">
        {analyzing && <Spinner size={14} />}
        {analyzing ? "Analyzing…" : label}
      </span>
    </Button>
  );
}

/** Long prose clamped to 3 lines with a show more/less toggle (F2). */
function CollapsibleText({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <p
        className={`text-sm leading-relaxed text-zinc-300 ${open ? "" : "line-clamp-3"}`}
      >
        {text}
      </p>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="mt-2 rounded text-xs font-medium text-indigo-400 transition-colors hover:text-indigo-300 focus-visible:ring-2 focus-visible:ring-indigo-400 focus-visible:outline-none"
      >
        {open ? "Show less" : "Show more"}
      </button>
    </div>
  );
}

const CONVENTION_PREVIEW = 5;

/** Bulleted conventions, capped to a preview with show all/less (F4). */
function ConventionList({ items }: { items: string[] }) {
  const [expanded, setExpanded] = useState(false);
  const visible = expanded ? items : items.slice(0, CONVENTION_PREVIEW);
  const hidden = items.length - CONVENTION_PREVIEW;
  return (
    <div>
      <ul className="space-y-1.5">
        {visible.map((c, i) => (
          <li key={i} className="flex gap-2 text-sm leading-snug text-zinc-300">
            <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-zinc-500" aria-hidden />
            <span>{c}</span>
          </li>
        ))}
      </ul>
      {hidden > 0 && (
        <button
          type="button"
          onClick={() => setExpanded((e) => !e)}
          aria-expanded={expanded}
          className="mt-2 rounded text-xs font-medium text-indigo-400 transition-colors hover:text-indigo-300 focus-visible:ring-2 focus-visible:ring-indigo-400 focus-visible:outline-none"
        >
          {expanded ? "Show less" : `Show all ${items.length} conventions`}
        </button>
      )}
    </div>
  );
}

/** One command row with a copy-to-clipboard affordance (F10). */
function CommandRow({ label, value }: { label: string; value: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      /* clipboard may be unavailable (insecure context) — fail silently */
    }
  }

  return (
    <div className="flex items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2">
      <span className="shrink-0 text-xs font-semibold uppercase tracking-wide text-zinc-400">
        {label}
      </span>
      <code
        className="min-w-0 flex-1 break-all font-mono text-sm text-emerald-300 sm:truncate sm:break-normal"
        title={value}
      >
        {value}
      </code>
      <button
        type="button"
        onClick={copy}
        title={copied ? "Copied" : "Copy command"}
        aria-label={copied ? "Copied" : `Copy ${label} command`}
        className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-zinc-400 transition-colors hover:bg-zinc-800 hover:text-zinc-200 focus-visible:ring-2 focus-visible:ring-indigo-400 focus-visible:outline-none"
      >
        {copied ? (
          <svg viewBox="0 0 20 20" fill="currentColor" aria-hidden="true" className="h-4 w-4 text-emerald-400">
            <path fillRule="evenodd" d="M16.7 5.3a1 1 0 0 1 0 1.4l-7.5 7.5a1 1 0 0 1-1.4 0l-3.5-3.5a1 1 0 1 1 1.4-1.4l2.8 2.79 6.8-6.79a1 1 0 0 1 1.4 0Z" clipRule="evenodd" />
          </svg>
        ) : (
          <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true" className="h-4 w-4">
            <rect x="7" y="7" width="9" height="9" rx="1.5" />
            <path d="M13 4.5H5A1.5 1.5 0 0 0 3.5 6v8" strokeLinecap="round" />
          </svg>
        )}
      </button>
    </div>
  );
}

// Soft-tinted chip tones (calmer than solid Badge fills — matches Linear/Vercel).
const CHIP_TONES: Record<string, string> = {
  sky: "border-sky-500/30 bg-sky-500/10 text-sky-300",
  violet: "border-violet-500/30 bg-violet-500/10 text-violet-300",
  emerald: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300",
  amber: "border-amber-500/30 bg-amber-500/10 text-amber-300",
  zinc: "border-zinc-700 bg-zinc-800/60 text-zinc-300",
};

function Chip({ label, tone = "zinc", mono = false }: { label: string; tone?: string; mono?: boolean }) {
  return (
    <span
      title={label}
      className={`inline-flex max-w-full items-center truncate rounded-md border px-2 py-0.5 text-xs font-medium ${
        mono ? "font-mono" : ""
      } ${CHIP_TONES[tone] ?? CHIP_TONES.zinc}`}
    >
      {label}
    </span>
  );
}

function ChipList({
  items,
  tone,
  empty,
  mono = false,
}: {
  items: string[];
  tone: string;
  empty: string;
  mono?: boolean;
}) {
  if (items.length === 0) return <EmptyNote>{empty}</EmptyNote>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((it) => (
        <Chip key={it} label={it} tone={tone} mono={mono} />
      ))}
    </div>
  );
}

// Status pill with a leading dot — used for the AI-enriched / heuristic badge.
const PILL_TONES: Record<string, { dot: string; box: string }> = {
  emerald: { dot: "bg-emerald-400", box: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300" },
  amber: { dot: "bg-amber-400", box: "border-amber-500/30 bg-amber-500/10 text-amber-300" },
};

function Pill({ label, tone }: { label: string; tone: "emerald" | "amber" }) {
  const t = PILL_TONES[tone];
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium ${t.box}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${t.dot}`} aria-hidden />
      {label}
    </span>
  );
}

function KnowledgeSkeleton() {
  // F8: use min-h (not fixed h-) so blocks stretch to real content and don't
  // snap-shrink on arrival, and mirror the real section count more closely.
  return (
    <div
      className="flex h-full min-h-0 flex-col space-y-4 overflow-y-auto"
      aria-busy="true"
      aria-live="polite"
    >
      <span className="sr-only">Loading repository knowledge…</span>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="h-6 w-32 animate-pulse rounded-full bg-zinc-800" />
        <div className="h-8 w-36 animate-pulse rounded bg-zinc-800" />
      </div>
      <div className="min-h-[6rem] animate-pulse rounded-xl border border-zinc-800 bg-zinc-900" />
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div className="min-h-[7rem] animate-pulse rounded-xl border border-zinc-800 bg-zinc-900" />
        <div className="min-h-[7rem] animate-pulse rounded-xl border border-zinc-800 bg-zinc-900" />
      </div>
      <div className="min-h-[7rem] animate-pulse rounded-xl border border-zinc-800 bg-zinc-900" />
      <div className="min-h-[6rem] animate-pulse rounded-xl border border-zinc-800 bg-zinc-900" />
    </div>
  );
}
