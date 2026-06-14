import { useCallback, useEffect, useMemo, useState } from "react";

import { api, type ProfileBody } from "../api";
import type { EngineProfile, ProfileTestResult } from "../types";
import { Badge, Button } from "../ui";

// Local field style — a darker variant of the shared `input` (zinc-950 surface)
// kept page-local so the form fields sit cleanly inside the zinc-900 card.
const field =
  "w-full rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 outline-none transition-colors placeholder:text-zinc-600 focus:border-indigo-500 focus-visible:ring-1 focus-visible:ring-indigo-500/40";
const label = "block text-xs font-medium text-zinc-400 mb-1";

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

function Spinner({ className = "" }: { className?: string }) {
  return (
    <span
      className={`inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-zinc-600 border-t-zinc-200 ${className}`}
      aria-hidden="true"
    />
  );
}

function fmtLatency(ms: number | null): string {
  if (ms == null) return "—";
  return `${Math.round(ms)} ms`;
}

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
  const [testing, setTesting] = useState(false);
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

  function set<K extends keyof ProfileBody>(key: K, value: ProfileBody[K]) {
    setForm((f) => ({ ...f, [key]: value }));
    // Identity-relevant edits invalidate a prior test result.
    setTest(null);
    setSaved(false);
  }
  const str = (v: string) => (v.trim() === "" ? null : v.trim());

  const showRouting = form.arg_mode === "flag" || form.arg_mode === "env";
  const showEnvFields = form.arg_mode === "env";

  function startNew() {
    setForm({ ...EMPTY_PROFILE });
    setEditingId(null);
    setTest(null);
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
      setSaved(true);
    } catch (e) {
      setFormError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function runTest() {
    if (!form.name.trim()) return;
    setFormError("");
    setTest(null);
    setTesting(true);
    try {
      setTest(await api.testProfile(form));
    } catch (e) {
      setFormError(String(e));
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
    <div className="grid grid-cols-2 gap-4">
      {/* ---------------------------------------------------------------- */}
      {/* Left: add / edit form                                            */}
      {/* ---------------------------------------------------------------- */}
      <form
        className="space-y-4 rounded-xl border border-zinc-800 bg-zinc-900 p-4"
        onSubmit={(e) => {
          e.preventDefault();
          save();
        }}
      >
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-zinc-300">
            {editingId ? "Edit profile" : "New profile"}
          </h3>
          {editingId && (
            <Button variant="ghost" onClick={startNew} title="Discard edits and start a new profile">
              New profile
            </Button>
          )}
        </div>

        {editingId && (
          <div className="flex items-center gap-2 rounded-lg border border-indigo-900/60 bg-indigo-950/40 px-3 py-2 text-xs text-indigo-300">
            <span>Editing</span>
            <span className="font-mono font-medium text-indigo-200">{editingName}</span>
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
          <p className="mt-1 text-xs text-zinc-500">
            {ARG_MODES.find((m) => m.value === form.arg_mode)?.hint}
          </p>
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

            <div className="grid grid-cols-2 gap-3">
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
                <input
                  id="profile-key"
                  className={field}
                  type="password"
                  placeholder={editingId ? "leave blank to keep existing" : "stored as a secret"}
                  value={form.auth_token ?? ""}
                  onChange={(e) => set("auth_token", str(e.target.value))}
                />
              </div>
            )}
          </div>
        )}

        {/* Actions */}
        <div className="flex items-center gap-2 pt-1">
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
                <Spinner /> Testing…
              </span>
            ) : (
              "Test"
            )}
          </Button>
          {saved && !busy && (
            <span className="text-sm font-medium text-emerald-400">Saved ✓</span>
          )}
        </div>

        {formError && (
          <div className="rounded-lg border border-rose-900/60 bg-rose-950/50 px-3 py-2 text-sm text-rose-300">
            {formError}
          </div>
        )}

        {/* Test result panel */}
        {testing && !test && (
          <div className="flex items-center gap-2 rounded-lg border border-zinc-800 bg-zinc-950/60 px-3 py-3 text-sm text-zinc-400">
            <Spinner /> Running test…
          </div>
        )}
        {test && <TestResult result={test} />}
      </form>

      {/* ---------------------------------------------------------------- */}
      {/* Right: profile list                                              */}
      {/* ---------------------------------------------------------------- */}
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-semibold uppercase tracking-wide text-zinc-300">
            Profiles
          </h3>
          {!loading && profiles.length > 0 && (
            <span className="text-xs text-zinc-500">{profiles.length}</span>
          )}
        </div>

        {loading ? (
          <div className="flex items-center gap-2 rounded-xl border border-zinc-800 bg-zinc-900 px-4 py-8 text-sm text-zinc-500">
            <Spinner /> Loading profiles…
          </div>
        ) : listError ? (
          <div className="rounded-xl border border-rose-900/60 bg-rose-950/50 px-4 py-3 text-sm text-rose-300">
            {listError}
          </div>
        ) : profiles.length === 0 ? (
          <div className="rounded-xl border border-zinc-800 bg-zinc-900 px-4 py-10 text-center text-sm text-zinc-500">
            No profiles yet. Create one on the left.
          </div>
        ) : (
          <div className="space-y-2">
            {profiles.map((p) => (
              <ProfileCard
                key={p.id}
                profile={p}
                active={editingId === p.id}
                deleting={deletingId === p.id}
                onEdit={() => startEdit(p)}
                onDelete={() => remove(p)}
              />
            ))}
          </div>
        )}
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

  return (
    <div
      className={`rounded-xl border bg-zinc-900 p-4 transition-colors ${
        active ? "border-indigo-500/70 ring-1 ring-indigo-500/30" : "border-zinc-800 hover:bg-zinc-800/40"
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate font-medium text-zinc-100" title={p.name}>
            {p.name}
          </span>
          {isDefault && <Badge text="default" color="bg-indigo-600" />}
        </div>
        <Badge text={p.arg_mode} color={MODE_COLOR[p.arg_mode] ?? "bg-zinc-700"} />
      </div>

      {/* Meta grid */}
      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        <dt className="text-zinc-500">model</dt>
        <dd className="truncate text-zinc-300" title={p.model ?? undefined}>
          {p.model ?? "—"}
        </dd>

        {p.subagent_model && (
          <>
            <dt className="text-zinc-500">subagent</dt>
            <dd className="truncate text-zinc-300" title={p.subagent_model}>
              {p.subagent_model}
            </dd>
          </>
        )}

        <dt className="text-zinc-500">effort</dt>
        <dd className="text-zinc-300">{p.effort ?? "—"}</dd>

        {p.base_url && (
          <>
            <dt className="text-zinc-500">base_url</dt>
            <dd className="truncate font-mono text-zinc-400" title={p.base_url}>
              {p.base_url}
            </dd>
          </>
        )}

        {p.auth_secret_id && (
          <>
            <dt className="text-zinc-500">auth</dt>
            <dd className="text-emerald-400">key set</dd>
          </>
        )}
      </dl>

      <div className="mt-3 flex gap-2">
        <Button variant="ghost" onClick={onEdit} title={`Edit ${p.name}`}>
          Edit
        </Button>
        {!isDefault && (
          <Button
            variant="danger"
            onClick={onDelete}
            disabled={deleting}
            title={`Delete ${p.name}`}
          >
            {deleting ? "Deleting…" : "Delete"}
          </Button>
        )}
      </div>
    </div>
  );
}

// --- Test result panel ------------------------------------------------------

function TestResult({ result }: { result: ProfileTestResult }) {
  const ok = result.ok;
  return (
    <div
      className={`rounded-lg border p-3 text-sm ${
        ok
          ? "border-emerald-900/60 bg-emerald-950/40 text-emerald-200"
          : "border-rose-900/60 bg-rose-950/50 text-rose-200"
      }`}
    >
      <div className="flex items-center gap-2 font-semibold">
        <span className={ok ? "text-emerald-400" : "text-rose-400"}>
          {ok ? "✓" : "✗"}
        </span>
        <span>{ok ? "Test passed" : "Test failed"}</span>
      </div>

      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        <dt className="text-zinc-400">model</dt>
        <dd className="truncate text-zinc-200" title={result.model_reported ?? undefined}>
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
