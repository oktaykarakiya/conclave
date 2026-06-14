import { useCallback, useEffect, useState } from "react";

import { api } from "../api";
import type { RepoKnowledge } from "../types";
import { Button } from "../ui";

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

  if (error) {
    return (
      <div className="space-y-4">
        <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-300">
          <div className="font-medium text-rose-200">Failed to load repository knowledge</div>
          <div className="mt-1 break-words text-rose-300/90">{error}</div>
        </div>
        <Button variant="ghost" onClick={load}>
          Retry
        </Button>
      </div>
    );
  }

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

  if (isEmpty) {
    return (
      <div className="flex min-h-[320px] flex-col items-center justify-center rounded-xl border border-zinc-800 bg-zinc-900 p-10 text-center">
        <div className="text-sm font-semibold text-zinc-300">No repository knowledge yet</div>
        <p className="mt-2 max-w-md text-sm text-zinc-500">
          Run onboarding to scan the repository, or run AI analysis to enrich the heuristic
          summary with languages, frameworks, commands and conventions.
        </p>
        <div className="mt-5">
          <Button variant="primary" onClick={runAi} disabled={analyzing}>
            {analyzing ? "Analyzing…" : "Run AI analysis"}
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Header: enrichment status + action */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <Pill
            tone={aiEnriched ? "emerald" : "amber"}
            label={aiEnriched ? "AI-enriched" : "Heuristic only"}
          />
          <span className="text-xs text-zinc-500">
            {languages.length} language{languages.length === 1 ? "" : "s"} ·{" "}
            {frameworks.length} framework{frameworks.length === 1 ? "" : "s"} ·{" "}
            {commandEntries.length} command{commandEntries.length === 1 ? "" : "s"}
          </span>
        </div>
        <Button variant="primary" onClick={runAi} disabled={analyzing}>
          {analyzing ? "Analyzing…" : aiEnriched ? "Re-run AI analysis" : "Run AI analysis"}
        </Button>
      </div>

      {/* Architecture summary */}
      {architecture.trim() !== "" && (
        <Section title="Architecture">
          <p className="text-sm leading-relaxed text-zinc-300">{architecture}</p>
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

      {/* Commands */}
      <Section title="Commands">
        {commandEntries.length > 0 ? (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            {commandEntries.map(([key, val]) => (
              <div
                key={key}
                className="flex items-center justify-between gap-3 rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2"
              >
                <span className="shrink-0 text-xs font-semibold uppercase tracking-wide text-zinc-500">
                  {key}
                </span>
                <code
                  className="truncate font-mono text-sm text-emerald-300"
                  title={val}
                >
                  {val}
                </code>
              </div>
            ))}
          </div>
        ) : (
          <EmptyNote>No commands detected</EmptyNote>
        )}
      </Section>

      {/* Conventions */}
      <Section title="Conventions">
        {conventions.length > 0 ? (
          <ul className="space-y-1.5">
            {conventions.map((c, i) => (
              <li key={i} className="flex gap-2 text-sm text-zinc-300">
                <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-zinc-600" aria-hidden />
                <span>{c}</span>
              </li>
            ))}
          </ul>
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
                  <div className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-zinc-500">
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
                <code
                  key={g}
                  className="max-w-full truncate rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1 font-mono text-xs text-amber-300"
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
  );
}

// --- Page-local helpers ------------------------------------------------------

const SECTION_CARD = "rounded-xl border border-zinc-800 bg-zinc-900 p-4";
const SECTION_TITLE = "mb-3 text-sm font-semibold uppercase tracking-wide text-zinc-300";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className={SECTION_CARD}>
      <h3 className={SECTION_TITLE}>{title}</h3>
      {children}
    </div>
  );
}

function EmptyNote({ children }: { children: React.ReactNode }) {
  return <span className="text-sm text-zinc-500">{children}</span>;
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
  return (
    <div className="space-y-4" aria-busy="true" aria-live="polite">
      <div className="flex items-center justify-between">
        <div className="h-6 w-32 animate-pulse rounded-full bg-zinc-800" />
        <div className="h-8 w-36 animate-pulse rounded bg-zinc-800" />
      </div>
      <div className="h-24 animate-pulse rounded-xl border border-zinc-800 bg-zinc-900" />
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div className="h-28 animate-pulse rounded-xl border border-zinc-800 bg-zinc-900" />
        <div className="h-28 animate-pulse rounded-xl border border-zinc-800 bg-zinc-900" />
      </div>
      <div className="h-28 animate-pulse rounded-xl border border-zinc-800 bg-zinc-900" />
    </div>
  );
}
