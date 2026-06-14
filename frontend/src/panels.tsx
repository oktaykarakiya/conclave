import { useCallback, useEffect, useRef, useState } from "react";

import { api, type ProfileBody } from "./api";
import type { EngineProfile, EventRow, Integrity, PlanningMessage, PlanningSession, PlanningTaskNode, ProfileTestResult, Quarantine, RepoKnowledge, Task, Verdict } from "./types";
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
  onClick?: (e: React.MouseEvent<HTMLButtonElement>) => void;
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

  const tree = buildTaskTree(tasks);
  const hasHierarchy = tasks.some((t) => t.parent_task_id);

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

        {/* Task tree */}
        <div className="space-y-1">
          {hasHierarchy ? (
            tree.length > 0 ? (
              tree.map((node) => (
                <TaskTreeItem
                  key={node.task.id}
                  node={node}
                  allTasks={tasks}
                  selectedId={selected}
                  onSelect={setSelected}
                  onReload={reload}
                />
              ))
            ) : (
              <div className="text-sm text-zinc-500">No tasks yet.</div>
            )
          ) : (
            /* Flat list fallback for projects without hierarchy */
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
                    <Badge text={t.state} color={STATE_COLORS[t.state]} />
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
          )}
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

/* ------------------------------------------------------------------------ */
/* Task tree helpers                                                        */
/* ------------------------------------------------------------------------ */

interface TaskTreeNode {
  task: Task;
  children: TaskTreeNode[];
}

