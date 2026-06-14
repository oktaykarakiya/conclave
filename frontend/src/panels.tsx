import { useCallback, useEffect, useRef, useState } from "react";

import { api, type ProfileBody } from "./api";
import type { EngineProfile, EventRow, Integrity, ProfileTestResult, Quarantine, Task, Verdict } from "./types";
import { useStream } from "./useStream";

const STATE_COLORS: Record<string, string> = {
  inbox: "bg-zinc-600",
  approved: "bg-sky-600",
  in_progress: "bg-amber-500",
  done: "bg-emerald-600",
  failed: "bg-red-600",
  cancelled: "bg-zinc-500",
};

const VERDICT_COLORS: Record<string, string> = {
  pass: "text-emerald-400",
  fail: "text-red-400",
  block: "text-red-400",
  decline: "text-amber-400",
  unknown: "text-zinc-400",
};

export function Badge({ text, color }: { text: string; color?: string }) {
  return (
    <span className={`inline-block rounded px-2 py-0.5 text-xs font-medium text-white ${color ?? "bg-zinc-600"}`}>
      {text}
    </span>
  );
}

export function Button({
  children,
  onClick,
  variant = "default",
  disabled,
  type = "button",
}: {
  children: React.ReactNode;
  onClick?: () => void;
  variant?: "default" | "primary" | "danger" | "ghost";
  disabled?: boolean;
  type?: "button" | "submit";
}) {
  const styles: Record<string, string> = {
    default: "bg-zinc-700 hover:bg-zinc-600 text-zinc-100",
    primary: "bg-emerald-600 hover:bg-emerald-500 text-white",
    danger: "bg-red-700 hover:bg-red-600 text-white",
    ghost: "bg-transparent hover:bg-zinc-800 text-zinc-300",
  };
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`rounded px-3 py-1.5 text-sm font-medium transition disabled:opacity-40 ${styles[variant]}`}
    >
      {children}
    </button>
  );
}

const input =
  "w-full rounded border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-emerald-500";

function summarize(ev: EventRow): string {
  const p = ev.payload ?? {};
  switch (ev.type) {
    case "agent.verdict":
      return `verdict=${String(p.verdict)} — ${String(p.reason ?? "").slice(0, 120)}`;
    case "pipeline.derived":
      return `reviewers: ${(p.pipeline as string[] | undefined)?.join(", ") ?? ""}`;
    case "attempt.started":
      return `attempt #${String(p.n)}`;
    case "attempt.failed":
      return `attempt #${String(p.n)} failed at ${String(p.stage ?? "?")}`;
    case "task.done":
      return `done (merged=${String(p.merged)})`;
    case "agent.result":
      return `ok=${String(p.ok)} model=${String(p.model_reported ?? "?")}`;
    case "plan.level_selected":
      return `level=${String(p.level)}`;
    case "plan.artifact":
      return `plan artifact (${Object.keys(p).length} keys)`;
    default:
      return Object.keys(p).length ? JSON.stringify(p).slice(0, 140) : "";
  }
}

// --- Tasks ------------------------------------------------------------------

