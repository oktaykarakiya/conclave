import { useCallback, useEffect, useMemo, useState } from "react";

import { api, type ProfileBody } from "../api";
import type { EngineProfile, ProfileTestResult } from "../types";
import { Badge, Button, Card, Spinner } from "../ui";

// Local field style — a darker variant of the shared `input` (zinc-950 surface)
// kept page-local so the form fields sit cleanly inside the zinc-900 card.
const field =
  "w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 outline-none transition-colors placeholder:text-zinc-600 focus:border-indigo-500 focus-visible:ring-1 focus-visible:ring-indigo-500/40";
const label = "block text-xs font-medium text-zinc-400 mb-1";
// Section header inside the panel — divider for clearer hierarchy when this
// page sits inside a long scrollable accordion section.
const sectionHeader =
  "flex items-center justify-between gap-2 border-b border-zinc-800 pb-2";
const sectionTitle = "text-sm font-semibold uppercase tracking-wide text-zinc-300";

const ARG_MODES: { value: string; label: string; hint: string }[] = [
  {
    value: "inherit",
    label: "inherit",
    hint: "Use the host's logged-in Claude default (no model routing).",
  },
  {
    value: "flag",
    label: "flag",
    hint: "Pass --model / --effort directly (Claude direct).",
  },
  {
    value: "env",
    label: "env",
    hint: "ANTHROPIC_* routing for a compatible endpoint (e.g. DeepSeek).",
  },
];

const EFFORTS = ["low", "medium", "high", "xhigh", "max"];

const EMPTY_PROFILE: ProfileBody = {
  name: "",
  project_id: null,
  arg_mode: "inherit",
  base_url: null,
  model: null,
  subagent_model: null,
  effort: null,
  auth_token: null,
  extra_env: {},
};

// Color per arg_mode for at-a-glance scanning (meaningful color only).
const MODE_COLOR: Record<string, string> = {
  inherit: "bg-zinc-700",
  flag: "bg-indigo-600",
  env: "bg-violet-700",
};

function modeHint(mode: string): string {
  return ARG_MODES.find((m) => m.value === mode)?.hint ?? "";
}

function fmtLatency(ms: number | null): string {
  if (ms == null) return "—";
  return `${Math.round(ms)} ms`;
}

