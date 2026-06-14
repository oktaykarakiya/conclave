import { useCallback, useEffect, useState } from "react";

import { api } from "../api";
import type { Integrity, Quarantine } from "../types";
import { Button, input } from "../ui";

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

  async function add() {
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
      await reload();
    } catch (e) {
      setAddError(String(e));
    } finally {
      setAdding(false);
    }
  }

  async function remove(id: string) {
    if (removingId) return;
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

  // Sort: expired first (most overdue first), then active by soonest expiry.
  const sorted = [...entries].sort((a, b) => {
    const da = daysUntil(a.until);
    const db = daysUntil(b.until);
    if (da === null && db === null) return 0;
    if (da === null) return 1;
    if (db === null) return -1;
    return da - db;
  });

  const healthy = integrity?.healthy ?? true;

  return (
    <div className="space-y-4">
      {/* Header: title + integrity stat */}
      <div className="flex flex-wrap items-center justify-between gap-3">
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
            className={`flex items-center gap-4 rounded-xl border px-4 py-2 ${
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
          <span className="font-mono">{integrity.expired_patterns.join(", ")}</span>
          <span className="text-rose-400/70"> — re-validate or remove them.</span>
        </div>
      )}

      {/* Add form */}
      <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-3">
        <div className="flex flex-wrap items-start gap-2">
          <div className="min-w-[200px] flex-1">
            <input
              className={`${input} font-mono`}
              placeholder="pattern (suite/test path)"
              title="Test pattern to quarantine (e.g. tests/flaky/test_login.py::test_oauth)"
              value={form.pattern}
              onChange={(e) => setForm({ ...form, pattern: e.target.value })}
            />
          </div>
          <div className="min-w-[200px] flex-1">
            <input
              className={input}
              placeholder="reason (required)"
              title="Why is this test quarantined?"
              value={form.reason}
              onChange={(e) => setForm({ ...form, reason: e.target.value })}
            />
          </div>
          <input
            className={`${input} w-44`}
            type="date"
            min={todayISO()}
            title="Quarantine expires on this date"
            value={form.until}
            onChange={(e) => setForm({ ...form, until: e.target.value })}
          />
          <Button variant="primary" onClick={add} disabled={!formValid || adding}>
            {adding ? "Adding…" : "Quarantine"}
          </Button>
        </div>
        {addError && (
          <div className="mt-2 rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-300">
            {addError}
          </div>
        )}
      </div>

      {/* List */}
      {loading ? (
        <ListSkeleton />
      ) : loadError ? (
        <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-3 text-sm text-rose-300">
          {loadError}
        </div>
      ) : sorted.length === 0 ? (
        <div className="rounded-xl border border-dashed border-zinc-800 bg-zinc-900/40 py-12 text-center">
          <div className="text-sm font-medium text-zinc-400">Nothing quarantined</div>
          <div className="mt-1 text-xs text-zinc-500">
            The green gate is enforcing every test. Add a pattern above to skip a flaky one.
          </div>
        </div>
      ) : (
        <div className="space-y-2">
          {sorted.map((q) => {
            const expired = isExpired(q.until);
            const busy = removingId === q.id;
            return (
              <div
                key={q.id}
                className={`flex items-center justify-between gap-3 rounded-xl border bg-zinc-900 p-3 transition-colors hover:bg-zinc-800/50 ${
                  expired ? "border-amber-500/30" : "border-zinc-800"
                }`}
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span
                      className="truncate font-mono text-sm text-zinc-100"
                      title={q.pattern}
                    >
                      {q.pattern}
                    </span>
                    <Pill
                      tone={expired ? "amber" : "emerald"}
                      title={expired ? "Past its expiry — no longer protecting the gate" : "Active"}
                    >
                      {expired ? "expired" : "active"}
                    </Pill>
                  </div>
                  <div className="mt-1 flex items-center gap-2 text-xs text-zinc-400">
                    <span className="truncate" title={q.reason}>
                      {q.reason}
                    </span>
                    <span className="text-zinc-600">·</span>
                    <span
                      className={`shrink-0 tabular-nums ${
                        expired ? "text-amber-400" : "text-zinc-500"
                      }`}
                      title={`Until ${fmtDate(q.until)}`}
                    >
                      {expiryLabel(q.until)} · {fmtDate(q.until)}
                    </span>
                  </div>
                </div>
                <Button
                  variant="danger"
                  onClick={() => remove(q.id)}
                  disabled={busy}
                >
                  {busy ? "Removing…" : "Remove"}
                </Button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/* --- small presentational bits -------------------------------------------- */

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

function ListSkeleton() {
  return (
    <div className="space-y-2" aria-busy="true">
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          className="flex items-center justify-between rounded-xl border border-zinc-800 bg-zinc-900 p-3"
        >
          <div className="w-full space-y-2">
            <div className="h-3.5 w-1/3 animate-pulse rounded bg-zinc-800" />
            <div className="h-3 w-1/2 animate-pulse rounded bg-zinc-800/70" />
          </div>
          <div className="h-8 w-20 shrink-0 animate-pulse rounded bg-zinc-800" />
        </div>
      ))}
    </div>
  );
}
