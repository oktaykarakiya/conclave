import type React from "react";
import { useCallback, useEffect, useState } from "react";

import { api } from "../api";
import type { Integrity, Quarantine } from "../types";
import { Button, Card, Spinner, input } from "../ui";

/* --- local helpers -------------------------------------------------------- */

type Tone = "zinc" | "amber" | "emerald" | "rose" | "indigo";

const TONE: Record<Tone, string> = {
  zinc: "bg-zinc-800 text-zinc-300 ring-zinc-700",
  amber: "bg-amber-500/10 text-amber-300 ring-amber-500/30",
  emerald: "bg-emerald-500/10 text-emerald-300 ring-emerald-500/30",
  rose: "bg-rose-500/10 text-rose-300 ring-rose-500/30",
  indigo: "bg-indigo-500/10 text-indigo-300 ring-indigo-500/30",
};

/** Tinted status pill — calmer than the solid shared Badge, for active/expired state. */
function Pill({ tone, children, title }: { tone: Tone; children: React.ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${TONE[tone]}`}
    >
      {children}
    </span>
  );
}

function todayISO(): string {
  // local YYYY-MM-DD, matching the <input type="date"> value format
  const d = new Date();
  const off = d.getTimezoneOffset() * 60000;
  return new Date(d.getTime() - off).toISOString().slice(0, 10);
}

/** Whole-day difference (until - today). Negative => already expired. */
function daysUntil(until: string): number | null {
  if (!until) return null;
  const u = new Date(`${until}T00:00:00`);
  if (Number.isNaN(u.getTime())) return null;
  const now = new Date(`${todayISO()}T00:00:00`);
  return Math.round((u.getTime() - now.getTime()) / 86_400_000);
}

function isExpired(until: string): boolean {
  const d = daysUntil(until);
  return d !== null && d < 0;
}

function fmtDate(until: string): string {
  const u = new Date(`${until}T00:00:00`);
  if (Number.isNaN(u.getTime())) return until || "—";
  return u.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/** "3d left" / "expires today" / "expired 2d ago" */
function expiryLabel(until: string): string {
  const d = daysUntil(until);
  if (d === null) return "no expiry";
  if (d < 0) return `expired ${Math.abs(d)}d ago`;
  if (d === 0) return "expires today";
  if (d === 1) return "1d left";
  return `${d}d left`;
}

/* --- component ------------------------------------------------------------ */

export function QuarantinePanel({ projectId }: { projectId: string }) {
  const [entries, setEntries] = useState<Quarantine[]>([]);
  const [integrity, setIntegrity] = useState<Integrity | null>(null);
  const [form, setForm] = useState({ pattern: "", reason: "", until: "" });
  const [showAdd, setShowAdd] = useState(false);

  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState("");
  const [removingId, setRemovingId] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setLoadError("");
    try {
      const [list, integ] = await Promise.all([
        api.listQuarantine(projectId),
        api.quarantineIntegrity(projectId),
      ]);
      setEntries(list);
      setIntegrity(integ);
    } catch (e) {
      setLoadError(String(e));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    setLoading(true);
    reload();
  }, [reload]);

  const formValid =
    form.pattern.trim() !== "" && form.reason.trim() !== "" && form.until.trim() !== "";

  // Clear a stale add error as soon as the user starts editing any field.
  function updateForm(patch: Partial<typeof form>) {
    if (addError) setAddError("");
    setForm((f) => ({ ...f, ...patch }));
  }

  async function add(e?: React.FormEvent) {
    e?.preventDefault();
    if (!formValid || adding) return;
    setAddError("");
    setAdding(true);
    try {
      await api.addQuarantine(projectId, {
        pattern: form.pattern.trim(),
        reason: form.reason.trim(),
        until: form.until,
      });
      setForm({ pattern: "", reason: "", until: "" });
      setShowAdd(false);
      await reload();
    } catch (e) {
      setAddError(String(e));
    } finally {
      setAdding(false);
    }
  }

  async function remove(id: string) {
    // Only block re-removing the same row; allow acting on other rows.
    if (removingId === id) return;
    setRemovingId(id);
    setLoadError("");
    try {
      await api.delQuarantine(id);
      await reload();
    } catch (e) {
      setLoadError(String(e));
    } finally {
      setRemovingId(null);
    }
  }

  // Sort: active entries by soonest expiry (most urgent first), then expired
  // entries (cleanup) below, then no-expiry last.
  const sorted = [...entries].sort((a, b) => {
    const da = daysUntil(a.until);
    const db = daysUntil(b.until);
    if (da === null && db === null) return 0;
    if (da === null) return 1;
    if (db === null) return -1;
    const aExp = da < 0;
    const bExp = db < 0;
    if (aExp !== bExp) return aExp ? 1 : -1; // active group before expired group
    return da - db; // within a group, soonest first
  });

  const healthy = integrity?.healthy ?? true;

  return (
    <div className="flex h-full min-h-0 flex-col gap-4">
      {/* Fixed top region: header + integrity + degraded detail + add disclosure */}
      <div className="shrink-0 space-y-4">
      {/* Header: title + integrity stat (stacks on mobile) */}
      <div className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-center sm:justify-between">
        <div>
          <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-300">
            Quarantined tests
          </h2>
          <p className="mt-0.5 text-xs text-zinc-500">
            Patterns excluded from the green gate until their expiry date.
          </p>
        </div>

        {integrity && (
          <div
            className={`flex w-full flex-wrap items-center gap-3 rounded-xl border px-4 py-2 sm:w-auto ${
              healthy
                ? "border-emerald-500/30 bg-emerald-500/5"
                : "border-rose-500/30 bg-rose-500/5"
            }`}
          >
            <Pill tone={healthy ? "emerald" : "rose"}>
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  healthy ? "bg-emerald-400" : "bg-rose-400"
                }`}
              />
              {healthy ? "Gate integrity OK" : "Gate degraded"}
            </Pill>
            <div className="flex items-center gap-4 text-xs">
              <Stat label="active" value={integrity.active} tone="zinc" />
              <Stat
                label="expired"
                value={integrity.expired}
                tone={integrity.expired > 0 ? "rose" : "zinc"}
              />
            </div>
          </div>
        )}
      </div>

      {/* Degraded detail */}
      {integrity && !healthy && integrity.expired_patterns.length > 0 && (
        <div className="rounded-xl border border-rose-500/30 bg-rose-500/5 p-3 text-xs text-rose-300">
          <span className="font-medium">Expired patterns still active:</span>{" "}
          <span className="break-all font-mono">{integrity.expired_patterns.join(", ")}</span>
          <span className="text-rose-400/70"> — re-validate or remove them.</span>
        </div>
      )}

      {/* Add rule — tucked behind a disclosure so the list stays primary */}
      <div>
        {!showAdd ? (
          <Button
            variant="ghost"
            onClick={() => setShowAdd(true)}
            title="Add a quarantine rule"
          >
            + Add quarantine rule
          </Button>
        ) : (
          <Card className="p-3">
            <form className="flex flex-col gap-3 sm:flex-row sm:flex-wrap sm:items-end" onSubmit={add}>
              <Field label="Pattern" className="min-w-0 flex-1 sm:min-w-[200px]">
                <input
                  className={`${input} font-mono`}
                  placeholder="suite/test path"
                  aria-label="Test pattern to quarantine"
                  title="Test pattern to quarantine (e.g. tests/flaky/test_login.py::test_oauth)"
                  value={form.pattern}
                  onChange={(e) => updateForm({ pattern: e.target.value })}
                />
              </Field>
              <Field label="Reason" className="min-w-0 flex-1 sm:min-w-[200px]">
                <input
                  className={input}
                  placeholder="why is it flaky?"
                  aria-label="Reason for quarantine"
                  title="Why is this test quarantined?"
                  value={form.reason}
                  onChange={(e) => updateForm({ reason: e.target.value })}
                />
              </Field>
              <Field label="Expires" className="w-full sm:w-44">
                <input
                  className={input}
                  style={{ colorScheme: "dark" }}
                  type="date"
                  min={todayISO()}
                  aria-label="Quarantine expiry date"
                  title="Quarantine expires on this date"
                  value={form.until}
                  onChange={(e) => updateForm({ until: e.target.value })}
                />
              </Field>
              <div className="flex w-full gap-2 sm:w-auto">
                <button
                  type="submit"
                  disabled={!formValid || adding}
                  className="flex min-h-[44px] flex-1 items-center justify-center rounded px-4 text-sm font-medium text-white transition focus-visible:ring-2 focus-visible:ring-indigo-400 focus-visible:outline-none disabled:opacity-40 bg-indigo-600 hover:bg-indigo-500 sm:flex-none"
                >
                  {adding ? "Adding…" : "Quarantine"}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setShowAdd(false);
                    setAddError("");
                  }}
                  className="flex min-h-[44px] items-center justify-center rounded px-4 text-sm font-medium text-zinc-300 transition hover:bg-zinc-800 focus-visible:ring-2 focus-visible:ring-indigo-400 focus-visible:outline-none"
                >
                  Cancel
                </button>
              </div>
            </form>
            {addError && (
              <div className="mt-2 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-300">
                {addError}
              </div>
            )}
          </Card>
        )}
      </div>
      </div>

      {/* List — scrolls within the page; top region stays fixed */}
      <div className="min-h-0 flex-1 overflow-y-auto">
      {loading ? (
        <div
          className="flex items-center justify-center gap-2 py-10 text-sm text-zinc-500"
          aria-busy="true"
          aria-label="Loading quarantine entries"
        >
          <Spinner /> Loading quarantine…
        </div>
      ) : loadError ? (
        <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-300">
          {loadError}
        </div>
      ) : sorted.length === 0 ? (
        <div className="rounded-xl border border-dashed border-zinc-800 bg-zinc-900/40 py-12 text-center">
          <div className="text-sm font-medium text-zinc-400">Nothing quarantined</div>
          <div className="mt-1 px-4 text-xs text-zinc-500">
            The green gate is enforcing every test. Add a pattern to skip a flaky one.
          </div>
        </div>
      ) : (
        <div className="space-y-2">
          {sorted.map((q) => (
            <QuarantineRow
              key={q.id}
              q={q}
              busy={removingId === q.id}
              onRemove={() => remove(q.id)}
            />
          ))}
        </div>
      )}
      </div>
    </div>
  );
}