// Currency formatter kept local for now (per shared-primitive guidance).
function fmtCost(usd: number | null): string {
  if (usd == null) return "—";
  if (usd === 0) return "$0";
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(2)}`;
}

export function ProfilesPanel() {
  const [profiles, setProfiles] = useState<EngineProfile[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState("");

  const [form, setForm] = useState<ProfileBody>({ ...EMPTY_PROFILE });
  const [editingId, setEditingId] = useState<string | null>(null);

  const [test, setTest] = useState<ProfileTestResult | null>(null);
  const [testedName, setTestedName] = useState<string | null>(null);
  const [testing, setTesting] = useState(false);
  const [showKey, setShowKey] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [formError, setFormError] = useState("");
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setListError("");
    try {
      setProfiles(await api.listProfiles());
    } catch (e) {
      setListError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  // Auto-hide the "Saved" confirmation so it registers then clears cleanly.
  useEffect(() => {
    if (!saved) return;
    const t = setTimeout(() => setSaved(false), 3000);
    return () => clearTimeout(t);
  }, [saved]);

  function set<K extends keyof ProfileBody>(key: K, value: ProfileBody[K]) {
    setForm((f) => ({ ...f, [key]: value }));
    // Identity-relevant edits invalidate a prior test result.
    setTest(null);
    setTestedName(null);
    setSaved(false);
  }
  const str = (v: string) => (v.trim() === "" ? null : v.trim());

  const showRouting = form.arg_mode === "flag" || form.arg_mode === "env";
  const showEnvFields = form.arg_mode === "env";

  function startNew() {
    setForm({ ...EMPTY_PROFILE });
    setEditingId(null);
    setTest(null);
    setTestedName(null);
    setShowKey(false);
    setFormError("");
    setSaved(false);
  }

  function startEdit(p: EngineProfile) {
    setForm({
      name: p.name,
      project_id: p.project_id,
      arg_mode: p.arg_mode,
      base_url: p.base_url,
      model: p.model,
      subagent_model: p.subagent_model,
      effort: p.effort,
      auth_token: null,
      extra_env: {},
    });
    setEditingId(p.id);
    setTest(null);
    setTestedName(null);
    setShowKey(false);
    setFormError("");
    setSaved(false);
  }

  async function save() {
    if (!form.name.trim()) return;
    setFormError("");
    setSaving(true);
    setSaved(false);
    try {
      await api.saveProfile(form);
      await reload();
      setForm({ ...EMPTY_PROFILE });
      setEditingId(null);
      setTest(null);
      setTestedName(null);
      setShowKey(false);
      setSaved(true);
    } catch (e) {
      setFormError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function runTest() {
    if (!form.name.trim()) return;
    const snapshotName = form.name.trim();
    setFormError("");
    setTest(null);
    setTestedName(snapshotName);
    setTesting(true);
    try {
      setTest(await api.testProfile(form));
    } catch (e) {
      setFormError(String(e));
      setTestedName(null);
    } finally {
      setTesting(false);
    }
  }

  async function remove(p: EngineProfile) {
    setFormError("");
    setDeletingId(p.id);
    try {
      await api.deleteProfile(p.id);
      if (editingId === p.id) startNew();
      await reload();
    } catch (e) {
      setListError(String(e));
    } finally {
      setDeletingId(null);
    }
  }

  const busy = saving || testing;
  const canSubmit = form.name.trim().length > 0;
  const editingName = useMemo(
    () => profiles.find((p) => p.id === editingId)?.name ?? null,
    [profiles, editingId],
  );

  return (
    // Root fills the fixed-height content viewport. On mobile the whole
    // (stacked) grid scrolls as one region; on desktop each column scrolls
    // independently so the page itself never grows taller than the screen.
    <div className="grid h-full min-h-0 grid-cols-1 gap-4 overflow-y-auto md:grid-cols-2 md:overflow-hidden">
      {/* ---------------------------------------------------------------- */}
      {/* Add / edit form                                                  */}
      {/* ---------------------------------------------------------------- */}
      <form
        className="min-h-0 min-w-0 space-y-4 rounded-xl border border-zinc-800 bg-zinc-900 p-4 md:overflow-y-auto"
        onSubmit={(e) => {
          e.preventDefault();
          save();
        }}
      >
        <div className={sectionHeader}>
          <h3 className={sectionTitle}>{editingId ? "Edit profile" : "New profile"}</h3>
          {editingId && (
            <Button variant="ghost" onClick={startNew} title="Discard edits and start a new profile">
              New profile
            </Button>
          )}
        </div>

        {editingId && (
          <div className="flex min-w-0 items-center gap-2 rounded-lg border border-indigo-900/60 bg-indigo-950/40 px-3 py-2 text-xs text-indigo-300">
            <span className="shrink-0">Editing</span>
            <span className="truncate font-mono font-medium text-indigo-200">{editingName}</span>
          </div>
        )}

        {/* Identity */}
        <div>
          <label className={label} htmlFor="profile-name">
            Name
          </label>
          <input
            id="profile-name"
            className={field}
            placeholder="e.g. deepseek"
            value={form.name}
            disabled={editingId != null && editingName === "system-default"}
            onChange={(e) => set("name", e.target.value)}
          />
        </div>

        {/* Mode */}
        <div>
          <label className={label} htmlFor="profile-argmode">
            Argument mode
          </label>
          <select
            id="profile-argmode"
            className={field}
            value={form.arg_mode}
            onChange={(e) => set("arg_mode", e.target.value)}
          >
            {ARG_MODES.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </select>
          <p className="mt-1 text-xs text-zinc-500">{modeHint(form.arg_mode)}</p>
        </div>

        {/* Routing fields — only meaningful for flag / env */}
        {showRouting && (
          <div className="space-y-4 rounded-lg border border-zinc-800/80 bg-zinc-950/40 p-3">
            <div className="text-xs font-medium uppercase tracking-wide text-zinc-500">
              Model routing
            </div>

            {showEnvFields && (
              <div>
                <label className={label} htmlFor="profile-baseurl">
                  Base URL
                </label>
                <input
                  id="profile-baseurl"
                  className={field}
                  placeholder="https://api.deepseek.com/anthropic"
                  value={form.base_url ?? ""}
                  onChange={(e) => set("base_url", str(e.target.value))}
                />
              </div>
            )}

            <div>
              <label className={label} htmlFor="profile-model">
                Model
              </label>
              <input
                id="profile-model"
                className={field}
                placeholder="e.g. deepseek-v4-pro"
                value={form.model ?? ""}
                onChange={(e) => set("model", str(e.target.value))}
              />
            </div>

            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <div>
                <label className={label} htmlFor="profile-subagent">
                  Subagent model
                </label>
                <input
                  id="profile-subagent"
                  className={field}
                  placeholder="optional"
                  value={form.subagent_model ?? ""}
                  onChange={(e) => set("subagent_model", str(e.target.value))}
                />
              </div>
              <div>
                <label className={label} htmlFor="profile-effort">
                  Effort
                </label>
                <select
                  id="profile-effort"
                  className={field}
                  value={form.effort ?? ""}
                  onChange={(e) => set("effort", str(e.target.value))}
                >
                  <option value="">unset</option>
                  {EFFORTS.map((x) => (
                    <option key={x} value={x}>
                      {x}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {showEnvFields && (
              <div>
                <label className={label} htmlFor="profile-key">
                  API key
                </label>
                <div className="relative">
                  <input
                    id="profile-key"
                    className={`${field} pr-16`}
                    type={showKey ? "text" : "password"}
                    placeholder={editingId ? "leave blank to keep existing" : "stored as a secret"}
                    value={form.auth_token ?? ""}
                    onChange={(e) => set("auth_token", str(e.target.value))}
                  />
                  <button
                    type="button"
                    onClick={() => setShowKey((s) => !s)}
                    aria-pressed={showKey}
                    title={showKey ? "Hide API key" : "Show API key"}
                    className="absolute inset-y-0 right-0 flex items-center px-3 text-xs font-medium text-zinc-400 transition-colors hover:text-zinc-200 focus-visible:ring-2 focus-visible:ring-indigo-400 focus-visible:outline-none"
                  >
                    {showKey ? "Hide" : "Show"}
                  </button>
                </div>
              </div>
            )}
          </div>
        )}

        {/* Actions */}
        <div className="flex flex-wrap items-center gap-2 pt-1">
          <Button
            type="submit"
            variant="primary"
            disabled={!canSubmit || busy}
            title={canSubmit ? "Save this profile" : "Name is required"}
          >
            {saving ? "Saving…" : editingId ? "Save changes" : "Save profile"}
          </Button>
          <Button
            onClick={runTest}
            disabled={!canSubmit || busy}
            title="Run a quick connectivity + model check"
          >
            {testing ? (
              <span className="flex items-center gap-2">
                <Spinner size={14} /> Testing…
              </span>
            ) : (
              "Test"
            )}
          </Button>
          {saved && !busy && (
            <span className="text-sm font-medium text-emerald-400" role="status">
              Saved ✓
            </span>
          )}
        </div>

        {formError && (
          <div
            role="alert"
            className="rounded-lg border border-rose-900/60 bg-rose-950/50 px-3 py-2 text-sm break-words text-rose-300"
          >
            {formError}
          </div>
        )}

        {/* Test result panel */}
        {testing && !test && (
          <div className="flex items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-950/60 px-3 py-3 text-sm text-zinc-400">
            <Spinner size={14} /> Testing {testedName ?? "profile"}…
          </div>
        )}
        {test && <TestResult result={test} name={testedName} />}
      </form>

      {/* ---------------------------------------------------------------- */}
      {/* Profile list                                                     */}
      {/* ---------------------------------------------------------------- */}
      <div className="flex min-h-0 min-w-0 flex-col gap-3">
        <div className={`${sectionHeader} shrink-0`}>
          <h3 className={sectionTitle}>Profiles</h3>
          {!loading && profiles.length > 0 && (
            <span className="text-xs tabular-nums text-zinc-500">{profiles.length}</span>
          )}
        </div>

        {/* Variable-length list scrolls within the column on desktop; on
            mobile the parent grid owns the scroll so this stays static. */}
        <div className="min-h-0 md:flex-1 md:overflow-y-auto">
          {loading ? (
            <Card className="flex items-center justify-center gap-2 py-8 text-sm text-zinc-500">
              <Spinner size={14} /> Loading profiles…
            </Card>
          ) : listError ? (
            <div
              role="alert"
              className="rounded-xl border border-rose-900/60 bg-rose-950/50 px-4 py-3 text-sm break-words text-rose-300"
            >
              {listError}
            </div>
          ) : profiles.length === 0 ? (
            <Card className="py-10 text-center text-sm text-zinc-500">
              No profiles yet — use the form to create one.
            </Card>
          ) : (
            <ul role="list" aria-label="Engine profiles" className="space-y-2">
              {profiles.map((p) => (
                <li key={p.id}>
                  <ProfileCard
                    profile={p}
                    active={editingId === p.id}
                    deleting={deletingId === p.id}
                    onEdit={() => startEdit(p)}
                    onDelete={() => remove(p)}
                  />
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}

// --- Profile card -----------------------------------------------------------

function ProfileCard({
  profile: p,
  active,
  deleting,
  onEdit,
  onDelete,
}: {
  profile: EngineProfile;
  active: boolean;
  deleting: boolean;
  onEdit: () => void;
  onDelete: () => void;
}) {
  const isDefault = p.name === "system-default";
  const [confirming, setConfirming] = useState(false);

  // Reset the confirm prompt if the user clicks away (focus leaves the card).
  function handleDelete() {
    if (confirming) {
      setConfirming(false);
      onDelete();
    } else {
      setConfirming(true);
    }
  }

  return (
    <Card
      className={`overflow-hidden p-4 transition-colors ${
        active
          ? "border-indigo-500/70 ring-1 ring-indigo-500/30"
          : "hover:bg-zinc-800/40"
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate font-medium text-zinc-100" title={p.name}>
            {p.name}
          </span>
          {isDefault && <Badge text="default" color="bg-indigo-600" />}
        </div>
        <span className="shrink-0" title={`${p.arg_mode}: ${modeHint(p.arg_mode)}`}>
          <Badge text={p.arg_mode} color={MODE_COLOR[p.arg_mode] ?? "bg-zinc-700"} />
        </span>
      </div>

      {/* Meta grid */}
      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        <dt className="text-zinc-500">model</dt>
        <dd className="min-w-0 truncate text-zinc-300" title={p.model ?? undefined}>
          {p.model ?? "—"}
        </dd>

        {p.subagent_model && (
          <>
            <dt className="text-zinc-500">subagent</dt>
            <dd className="min-w-0 truncate text-zinc-300" title={p.subagent_model}>
              {p.subagent_model}
            </dd>
          </>
        )}

        <dt className="text-zinc-500">effort</dt>
        <dd className="text-zinc-300">{p.effort ?? "—"}</dd>

        {p.base_url && (
          <>
            <dt className="text-zinc-500">base_url</dt>
            <dd className="min-w-0 truncate font-mono text-zinc-400" title={p.base_url}>
              {p.base_url}
            </dd>
          </>
        )}

        {p.auth_secret_id && (
          <>
            <dt className="text-zinc-500">auth</dt>
            <dd className="text-emerald-400" title="API key stored as a write-only secret (plaintext at rest in the local SQLite DB; never returned by the API)">
              key set
            </dd>
          </>
        )}
      </dl>

      {/* Actions — wrappers enforce a ≥44px touch target on mobile. */}
      <div className="mt-3 flex flex-wrap gap-2">
        <div className="flex min-h-[44px] items-center sm:min-h-0">
          <Button variant="ghost" onClick={onEdit} title={`Edit ${p.name}`}>
            Edit
          </Button>
        </div>
        {!isDefault && (
          <div
            className="flex min-h-[44px] items-center sm:min-h-0"
            onMouseLeave={() => setConfirming(false)}
          >
            <Button
              variant="danger"
              onClick={handleDelete}
              disabled={deleting}
              title={confirming ? `Confirm deletion of ${p.name}` : `Delete ${p.name}`}
            >
              {deleting ? "Deleting…" : confirming ? "Confirm delete?" : "Delete"}
            </Button>
            {confirming && !deleting && (
              <Button variant="ghost" onClick={() => setConfirming(false)} title="Cancel">
                Cancel
              </Button>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}

// --- Test result panel ------------------------------------------------------

function TestResult({ result, name }: { result: ProfileTestResult; name: string | null }) {
  const ok = result.ok;
  const heading = `${ok ? "Test passed" : "Test failed"}${name ? ` — ${name}` : ""}`;
  return (
    <div
      role="status"
      className={`rounded-lg border p-3 text-sm ${
        ok
          ? "border-emerald-900/60 bg-emerald-950/40 text-emerald-200"
          : "border-rose-900/60 bg-rose-950/50 text-rose-200"
      }`}
    >
      <div className="flex items-center gap-2 font-semibold">
        <span aria-hidden="true" className={ok ? "text-emerald-400" : "text-rose-400"}>
          {ok ? "✓" : "✗"}
        </span>
        <span>{heading}</span>
      </div>

      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        <dt className="text-zinc-400">model</dt>
        <dd className="min-w-0 truncate text-zinc-200" title={result.model_reported ?? undefined}>
          {result.model_reported ?? "—"}
        </dd>
        <dt className="text-zinc-400">latency</dt>
        <dd className="tabular-nums text-zinc-200">{fmtLatency(result.latency_ms)}</dd>
        <dt className="text-zinc-400">cost</dt>
        <dd className="tabular-nums text-zinc-200">{fmtCost(result.cost_usd)}</dd>
      </dl>

      {result.error && (
        <div className="mt-2 whitespace-pre-wrap break-words text-xs text-rose-300/90">
          {result.error}
        </div>
      )}
    </div>
  );
}