export function TasksPanel({ projectId }: { projectId: string }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [verdicts, setVerdicts] = useState<Verdict[]>([]);
  const [request, setRequest] = useState("");
  const [autoApprove, setAutoApprove] = useState(true);
  const [error, setError] = useState("");

  const reload = useCallback(async () => {
    try {
      setTasks(await api.listTasks(projectId));
    } catch (e) {
      setError(String(e));
    }
  }, [projectId]);

  useEffect(() => {
    reload();
    const id = setInterval(reload, 3000);
    return () => clearInterval(id);
  }, [reload]);

  useEffect(() => {
    if (!selected) return;
    api.taskVerdicts(selected).then(setVerdicts).catch(() => setVerdicts([]));
  }, [selected, tasks]);

  async function create() {
    if (!request.trim()) return;
    setError("");
    try {
      await api.createTask(projectId, { request, title: "", use_planner: null, auto_approve: autoApprove });
      setRequest("");
      await reload();
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <div className="grid grid-cols-2 gap-4">
      <div className="space-y-3">
        <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-3">
          <textarea
            className={`${input} h-24 resize-none`}
            placeholder="Describe the task for the team…"
            value={request}
            onChange={(e) => setRequest(e.target.value)}
          />
          <div className="mt-2 flex items-center justify-between">
            <label className="flex items-center gap-2 text-sm text-zinc-400">
              <input type="checkbox" checked={autoApprove} onChange={(e) => setAutoApprove(e.target.checked)} />
              auto-approve (run immediately)
            </label>
            <Button variant="primary" onClick={create}>
              Create task
            </Button>
          </div>
        </div>
        {error && <div className="rounded bg-red-950 p-2 text-sm text-red-300">{error}</div>}
        <div className="space-y-2">
          {tasks.map((t) => (
            <div
              key={t.id}
              className={`cursor-pointer rounded-lg border p-3 ${
                selected === t.id ? "border-emerald-600 bg-zinc-800" : "border-zinc-800 bg-zinc-900 hover:bg-zinc-850"
              }`}
              onClick={() => setSelected(t.id)}
            >
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-1.5">
                  <Badge text={t.state} color={STATE_COLORS[t.state]} />
                  {t.level != null && <Badge text={`L${t.level}`} color="bg-violet-600" />}
                </div>
                <span className="font-mono text-xs text-zinc-500">{t.id.slice(0, 8)}</span>
              </div>
              <div className="mt-1 line-clamp-2 text-sm text-zinc-200">{t.title || t.request}</div>
              {t.result_summary && <div className="mt-1 text-xs text-zinc-500">{t.result_summary}</div>}
              <div className="mt-2 flex gap-2">
                {t.state === "inbox" && (
                  <Button onClick={() => api.approve(t.id).then(reload)}>Approve</Button>
                )}
                {(t.state === "inbox" || t.state === "approved") && (
                  <Button variant="ghost" onClick={() => api.cancel(t.id).then(reload)}>
                    Cancel
                  </Button>
                )}
              </div>
            </div>
          ))}
          {tasks.length === 0 && <div className="text-sm text-zinc-500">No tasks yet.</div>}
        </div>
      </div>

      <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-3">
        <h3 className="mb-2 text-sm font-semibold text-zinc-300">Verdicts</h3>
        {!selected && <div className="text-sm text-zinc-500">Select a task to see review verdicts.</div>}
        {selected && verdicts.length === 0 && <div className="text-sm text-zinc-500">No verdicts recorded yet.</div>}
        <div className="space-y-2">
          {verdicts.map((v) => (
            <div key={v.id} className="rounded border border-zinc-800 p-2">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium text-zinc-200">{v.agent}</span>
                <span className={`text-sm font-semibold ${VERDICT_COLORS[v.verdict] ?? "text-zinc-400"}`}>
                  {v.verdict}
                </span>
              </div>
              <div className="text-xs text-zinc-400">attempt #{v.attempt} · grounded {v.grounded_count}</div>
              {v.reason && <div className="mt-1 text-xs text-zinc-300">{v.reason}</div>}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// --- Live log ---------------------------------------------------------------

export function LivePanel({ projectId }: { projectId: string }) {
  const events = useStream(projectId);
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events]);

  return (
    <div className="h-[70vh] overflow-y-auto rounded-lg border border-zinc-800 bg-black p-3 font-mono text-xs">
      {events.length === 0 && <div className="text-zinc-600">Waiting for events… create or approve a task.</div>}
      {events.map((ev) => (
        <div key={ev.id} className="flex gap-2 py-0.5">
          <span className="shrink-0 text-zinc-600">{ev.ts.slice(11, 19)}</span>
          <span className="shrink-0 text-emerald-500">{ev.type}</span>
          {ev.agent && <span className="shrink-0 text-sky-400">[{ev.agent}]</span>}
          <span className="text-zinc-300">{summarize(ev)}</span>
        </div>
      ))}
      <div ref={endRef} />
    </div>
  );
}

// --- Config -----------------------------------------------------------------

export function ConfigPanel({ projectId }: { projectId: string }) {
  const [text, setText] = useState("");
  const [status, setStatus] = useState("");

  useEffect(() => {
    api.getConfig(projectId).then((c) => setText(JSON.stringify(c, null, 2)));
  }, [projectId]);

  async function save() {
    setStatus("");
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      setStatus("Invalid JSON");
      return;
    }
    try {
      await api.patchConfig(projectId, parsed);
      setStatus("Saved ✓");
    } catch (e) {
      setStatus(String(e));
    }
  }

  return (
    <div className="space-y-2">
      <p className="text-sm text-zinc-400">
        Project configuration (target branch, per-agent models/effort, gate, planning…). Edits are
        validated before saving.
      </p>
      <textarea className={`${input} h-[60vh] font-mono`} value={text} onChange={(e) => setText(e.target.value)} />
      <div className="flex items-center gap-3">
        <Button variant="primary" onClick={save}>
          Save config
        </Button>
        {status && <span className="text-sm text-zinc-400">{status}</span>}
      </div>
    </div>
  );
}

// --- Engine profiles --------------------------------------------------------

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

export function ProfilesPanel() {
  const [profiles, setProfiles] = useState<EngineProfile[]>([]);
  const [form, setForm] = useState<ProfileBody>({ ...EMPTY_PROFILE });
  const [test, setTest] = useState<ProfileTestResult | null>(null);
  const [error, setError] = useState("");

  const reload = useCallback(() => api.listProfiles().then(setProfiles).catch(() => {}), []);
  useEffect(() => {
    reload();
  }, [reload]);

  function set<K extends keyof ProfileBody>(key: K, value: ProfileBody[K]) {
    setForm((f) => ({ ...f, [key]: value }));
  }
  const str = (v: string) => (v.trim() === "" ? null : v.trim());

  async function save() {
    setError("");
    try {
      await api.saveProfile(form);
      setForm({ ...EMPTY_PROFILE });
      setTest(null);
      await reload();
    } catch (e) {
      setError(String(e));
    }
  }
  async function runTest() {
    setError("");
    setTest(null);
    try {
      setTest(await api.testProfile(form));
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <div className="grid grid-cols-2 gap-4">
      <div className="space-y-3 rounded-lg border border-zinc-800 bg-zinc-900 p-3">
        <h3 className="text-sm font-semibold text-zinc-300">Add / edit engine profile</h3>
        <input className={input} placeholder="name (e.g. deepseek)" value={form.name} onChange={(e) => set("name", e.target.value)} />
        <select className={input} value={form.arg_mode} onChange={(e) => set("arg_mode", e.target.value)}>
          <option value="inherit">inherit — use the host's logged-in Claude default</option>
          <option value="flag">flag — pass --model / --effort (Claude direct)</option>
          <option value="env">env — ANTHROPIC_* routing (DeepSeek / compatible)</option>
        </select>
        <input className={input} placeholder="base_url (env mode, e.g. https://api.deepseek.com/anthropic)" value={form.base_url ?? ""} onChange={(e) => set("base_url", str(e.target.value))} />
        <input className={input} placeholder="model (free text, e.g. deepseek-v4-pro)" value={form.model ?? ""} onChange={(e) => set("model", str(e.target.value))} />
        <input className={input} placeholder="subagent_model (optional)" value={form.subagent_model ?? ""} onChange={(e) => set("subagent_model", str(e.target.value))} />
        <select className={input} value={form.effort ?? ""} onChange={(e) => set("effort", str(e.target.value))}>
          <option value="">effort: unset</option>
          {["low", "medium", "high", "xhigh", "max"].map((x) => (
            <option key={x} value={x}>
              {x}
            </option>
          ))}
        </select>
        <input className={input} type="password" placeholder="API key (stored as a secret; leave blank to keep)" value={form.auth_token ?? ""} onChange={(e) => set("auth_token", str(e.target.value))} />
        <div className="flex gap-2">
          <Button variant="primary" onClick={save} disabled={!form.name}>
            Save
          </Button>
          <Button onClick={runTest} disabled={!form.name}>
            Test
          </Button>
        </div>
        {error && <div className="rounded bg-red-950 p-2 text-sm text-red-300">{error}</div>}
        {test && (
          <div className={`rounded p-2 text-sm ${test.ok ? "bg-emerald-950 text-emerald-300" : "bg-red-950 text-red-300"}`}>
            {test.ok ? "✓ OK" : "✗ Failed"} · model={test.model_reported ?? "?"} · {test.latency_ms ?? "?"}ms
            {test.error ? ` · ${test.error}` : ""}
          </div>
        )}
      </div>

      <div className="space-y-2">
        {profiles.map((p) => (
          <div key={p.id} className="rounded-lg border border-zinc-800 bg-zinc-900 p-3">
            <div className="flex items-center justify-between">
              <span className="font-medium text-zinc-100">{p.name}</span>
              <Badge text={p.arg_mode} color="bg-zinc-700" />
            </div>
            <div className="mt-1 text-xs text-zinc-400">
              {p.model ?? "—"} {p.base_url ? `· ${p.base_url}` : ""} {p.auth_secret_id ? "· 🔑 key set" : ""}
            </div>
            <div className="mt-2 flex gap-2">
              <Button variant="ghost" onClick={() => setForm({ ...EMPTY_PROFILE, name: p.name, project_id: p.project_id, arg_mode: p.arg_mode, base_url: p.base_url, model: p.model, subagent_model: p.subagent_model, effort: p.effort, auth_token: null, extra_env: {} })}>
                Edit
              </Button>
              {p.name !== "system-default" && (
                <Button variant="danger" onClick={() => api.deleteProfile(p.id).then(reload)}>
                  Delete
                </Button>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// --- Quarantine -------------------------------------------------------------

export function QuarantinePanel({ projectId }: { projectId: string }) {
  const [entries, setEntries] = useState<Quarantine[]>([]);
  const [integrity, setIntegrity] = useState<Integrity | null>(null);
  const [form, setForm] = useState({ pattern: "", reason: "", until: "" });

  const reload = useCallback(async () => {
    setEntries(await api.listQuarantine(projectId));
    setIntegrity(await api.quarantineIntegrity(projectId));
  }, [projectId]);
  useEffect(() => {
    reload();
  }, [reload]);

  async function add() {
    if (!form.pattern || !form.reason || !form.until) return;
    await api.addQuarantine(projectId, form);
    setForm({ pattern: "", reason: "", until: "" });
    await reload();
  }

  return (
    <div className="space-y-3">
      {integrity && (
        <div className={`rounded-lg p-3 text-sm ${integrity.healthy ? "bg-emerald-950 text-emerald-300" : "bg-red-950 text-red-300"}`}>
          Green-gate integrity: {integrity.active} active, {integrity.expired} expired
          {integrity.healthy ? " — healthy" : ` — EXPIRED: ${integrity.expired_patterns.join(", ")}`}
        </div>
      )}
      <div className="flex flex-wrap gap-2 rounded-lg border border-zinc-800 bg-zinc-900 p-3">
        <input className={`${input} flex-1`} placeholder="pattern (suite/test path)" value={form.pattern} onChange={(e) => setForm({ ...form, pattern: e.target.value })} />
        <input className={`${input} flex-1`} placeholder="reason (required)" value={form.reason} onChange={(e) => setForm({ ...form, reason: e.target.value })} />
        <input className={`${input} w-40`} type="date" value={form.until} onChange={(e) => setForm({ ...form, until: e.target.value })} />
        <Button variant="primary" onClick={add}>
          Quarantine
        </Button>
      </div>
      <div className="space-y-2">
        {entries.map((q) => (
          <div key={q.id} className="flex items-center justify-between rounded-lg border border-zinc-800 bg-zinc-900 p-3">
            <div>
              <div className="font-mono text-sm text-zinc-100">{q.pattern}</div>
              <div className="text-xs text-zinc-400">{q.reason} · until {q.until}</div>
            </div>
            <Button variant="danger" onClick={() => api.delQuarantine(q.id).then(reload)}>
              Remove
            </Button>
          </div>
        ))}
        {entries.length === 0 && <div className="text-sm text-zinc-500">No quarantined suites.</div>}
      </div>
    </div>
  );
}