/* --- small presentational bits -------------------------------------------- */

/** A single quarantine row with expandable long pattern/reason + inline delete confirm. */
function QuarantineRow({
  q,
  busy,
  onRemove,
}: {
  q: Quarantine;
  busy: boolean;
  onRemove: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const expired = isExpired(q.until);

  return (
    <div
      className={`flex flex-col gap-2 rounded-xl border bg-zinc-900 p-3 transition-colors hover:bg-zinc-800/50 sm:flex-row sm:items-start sm:justify-between sm:gap-3 ${
        expired ? "border-rose-500/30" : "border-zinc-800"
      }`}
    >
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        title={expanded ? "Collapse details" : "Show full pattern & reason"}
        className="min-w-0 flex-1 rounded text-left focus-visible:ring-2 focus-visible:ring-indigo-400 focus-visible:outline-none"
      >
        <div className="flex items-center gap-2">
          <span
            className={`font-mono text-sm text-zinc-100 ${
              expanded ? "break-all whitespace-pre-wrap" : "truncate"
            }`}
          >
            {q.pattern}
          </span>
          <Pill
            tone={expired ? "rose" : "emerald"}
            title={expired ? "Past its expiry — no longer protecting the gate" : "Active"}
          >
            {expired ? "expired" : "active"}
          </Pill>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-zinc-400">
          <span className={expanded ? "break-words whitespace-pre-wrap" : "min-w-0 truncate"}>
            {q.reason}
          </span>
          <span className="text-zinc-600">·</span>
          <span
            className={`shrink-0 tabular-nums ${expired ? "text-rose-400" : "text-zinc-500"}`}
          >
            {expiryLabel(q.until)} · {fmtDate(q.until)}
          </span>
        </div>
      </button>

      {/* Delete with inline two-step confirmation (no modal) */}
      {confirming ? (
        <div className="flex shrink-0 gap-2">
          <Button variant="danger" onClick={onRemove} disabled={busy} title="Confirm removal">
            {busy ? "Removing…" : "Confirm"}
          </Button>
          <Button variant="ghost" onClick={() => setConfirming(false)} disabled={busy}>
            Cancel
          </Button>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setConfirming(true)}
          title="Remove this quarantine rule"
          className="flex min-h-[44px] shrink-0 items-center justify-center rounded px-4 text-sm font-medium text-rose-300 transition hover:bg-rose-500/10 focus-visible:ring-2 focus-visible:ring-rose-400 focus-visible:outline-none"
        >
          Remove
        </button>
      )}
    </div>
  );
}

function Field({
  label,
  className = "",
  children,
}: {
  label: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <label className={`block ${className}`}>
      <span className="mb-1 block text-xs text-zinc-500">{label}</span>
      {children}
    </label>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone: Tone }) {
  const color =
    tone === "rose" ? "text-rose-300" : tone === "emerald" ? "text-emerald-300" : "text-zinc-200";
  return (
    <div className="flex items-baseline gap-1.5">
      <span className={`tabular-nums text-base font-semibold ${color}`}>{value}</span>
      <span className="text-zinc-500">{label}</span>
    </div>
  );
}