interface TaskUsage {
  total_turns: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  agent_count: number;
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function buildTaskTree(tasks: Task[]): TaskTreeNode[] {
  const map = new Map<string, TaskTreeNode>();
  const roots: TaskTreeNode[] = [];
  for (const t of tasks) {
    map.set(t.id, { task: t, children: [] });
  }
  for (const t of tasks) {
    const node = map.get(t.id)!;
    if (t.parent_task_id && map.has(t.parent_task_id)) {
      map.get(t.parent_task_id)!.children.push(node);
    } else {
      roots.push(node);
    }
  }
  // Sort children by created_at for sequential order
  for (const [, n] of map) {
    n.children.sort(
      (a, b) =>
        new Date(a.task.created_at).getTime() -
        new Date(b.task.created_at).getTime(),
    );
  }
  roots.sort(
    (a, b) =>
      new Date(a.task.created_at).getTime() -
      new Date(b.task.created_at).getTime(),
  );
  return roots;
}

function fmtTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function TaskTreeItem({
  node,
  allTasks,
  selectedId,
  onSelect,
  onReload,
  depth = 0,
}: {
  node: TaskTreeNode;
  allTasks: Task[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onReload: () => void;
  depth?: number;
}) {
  const [expanded, setExpanded] = useState(true);
  const [usage, setUsage] = useState<TaskUsage | null>(null);
  const [showDetails, setShowDetails] = useState(false);
  const hasChildren = node.children.length > 0;
  const t = node.task;

  // Fetch token usage on mount (and refresh when the task id changes)
  useEffect(() => {
    api.taskUsage(t.id).then(setUsage).catch(() => {});
  }, [t.id]);

  async function handleCascadeApprove(e: React.MouseEvent) {
    e.stopPropagation();
    try {
      await api.cascadeApprove(t.id);
      onReload();
    } catch {
      /* ignore */
    }
  }

  async function handleApprove(e: React.MouseEvent) {
    e.stopPropagation();
    await api.approve(t.id);
    onReload();
  }

  async function handleCancel(e: React.MouseEvent) {
    e.stopPropagation();
    await api.cancel(t.id);
    onReload();
  }

  const stateColor = STATE_COLORS[t.state] ?? "bg-zinc-600";

  return (
    <div>
      <div
        className={`cursor-pointer rounded-lg border p-2.5 ${
          selectedId === t.id
            ? "border-emerald-600 bg-zinc-800"
            : "border-zinc-800 bg-zinc-900 hover:bg-zinc-850"
        }`}
        style={{ marginLeft: `${depth * 20}px` }}
        onClick={() => onSelect(t.id)}
      >
        {/* Top row: expand, badge, title, time */}
        <div className="flex items-center gap-1.5">
          {hasChildren ? (
            <button
              onClick={(e) => {
                e.stopPropagation();
                setExpanded(!expanded);
              }}
              className="text-zinc-500 hover:text-zinc-300 text-xs w-4 shrink-0 leading-none"
            >
              {expanded ? "▾" : "▸"}
            </button>
          ) : (
            <span className="w-4 shrink-0" />
          )}
          <Badge text={t.state} color={stateColor} />
          {hasChildren && (
            <span className="text-xs text-zinc-500 font-medium">
              {node.children.length}
            </span>
          )}
          <span className="text-sm text-zinc-200 truncate font-medium">
            {t.title || t.request.slice(0, 80)}
          </span>
          <span className="text-xs text-zinc-600 ml-auto shrink-0 tabular-nums flex gap-1">
            <span title="Created">{fmtTime(t.created_at)}</span>
            {(t.state === "done" || t.state === "failed") && (
              <span title="Completed">→ {fmtTime(t.updated_at)}</span>
            )}
            {t.state === "in_progress" && (
              <span className="text-amber-500" title="Started">(running)</span>
            )}
          </span>
        </div>

        {/* Second row: stats + timing */}
        <div className="flex items-center gap-3 mt-1 ml-5 text-xs text-zinc-500">
          {/* Time range for done/failed */}
          {(t.state === "done" || t.state === "failed") && (
            <span title="Duration">
              {(() => {
                const start = new Date(t.created_at).getTime();
                const end = new Date(t.updated_at).getTime();
                const mins = Math.round((end - start) / 60000);
                return mins > 0 ? `${mins}m` : "<1m";
              })()}
            </span>
          )}
          {usage && (
            <>
              <span title="Input tokens">↓{fmtTokens(usage.input_tokens)} in</span>
              <span title="Output tokens">↑{fmtTokens(usage.output_tokens)} out</span>
              <span title="Cache read tokens">⚡{fmtTokens(usage.cache_read_tokens)} cached</span>
              <span title="Cache creation tokens">+{fmtTokens(usage.cache_creation_tokens)} cache-write</span>
              <span title="Total turns">{usage.total_turns} turns</span>
              <span title="Agent invocations">{usage.agent_count} agents</span>
            </>
          )}
          {!usage && <span className="text-zinc-700">loading…</span>}
          {t.result_summary && (
            <span className="text-zinc-400 truncate max-w-[300px]" title={t.result_summary}>
              {t.result_summary}
            </span>
          )}
          <button
            onClick={(e) => { e.stopPropagation(); setShowDetails(!showDetails); }}
            className="text-zinc-600 hover:text-zinc-400 ml-auto"
          >
            {showDetails ? "less" : "more"}
          </button>
        </div>

        {/* Expanded details */}
        {showDetails && (
          <div className="mt-2 ml-5 p-2 rounded bg-zinc-950 text-xs text-zinc-400 space-y-1">
            <div>ID: <span className="font-mono text-zinc-500">{t.id}</span></div>
            <div>Created: {new Date(t.created_at).toLocaleString()}</div>
            <div>Updated: {new Date(t.updated_at).toLocaleString()}</div>
            {t.level != null && <div>Level: {t.level}</div>}
            {t.branch && <div>Branch: <span className="font-mono">{t.branch}</span></div>}
            {t.request && t.request !== t.title && (
              <div className="text-zinc-500 mt-1 max-h-20 overflow-y-auto whitespace-pre-wrap">
                {t.request.slice(0, 300)}
              </div>
            )}
          </div>
        )}

        {/* Actions */}
        <div className="mt-1.5 flex gap-2 ml-5">
          {t.state === "inbox" && hasChildren && (
            <Button variant="primary" onClick={handleCascadeApprove}>
              Approve tree
            </Button>
          )}
          {t.state === "inbox" && !hasChildren && (
            <Button onClick={handleApprove}>Approve</Button>
          )}
          {(t.state === "inbox" || t.state === "approved") && (
            <Button variant="ghost" onClick={handleCancel}>
              Cancel
            </Button>
          )}
        </div>
      </div>

      {/* Children (sequential order by created_at) */}
      {expanded &&
        node.children.map((child) => (
          <TaskTreeItem
            key={child.task.id}
            node={child}
            allTasks={allTasks}
            selectedId={selectedId}
            onSelect={onSelect}
            onReload={onReload}
            depth={depth + 1}
          />
        ))}
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
            <div className="mt-1 text-xs text-zinc-400 space-y-0.5">
              <div>{p.model ?? "—"} {p.effort ? `@ ${p.effort}` : ""} {p.base_url ? `· ${p.base_url}` : ""} {p.auth_secret_id ? "· 🔑 key set" : ""}</div>
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

// --- Knowledge (repo intelligence) -------------------------------------------

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

  if (loading) return <div className="text-sm text-zinc-500">Loading…</div>;
  if (error) return <div className="rounded bg-red-950 p-3 text-sm text-red-300">{error}</div>;
  if (!knowledge) return <div className="text-sm text-zinc-500">No knowledge recorded. Run onboarding first.</div>;

  const section = "rounded-lg border border-zinc-800 bg-zinc-900 p-4";
  const sectionTitle = "mb-2 text-sm font-semibold text-zinc-300";

  return (
    <div className="space-y-4">
      {/* Status badge */}
      <div className="flex items-center gap-3">
        <Badge
          text={knowledge.ai_enriched ? "AI-enriched" : "Heuristic only"}
          color={knowledge.ai_enriched ? "bg-emerald-700" : "bg-amber-700"}
        />
        <Button variant="primary" onClick={runAi} disabled={analyzing}>
          {analyzing ? "Analyzing…" : knowledge.ai_enriched ? "Re-run AI analysis" : "Run AI analysis"}
        </Button>
      </div>

      {/* Architecture summary */}
      {knowledge.architecture_summary && (
        <div className={section}>
          <h3 className={sectionTitle}>Architecture</h3>
          <p className="text-sm text-zinc-300">{knowledge.architecture_summary}</p>
        </div>
      )}

      {/* Languages & Frameworks */}
      <div className="grid grid-cols-2 gap-4">
        <div className={section}>
          <h3 className={sectionTitle}>Languages</h3>
          <div className="flex flex-wrap gap-1">
            {knowledge.languages.length > 0
              ? knowledge.languages.map((l) => <Badge key={l} text={l} color="bg-sky-700" />)
              : <span className="text-sm text-zinc-500">none detected</span>}
          </div>
        </div>
        <div className={section}>
          <h3 className={sectionTitle}>Frameworks</h3>
          <div className="flex flex-wrap gap-1">
            {knowledge.frameworks.length > 0
              ? knowledge.frameworks.map((f) => <Badge key={f} text={f} color="bg-violet-700" />)
              : <span className="text-sm text-zinc-500">none detected</span>}
          </div>
        </div>
      </div>

      {/* Commands */}
      <div className={section}>
        <h3 className={sectionTitle}>Commands</h3>
        {Object.keys(knowledge.commands).length > 0 ? (
          <div className="grid grid-cols-2 gap-2">
            {Object.entries(knowledge.commands).map(([key, val]) => (
              <div key={key} className="flex items-center justify-between rounded bg-zinc-950 px-3 py-2">
                <span className="text-xs font-medium text-zinc-400 uppercase">{key}</span>
                <code className="text-sm text-emerald-400">{val}</code>
              </div>
            ))}
          </div>
        ) : (
          <span className="text-sm text-zinc-500">no commands detected</span>
        )}
      </div>

      {/* Conventions */}
      <div className={section}>
        <h3 className={sectionTitle}>Conventions</h3>
        {knowledge.conventions.length > 0 ? (
          <ul className="list-inside list-disc space-y-1">
            {knowledge.conventions.map((c, i) => (
              <li key={i} className="text-sm text-zinc-300">{c}</li>
            ))}
          </ul>
        ) : (
          <span className="text-sm text-zinc-500">no conventions detected</span>
        )}
      </div>

      {/* Layout & Protected */}
      <div className="grid grid-cols-2 gap-4">
        <div className={section}>
          <h3 className={sectionTitle}>Layout</h3>
          <div className="flex flex-wrap gap-1">
            {knowledge.layout?.dirs?.length > 0
              ? knowledge.layout.dirs.map((d) => <Badge key={d} text={d} color="bg-zinc-600" />)
              : <span className="text-sm text-zinc-500">—</span>}
          </div>
        </div>
        <div className={section}>
          <h3 className={sectionTitle}>Protected globs</h3>
          <div className="flex flex-wrap gap-1">
            {knowledge.protected_globs.length > 0
              ? knowledge.protected_globs.map((g) => (
                  <code key={g} className="rounded bg-zinc-950 px-2 py-1 text-xs text-amber-400">{g}</code>
                ))
              : <span className="text-sm text-zinc-500">—</span>}
          </div>
        </div>
      </div>
    </div>
  );
}

// --- Agent-Ception (multi-agent planning assistant) -------------------------

const PLANNING_SESSION_COLORS: Record<string, string> = {
  active: "bg-amber-500",
  stable: "bg-sky-500",
  completed: "bg-emerald-600",
  cancelled: "bg-zinc-500",
};

const PLANNING_NODE_COLORS: Record<string, string> = {
  proposed: "bg-zinc-600",
  refined: "bg-amber-600",
  approved: "bg-emerald-600",
};

function usePlanningStream(sessionId: string | null): EventRow[] {
  const [events, setEvents] = useState<EventRow[]>([]);
  useEffect(() => {
    setEvents([]);
    if (!sessionId) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(
      `${proto}://${location.host}/ws/planning?session_id=${sessionId}`,
    );
    ws.onmessage = (msg) => {
      try {
        const ev = JSON.parse(msg.data) as EventRow;
        setEvents((prev) => [...prev.slice(-199), ev]);
      } catch {
        /* ignore malformed messages */
      }
    };
    return () => ws.close();
  }, [sessionId]);
  return events;
}

export function AgentCeptionPanel({ projectId }: { projectId: string }) {
  const [sessions, setSessions] = useState<PlanningSession[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<PlanningMessage[]>([]);
  const [taskNodes, setTaskNodes] = useState<PlanningTaskNode[]>([]);
  const [prompt, setPrompt] = useState("");
  const [title, setTitle] = useState("");
  const [humanInput, setHumanInput] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  const events = usePlanningStream(activeSessionId);

  const reloadSessions = useCallback(async () => {
    try {
      const ss = await api.listPlanningSessions(projectId);
      setSessions(ss);
    } catch {
      /* ignore */
    }
  }, [projectId]);

  const loadSessionData = useCallback(
    async (sid: string) => {
      try {
        const [msgs, nodes] = await Promise.all([
          api.listPlanningMessages(sid),
          api.listPlanningTaskNodes(sid),
        ]);
        setMessages(msgs);
        setTaskNodes(nodes);
      } catch {
        /* ignore */
      }
    },
    [],
  );

  // Load sessions on mount
  useEffect(() => {
    reloadSessions();
  }, [reloadSessions]);

  // Auto-select first session if none active
  useEffect(() => {
    if (!activeSessionId && sessions.length > 0) {
      const active = sessions.find(
        (s) => s.status === "active" || s.status === "stable",
      );
      setActiveSessionId(active?.id ?? sessions[0].id);
    }
  }, [sessions, activeSessionId]);

  // Load data when session changes
  useEffect(() => {
    if (activeSessionId) loadSessionData(activeSessionId);
  }, [activeSessionId, loadSessionData]);

  // React to real-time planning events
  useEffect(() => {
    if (!activeSessionId || events.length === 0) return;
    const last = events[events.length - 1];
    if (last.payload?.planning_session_id !== activeSessionId) return;
    const t = last.type;
    if (
      t.startsWith("planning.agent_turn") ||
      t.startsWith("planning.task") ||
      t.startsWith("planning.session_completed")
    ) {
      loadSessionData(activeSessionId);
    }
    if (
      t.includes("created") ||
      t.includes("completed") ||
      t.includes("cancelled")
    ) {
      reloadSessions();
    }
  }, [events, activeSessionId, loadSessionData, reloadSessions]);

  // Auto-scroll chat
  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function createSession() {
    if (!prompt.trim()) return;
    setCreating(true);
    setError("");
    try {
      const session = await api.createPlanningSession(projectId, {
        title: title || prompt.slice(0, 60),
        prompt,
      });
      setPrompt("");
      setTitle("");
      setActiveSessionId(session.id);
      await reloadSessions();
    } catch (e) {
      setError(String(e));
    } finally {
      setCreating(false);
    }
  }

  async function sendMessage() {
    if (!humanInput.trim() || !activeSessionId) return;
    const text = humanInput;
    setHumanInput("");
    try {
      await api.addPlanningMessage(activeSessionId, text);
      await loadSessionData(activeSessionId);
    } catch (e) {
      setError(String(e));
    }
  }

  async function approveSession() {
    if (!activeSessionId) return;
    setError("");
    try {
      await api.approvePlanningSession(activeSessionId);
      await reloadSessions();
      await loadSessionData(activeSessionId);
    } catch (e) {
      setError(String(e));
    }
  }

  async function cancelSession() {
    if (!activeSessionId) return;
    try {
      await api.cancelPlanningSession(activeSessionId);
      await reloadSessions();
    } catch (e) {
      setError(String(e));
    }
  }

  const activeSession = sessions.find((s) => s.id === activeSessionId) ?? null;
  const isStable = activeSession?.status === "stable";
  const isActive = activeSession?.status === "active";

  // Build tree from flat node list
  const tree = buildTree(taskNodes);

  return (
    <div className="grid grid-cols-[240px_1fr_300px] gap-4 h-full">
      {/* Left: session list + new session form */}
      <div className="space-y-2 rounded-lg border border-zinc-800 bg-zinc-900 p-3 overflow-y-auto">
        <h3 className="text-sm font-semibold text-zinc-300">Sessions</h3>
        {sessions.map((s) => (
          <div
            key={s.id}
            className={`cursor-pointer rounded p-2 text-sm ${
              activeSessionId === s.id
                ? "bg-emerald-700 text-white"
                : "text-zinc-200 hover:bg-zinc-800"
            }`}
            onClick={() => setActiveSessionId(s.id)}
          >
            <div className="truncate font-medium">
              {s.title || s.prompt.slice(0, 40)}
            </div>
            <div className="flex items-center gap-1 mt-1">
              <Badge
                text={s.status}
                color={PLANNING_SESSION_COLORS[s.status] ?? "bg-zinc-600"}
              />
              <span className="text-xs text-zinc-400">t{s.turn_number}</span>
            </div>
          </div>
        ))}
        {sessions.length === 0 && (
          <div className="text-xs text-zinc-500">
            No sessions yet. Create one below.
          </div>
        )}

        {/* New session form */}
        <div className="border-t border-zinc-800 pt-2 mt-2">
          <textarea
            className={`${input} h-20 resize-none text-xs`}
            placeholder="Describe the feature to implement…"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
          />
          <input
            className={`${input} mt-1 text-xs`}
            placeholder="Title (optional)"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
          <Button
            variant="primary"
            onClick={createSession}
            disabled={creating || !prompt.trim()}
          >
            {creating ? "Starting…" : "New Session"}
          </Button>
        </div>
      </div>

      {/* Center: chat */}
      <div className="flex flex-col rounded-lg border border-zinc-800 bg-zinc-900 overflow-hidden">
        {/* Header */}
        {activeSession && (
          <div className="flex items-center justify-between border-b border-zinc-800 px-3 py-2">
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-zinc-200 truncate max-w-[320px]">
                {activeSession.title || activeSession.prompt.slice(0, 60)}
              </span>
              <Badge
                text={activeSession.status}
                color={
                  PLANNING_SESSION_COLORS[activeSession.status] ?? "bg-zinc-600"
                }
              />
            </div>
            <div className="flex gap-1">
              {isActive && (
                <Button variant="ghost" onClick={cancelSession}>
                  Cancel
                </Button>
              )}
              {isStable && (
                <Button variant="primary" onClick={approveSession}>
                  Approve &amp; Create Tasks
                </Button>
              )}
            </div>
          </div>
        )}

        {/* Messages */}
        <div className="flex-1 overflow-y-auto p-3 space-y-3">
          {messages.length === 0 && activeSessionId && (
            <div className="text-sm text-zinc-500 text-center py-8">
              {isActive
                ? "Agents are discussing… messages will appear here in real time."
                : "No messages yet."}
            </div>
          )}
          {!activeSessionId && (
            <div className="text-sm text-zinc-500 text-center py-8">
              Select or create a planning session.
            </div>
          )}
          {messages.map((msg) => (
            <div
              key={msg.id}
              className={`flex ${
                msg.role === "human" ? "justify-end" : "justify-start"
              }`}
            >
              <div
                className={`max-w-[80%] rounded-lg p-3 ${
                  msg.role === "human"
                    ? "bg-emerald-700 text-white"
                    : "bg-zinc-800 text-zinc-100"
                }`}
              >
                <div className="flex items-center gap-2 mb-1">
                  <span className="text-xs font-semibold uppercase tracking-wide opacity-70">
                    {msg.role === "human" ? "You" : msg.agent}
                  </span>
                  <span className="text-xs opacity-40">
                    turn {msg.turn_number}
                  </span>
                </div>
                <div className="text-sm whitespace-pre-wrap leading-relaxed">
                  {msg.content}
                </div>
              </div>
            </div>
          ))}
          <div ref={endRef} />
        </div>

        {/* Input bar */}
        {activeSessionId && (isActive || isStable) && (
          <div className="border-t border-zinc-800 p-3 flex gap-2">
            <textarea
              className={`${input} flex-1 resize-none h-10`}
              placeholder={
                isActive
                  ? "Interject to steer the discussion… (Enter to send)"
                  : "Add a final message…"
              }
              value={humanInput}
              onChange={(e) => setHumanInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  sendMessage();
                }
              }}
            />
            <Button onClick={sendMessage} disabled={!humanInput.trim()}>
              Send
            </Button>
          </div>
        )}
      </div>

      {/* Right: task tree */}
      <div className="rounded-lg border border-zinc-800 bg-zinc-900 p-3 overflow-y-auto">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold text-zinc-300">
            Tasks ({taskNodes.length})
          </h3>
          {isStable && (
            <Button variant="primary" onClick={approveSession}>
              Approve All
            </Button>
          )}
        </div>
        {error && (
          <div className="mb-2 rounded bg-red-950 p-2 text-xs text-red-300">
            {error}
          </div>
        )}
        {tree.length === 0 && (
          <div className="text-sm text-zinc-500">
            No tasks yet. Start a session to begin the planning discussion.
          </div>
        )}
        {tree.map((node) => (
          <TaskTreeNode key={node.id} node={node} depth={0} />
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------------ */
/* Task tree helpers                                                        */
/* ------------------------------------------------------------------------ */

interface TreeNode extends PlanningTaskNode {
  children: TreeNode[];
}

function buildTree(nodes: PlanningTaskNode[]): TreeNode[] {
  const map = new Map<string, TreeNode>();
  const roots: TreeNode[] = [];
  for (const n of nodes) {
    map.set(n.id, { ...n, children: [] });
  }
  for (const n of nodes) {
    const node = map.get(n.id)!;
    if (n.parent_id && map.has(n.parent_id)) {
      map.get(n.parent_id)!.children.push(node);
    } else {
      roots.push(node);
    }
  }
  return roots;
}

function TaskTreeNode({ node, depth }: { node: TreeNode; depth: number }) {
  const [expanded, setExpanded] = useState(true);
  const hasChildren = node.children.length > 0;
  const statusColor =
    PLANNING_NODE_COLORS[node.status] ?? "bg-zinc-600";

  return (
    <div>
      <div
        className="flex items-center gap-1 py-1 group"
        style={{ paddingLeft: `${depth * 16}px` }}
      >
        <button
          onClick={() => setExpanded(!expanded)}
          className="text-zinc-600 hover:text-zinc-300 text-xs w-4 text-left shrink-0"
        >
          {hasChildren ? (expanded ? "▾" : "▸") : " "}
        </button>
        <Badge text={node.status} color={statusColor} />
        <span
          className="text-sm text-zinc-200 truncate"
          title={node.description || node.title}
        >
          {node.title}
        </span>
        {node.task_id && (
          <span
            className="text-xs text-emerald-500 shrink-0"
            title="Linked to real task"
          >
            ✓
          </span>
        )}
      </div>
      {expanded &&
        node.children.map((child) => (
          <TaskTreeNode key={child.id} node={child} depth={depth + 1} />
        ))}
    </div>
  );
}
