import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "../api";
import type { EventRow, Task, Verdict } from "../types";
import { useStream } from "../useStream";
import {
  Badge,
  Button,
  Spinner,
  input,
  STATE_COLORS,
  VERDICT_COLORS,
  fmtTime,
  fmtTokens,
} from "../ui";

const PAGE_SIZE = 15;

// Long free-text (result summaries, verdict reasons, requests) collapses to a
// short preview with a "Show more" toggle, per the global collapsible-text rule.
const TEXT_COLLAPSE_THRESHOLD = 160;

// Honest filter taxonomy (no dedicated `blocked` state exists in the schema).
type FilterKey = "all" | "active" | "done" | "failed" | "blocked";

const FILTERS: { key: FilterKey; label: string; states: string[] | null }[] = [
  { key: "all", label: "All", states: null },
  { key: "active", label: "Active", states: ["in_progress", "approved"] },
  { key: "done", label: "Done", states: ["done"] },
  { key: "failed", label: "Failed", states: ["failed"] },
  { key: "blocked", label: "Blocked", states: ["inbox", "cancelled"] },
];

// Sort weight so Active floats to the top of the "All" view.
const STATE_RANK: Record<string, number> = {
  in_progress: 0,
  approved: 1,
  inbox: 2,
  failed: 3,
  done: 4,
  cancelled: 5,
};

// ----------------------------------------------------------------------------
// API paging wrapper. The backend does not yet honor limit/offset, so today we
// fetch the full set and clamp to [offset, offset+limit) client-side over the
// server's newest-first list. The signature matches `api.listTasks` options so
// once the backend honors pagination the body can pass `opts` straight through.
// ----------------------------------------------------------------------------
async function fetchTasksPage(
  projectId: string,
  opts: { state?: string; limit: number; offset: number },
): Promise<Task[]> {
  const rows = await api.listTasks(projectId, opts.state);
  return rows.slice(opts.offset, opts.offset + opts.limit);
}

// ============================================================================
// TasksPanel
// ============================================================================

export function TasksPanel({ projectId }: { projectId: string }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [selected, setSelected] = useState<string | null>(null);
  const [verdicts, setVerdicts] = useState<Verdict[]>([]);

  const [request, setRequest] = useState("");
  const [autoApprove, setAutoApprove] = useState(true);
  const [creating, setCreating] = useState(false);

  const [filter, setFilter] = useState<FilterKey>("all");
  const [query, setQuery] = useState("");
  const [visible, setVisible] = useState(PAGE_SIZE);

  // Live event stream → cheap per-task stage/agent hint for in_progress rows.
  const { events } = useStream(projectId);
  const liveHint = useMemo(() => buildLiveHints(events), [events]);

  const reload = useCallback(async () => {
    try {
      // Fetch the FULL project task set so the parent/child tree stays intact,
      // then paginate at the ROOT level below (pageRoots).
      const rows = await fetchTasksPage(projectId, {
        limit: 1000,
        offset: 0,
      });
      setTasks(rows);
      setError("");
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  // Reset view when the project changes.
  useEffect(() => {
    setLoading(true);
    setTasks([]);
    setSelected(null);
    setFilter("all");
    setQuery("");
    setVisible(PAGE_SIZE);
  }, [projectId]);

  useEffect(() => {
    reload();
    const id = setInterval(reload, 3000);
    return () => clearInterval(id);
  }, [reload]);

  useEffect(() => {
    if (!selected) {
      setVerdicts([]);
      return;
    }
    api.taskVerdicts(selected).then(setVerdicts).catch(() => setVerdicts([]));
  }, [selected, tasks]);

  async function create() {
    if (!request.trim()) return;
    setError("");
    setCreating(true);
    try {
      await api.createTask(projectId, {
        request,
        title: "",
        use_planner: null,
        auto_approve: autoApprove,
      });
      setRequest("");
      await reload();
    } catch (e) {
      setError(String(e));
    } finally {
      setCreating(false);
    }
  }

  // ---- filtering / search / paging over ROOT tasks -------------------------
  const matchesText = useCallback(
    (t: Task) => {
      const q = query.trim().toLowerCase();
      if (!q) return true;
      return (
        (t.title ?? "").toLowerCase().includes(q) ||
        (t.request ?? "").toLowerCase().includes(q)
      );
    },
    [query],
  );

  const activeFilter = FILTERS.find((f) => f.key === filter)!;

  // Counts for chips (computed over the full loaded set, ignoring text query).
  const counts = useMemo(() => {
    const c: Record<FilterKey, number> = {
      all: tasks.length,
      active: 0,
      done: 0,
      failed: 0,
      blocked: 0,
    };
    for (const t of tasks) {
      for (const f of FILTERS) {
        if (f.key === "all") continue;
        if (f.states!.includes(t.state)) c[f.key]++;
      }
    }
    return c;
  }, [tasks]);

  // Build the tree, then filter at the ROOT level (a matching descendant keeps
  // its root). Flat (parentless, childless) tasks coexist as single-node roots.
  const { roots, childrenOf } = useMemo(() => buildTaskTree(tasks), [tasks]);

  const filteredRoots = useMemo(() => {
    const stateOk = (t: Task) =>
      activeFilter.states === null || activeFilter.states.includes(t.state);

    // A root passes if it (or any descendant) matches both state + text.
    const subtreeMatches = (id: string): boolean => {
      const t = tasks.find((x) => x.id === id);
      if (t && stateOk(t) && matchesText(t)) return true;
      return (childrenOf.get(id) ?? []).some((c) => subtreeMatches(c.task.id));
    };

    const passing = roots.filter((r) => subtreeMatches(r.task.id));

    // Sort: in "all", pin Active on top via STATE_RANK then newest-first.
    return [...passing].sort((a, b) => {
      const ra = STATE_RANK[a.task.state] ?? 9;
      const rb = STATE_RANK[b.task.state] ?? 9;
      if (filter === "all" && ra !== rb) return ra - rb;
      return (
        new Date(b.task.created_at).getTime() -
        new Date(a.task.created_at).getTime()
      );
    });
  }, [roots, childrenOf, tasks, activeFilter, filter, matchesText]);

  const pageRoots = filteredRoots.slice(0, visible);
  const hasMore = filteredRoots.length > visible;

  // Reset paging when filter/query change.
  useEffect(() => {
    setVisible(PAGE_SIZE);
  }, [filter, query]);

  const selectedTask = useMemo(
    () => tasks.find((t) => t.id === selected) ?? null,
    [tasks, selected],
  );

  // Click an already-selected row to deselect (toggle the verdicts view).
  const toggleSelect = useCallback(
    (id: string) => setSelected((cur) => (cur === id ? null : id)),
    [],
  );

  return (
    // Single-column on phones; two columns from lg up. No fixed height —
    // content flows inside the parent accordion <Section>.
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      {/* ------------------------------------------------------------------ */}
      {/* Left column: create + filters + list                                */}
      {/* ------------------------------------------------------------------ */}
      <div className="min-w-0 space-y-3">
        {/* Create task */}
        <div className="rounded-xl border border-zinc-800 bg-zinc-900 p-4">
          <textarea
            className={`${input} h-24 resize-none`}
            placeholder="Describe the task for the team…"
            value={request}
            onChange={(e) => setRequest(e.target.value)}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                e.preventDefault();
                create();
              }
            }}
          />
          <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
            <label
              className="flex min-h-[44px] cursor-pointer items-center gap-2 text-sm text-zinc-400"
              title="When checked, the task bypasses the inbox and starts executing immediately."
            >
              <input
                type="checkbox"
                className="h-4 w-4 accent-indigo-500"
                checked={autoApprove}
                onChange={(e) => setAutoApprove(e.target.checked)}
              />
              Auto-approve (run immediately)
            </label>
            <Button
              variant="primary"
              onClick={create}
              disabled={creating || !request.trim()}
              title="Create task (Cmd/Ctrl+Enter)"
            >
              {creating ? (
                <span className="flex items-center gap-2">
                  <Spinner size={14} /> Creating…
                </span>
              ) : (
                "Create task"
              )}
            </Button>
          </div>
        </div>

        {error && (
          <div
            role="alert"
            className="rounded-xl border border-rose-900/60 bg-rose-950/60 p-3 text-sm text-rose-300"
          >
            {error}
          </div>
        )}

        {/* Filter chips + search */}
        <div className="space-y-2">
          {/* Horizontally scrollable single row on phones (Linear/GitHub
              pattern) so the chips never wrap into the search bar. */}
          <div className="-mx-1 flex items-center gap-1.5 overflow-x-auto px-1 pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
            {FILTERS.map((f) => {
              const active = filter === f.key;
              const n = counts[f.key];
              return (
                <button
                  key={f.key}
                  type="button"
                  title={chipTitle(f.key)}
                  aria-pressed={active}
                  onClick={() => setFilter(f.key)}
                  className={`flex min-h-[36px] shrink-0 items-center rounded-lg px-3 py-1.5 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-indigo-500/50 ${
                    active
                      ? "bg-indigo-500/15 text-indigo-300 ring-1 ring-indigo-500/40"
                      : "text-zinc-400 hover:bg-zinc-800/60 hover:text-zinc-200"
                  }`}
                >
                  {f.label}
                  <span
                    className={`ml-1.5 tabular-nums ${
                      active ? "text-indigo-400/80" : "text-zinc-600"
                    }`}
                  >
                    {n}
                  </span>
                </button>
              );
            })}
          </div>
          <div className="relative">
            <input
              className={`${input} pl-9`}
              placeholder="Search title or request…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              aria-label="Search tasks"
            />
            <svg
              viewBox="0 0 20 20"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.6"
              aria-hidden="true"
              className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500"
            >
              <circle cx="9" cy="9" r="5.5" />
              <path d="m17 17-3.5-3.5" strokeLinecap="round" />
            </svg>
            {query && (
              <button
                type="button"
                title="Clear search"
                aria-label="Clear search"
                onClick={() => setQuery("")}
                className="absolute right-1.5 top-1/2 flex h-7 w-7 -translate-y-1/2 items-center justify-center rounded text-zinc-500 hover:text-zinc-200 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-indigo-500/50"
              >
                <svg
                  viewBox="0 0 20 20"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.6"
                  aria-hidden="true"
                  className="h-3.5 w-3.5"
                >
                  <path d="m5 5 10 10M15 5 5 15" strokeLinecap="round" />
                </svg>
              </button>
            )}
          </div>
        </div>

        {/* List body: loading / empty / error / rows */}
        <div className="space-y-1.5">
          {loading ? (
            <ListSkeleton />
          ) : tasks.length === 0 ? (
            <EmptyState
              title="No tasks yet"
              hint="Create one above to put the team to work."
            />
          ) : pageRoots.length === 0 ? (
            <EmptyState
              title="No matches"
              hint="Try a different filter or clear the search."
            />
          ) : (
            <>
              {pageRoots.map((node) => (
                <TaskTreeItem
                  key={node.task.id}
                  node={node}
                  childrenOf={childrenOf}
                  selectedId={selected}
                  onSelect={toggleSelect}
                  onReload={reload}
                  liveHint={liveHint}
                  depth={0}
                />
              ))}

              {/* Pager footer */}
              <div className="flex items-center justify-between pt-1 text-xs text-zinc-500">
                <span className="tabular-nums">
                  showing {pageRoots.length} of {filteredRoots.length}
                </span>
                {hasMore && (
                  <Button
                    variant="ghost"
                    onClick={() => setVisible((v) => v + PAGE_SIZE)}
                  >
                    Load more
                  </Button>
                )}
              </div>
            </>
          )}
        </div>
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Right column: verdicts. Only rendered once a task is selected so it  */}
      {/* never sits as a big empty block above the list on mobile.           */}
      {/* ------------------------------------------------------------------ */}
      {selectedTask && (
        <div className="min-w-0 rounded-xl border border-zinc-800 bg-zinc-900 p-4">
          <div className="mb-3 flex items-center justify-between gap-2">
            <h3 className="min-w-0 text-sm font-semibold uppercase tracking-wide text-zinc-300">
              Verdicts
            </h3>
            <button
              type="button"
              title="Close verdicts"
              aria-label="Close verdicts"
              onClick={() => setSelected(null)}
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded text-zinc-500 hover:text-zinc-200 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-indigo-500/50"
            >
              <svg
                viewBox="0 0 20 20"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.6"
                aria-hidden="true"
                className="h-3.5 w-3.5"
              >
                <path d="m5 5 10 10M15 5 5 15" strokeLinecap="round" />
              </svg>
            </button>
          </div>

          <div
            className="mb-3 truncate text-xs text-zinc-500"
            title={selectedTask.title || selectedTask.request}
          >
            {selectedTask.title || selectedTask.request}
          </div>

          {verdicts.length === 0 ? (
            selectedTask.state === "in_progress" ||
            selectedTask.state === "approved" ? (
              <div
                role="status"
                className="flex items-center justify-center gap-2 rounded-xl border border-dashed border-zinc-800 bg-zinc-900/40 p-8 text-center text-sm text-zinc-400"
              >
                <Spinner size={14} /> Awaiting review…
              </div>
            ) : (
              <EmptyState
                title="No verdicts"
                hint="No reviews were recorded for this task."
              />
            )
          ) : (
            <div className="space-y-2">
              {verdicts.map((v) => (
                <div
                  key={v.id}
                  className="rounded-lg border border-zinc-800 bg-zinc-950/40 p-3 transition-colors"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="min-w-0 truncate text-sm font-medium text-zinc-200">
                      {v.agent}
                    </span>
                    <span
                      className={`shrink-0 text-sm font-semibold ${
                        VERDICT_COLORS[v.verdict] ?? "text-zinc-400"
                      }`}
                    >
                      {v.verdict}
                    </span>
                  </div>
                  <div className="mt-0.5 text-xs text-zinc-500 tabular-nums">
                    attempt #{v.attempt} · grounded {v.grounded_count}
                  </div>
                  {v.reason && (
                    <CollapsibleText
                      text={v.reason}
                      className="mt-1.5 text-xs leading-relaxed text-zinc-300"
                    />
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ============================================================================
// Page-local helpers + sub-components
// ============================================================================

function chipTitle(key: FilterKey): string {
  switch (key) {
    case "active":
      return "Running or approved (in_progress + approved)";
    case "done":
      return "Completed successfully";
    case "failed":
      return "Failed attempts";
    case "blocked":
      return "Awaiting a human decision or stopped (inbox + cancelled)";
    default:
      return "All tasks";
  }
}

/**
 * Long free-text with a collapsed preview + "Show more" / "Show less" toggle.
 * Preserves newlines and wraps long words. Short text renders inline with no
 * toggle. Matches the global collapsible-long-text mandate.
 */
function CollapsibleText({
  text,
  className = "",
}: {
  text: string;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const long = text.length > TEXT_COLLAPSE_THRESHOLD;
  const shown =
    long && !open ? `${text.slice(0, TEXT_COLLAPSE_THRESHOLD).trimEnd()}…` : text;

  return (
    <div className={className}>
      <span className="whitespace-pre-wrap break-words">{shown}</span>
      {long && (
        <button
          type="button"
          aria-expanded={open}
          onClick={(e) => {
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

/** Honest elapsed: created→updated (INCLUDES queue wait; no real exec start). */
function fmtElapsed(createdIso: string, updatedIso: string): string {
  const ms = new Date(updatedIso).getTime() - new Date(createdIso).getTime();
  if (!Number.isFinite(ms) || ms <= 0) return "<1s";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

const ELAPSED_TITLE =
  "Elapsed from created → updated. Includes queue wait — there is no true start-of-execution timestamp.";

// --- task tree --------------------------------------------------------------
interface TaskTreeNode {
  task: Task;
  children: TaskTreeNode[];
}

/**
 * Build a forest from the flat task list. Returns the root nodes plus a
 * children index keyed by task id (used for cheap subtree filtering).
 * Children are ordered oldest→newest (sequential decomposition order);
 * roots are ordered newest→oldest (the caller re-sorts for the pinned view).
 */
function buildTaskTree(tasks: Task[]): {
  roots: TaskTreeNode[];
  childrenOf: Map<string, TaskTreeNode[]>;
} {
  const map = new Map<string, TaskTreeNode>();
  const roots: TaskTreeNode[] = [];
  for (const t of tasks) map.set(t.id, { task: t, children: [] });
  for (const t of tasks) {
    const node = map.get(t.id)!;
    if (t.parent_task_id && map.has(t.parent_task_id)) {
      map.get(t.parent_task_id)!.children.push(node);
    } else {
      roots.push(node);
    }
  }
  const childrenOf = new Map<string, TaskTreeNode[]>();
  for (const [id, n] of map) {
    n.children.sort(
      (a, b) =>
        new Date(a.task.created_at).getTime() -
        new Date(b.task.created_at).getTime(),
    );
    childrenOf.set(id, n.children);
  }
  roots.sort(
    (a, b) =>
      new Date(b.task.created_at).getTime() -
      new Date(a.task.created_at).getTime(),
  );
  return { roots, childrenOf };
}

// --- live hints from the event stream ---------------------------------------
interface LiveHint {
  type: string;
  agent: string | null;
  text: string;
}

/** Most-recent streamed event per task_id, condensed to a short stage label. */
function buildLiveHints(events: EventRow[]): Map<string, LiveHint> {
  const m = new Map<string, LiveHint>();
  for (const ev of events) {
    if (!ev.task_id) continue;
    m.set(ev.task_id, {
      type: ev.type,
      agent: ev.agent,
      text: stageLabel(ev),
    });
  }
  return m;
}

function stageLabel(ev: EventRow): string {
  const p = ev.payload ?? {};
  switch (ev.type) {
    case "attempt.started":
      return `attempt #${String(p.n ?? "?")} started`;
    case "attempt.failed":
      return `attempt #${String(p.n ?? "?")} failed @ ${String(p.stage ?? "?")}`;
    case "pipeline.derived":
      return "deriving reviewers";
    case "agent.result":
      return `agent ${String(p.ok) === "true" ? "ok" : "ran"}`;
    case "agent.verdict":
      return `verdict ${String(p.verdict ?? "")}`;
    default:
      return ev.type.replace(/[._]/g, " ");
  }
}

// --- async-state helpers ----------------------------------------------------
function ListSkeleton() {
  return (
    <div className="space-y-1.5" aria-busy="true" aria-label="Loading tasks">
      {Array.from({ length: 4 }).map((_, i) => (
        <div
          key={i}
          className="animate-pulse rounded-xl border border-zinc-800 bg-zinc-900 p-3"
        >
          <div className="flex items-center gap-2">
            <div className="h-4 w-16 rounded bg-zinc-800" />
            <div className="h-4 flex-1 rounded bg-zinc-800/70" />
          </div>
          <div className="mt-2 h-3 w-2/3 rounded bg-zinc-800/50" />
        </div>
      ))}
    </div>
  );
}

function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="rounded-xl border border-dashed border-zinc-800 bg-zinc-900/40 p-8 text-center">
      <div className="text-sm font-medium text-zinc-400">{title}</div>
      {hint && <div className="mt-1 text-xs text-zinc-600">{hint}</div>}
    </div>
  );
}

// --- a single task row (tree-aware) -----------------------------------------
const TERMINAL = new Set(["done", "failed", "cancelled"]);
// Cap nesting indent so deep trees never push content off a narrow viewport.
const INDENT_STEP = 14;
const INDENT_MAX = 42;

function TaskTreeItem({
  node,
  childrenOf,
  selectedId,
  onSelect,
  onReload,
  liveHint,
  depth,
}: {
  node: TaskTreeNode;
  childrenOf: Map<string, TaskTreeNode[]>;
  selectedId: string | null;
  onSelect: (id: string) => void;
  onReload: () => void;
  liveHint: Map<string, LiveHint>;
  depth: number;
}) {
  const t = node.task;
  const children = childrenOf.get(t.id) ?? node.children;
  const hasChildren = children.length > 0;

  const [expanded, setExpanded] = useState(depth === 0);
  const [showDetails, setShowDetails] = useState(false);
  const [actioning, setActioning] = useState(false);
  const [usage, setUsage] = useState<{
    total_turns: number;
    input_tokens: number;
    output_tokens: number;
    cache_read_tokens: number;
    cache_creation_tokens: number;
    agent_count: number;
  } | null>(null);
  const [usageLoading, setUsageLoading] = useState(false);

  const fetchedRef = useRef(false);
  const prevStateRef = useRef(t.state);

  const loadUsage = useCallback(() => {
    setUsageLoading(true);
    api
      .taskUsage(t.id)
      .then((u) => setUsage(u))
      .catch(() => {})
      .finally(() => setUsageLoading(false));
  }, [t.id]);

  // Fetch usage lazily: on first detail-expand, and whenever the task ENTERS a
  // terminal state (fixes the stale "0 tokens right after done" bug). We avoid
  // polling usage for collapsed, non-terminal rows.
  useEffect(() => {
    const prev = prevStateRef.current;
    const enteredTerminal = !TERMINAL.has(prev) && TERMINAL.has(t.state);
    prevStateRef.current = t.state;
    if (enteredTerminal) {
      loadUsage();
      fetchedRef.current = true;
    }
  }, [t.state, loadUsage]);

  useEffect(() => {
    if (showDetails && !fetchedRef.current) {
      fetchedRef.current = true;
      loadUsage();
    }
  }, [showDetails, loadUsage]);

  // Wrap an async action with a local in-flight flag so the buttons can show a
  // spinner and refuse double-submits, then always refresh the list.
  const runAction = useCallback(
    async (e: React.MouseEvent, fn: () => Promise<unknown>) => {
      e.stopPropagation();
      if (actioning) return;
      setActioning(true);
      try {
        await fn();
      } finally {
        setActioning(false);
        onReload();
      }
    },
    [actioning, onReload],
  );

  const handleApprove = (e: React.MouseEvent) =>
    runAction(e, () => api.approve(t.id));
  const handleCascade = (e: React.MouseEvent) =>
    runAction(e, () => api.cascadeApprove(t.id));
  const handleCancel = (e: React.MouseEvent) =>
    runAction(e, () => api.cancel(t.id));

  const selected = selectedId === t.id;
  const running = t.state === "in_progress";
  const hint = running ? liveHint.get(t.id) : undefined;
  const terminal = TERMINAL.has(t.state);

  const indent = Math.min(depth * INDENT_STEP, INDENT_MAX);

  return (
    <div>
      <div
        className={`cursor-pointer rounded-xl border p-3 transition-colors ${
          selected
            ? "border-indigo-500/60 bg-indigo-500/10"
            : "border-zinc-800 bg-zinc-900 hover:bg-zinc-800/50"
        } ${depth ? "border-l-2 border-l-indigo-500/30" : ""}`}
        style={depth ? { marginLeft: indent } : undefined}
        onClick={() => onSelect(t.id)}
      >
        {/* Row 1: expand · state · title · time range. The leading control is a
            fixed-width flex child; the body is flex-1 so sub-rows align without
            magic left-margins. */}
        <div className="flex items-start gap-2">
          {hasChildren ? (
            <button
              type="button"
              title={expanded ? "Collapse subtasks" : "Expand subtasks"}
              aria-label={expanded ? "Collapse subtasks" : "Expand subtasks"}
              aria-expanded={expanded}
              onClick={(e) => {
                e.stopPropagation();
                setExpanded((v) => !v);
              }}
              className="-m-1 flex h-7 w-7 shrink-0 items-center justify-center rounded p-1 text-zinc-500 hover:text-zinc-300 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-indigo-500/50"
            >
              <svg
                viewBox="0 0 20 20"
                fill="currentColor"
                aria-hidden="true"
                className={`h-3.5 w-3.5 transition-transform duration-150 ${
                  expanded ? "rotate-90" : ""
                }`}
              >
                <path
                  fillRule="evenodd"
                  d="M7.21 14.77a.75.75 0 0 1 .02-1.06L11.168 10 7.23 6.29a.75.75 0 1 1 1.04-1.08l4.5 4.25a.75.75 0 0 1 0 1.08l-4.5 4.25a.75.75 0 0 1-1.06-.02Z"
                  clipRule="evenodd"
                />
              </svg>
            </button>
          ) : (
            <span className="w-5 shrink-0" aria-hidden="true" />
          )}

          <div className="min-w-0 flex-1">
            {/* Title line: badge + (count) + title + timestamps. Wraps cleanly
                on narrow screens instead of forcing horizontal overflow. */}
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <Badge text={t.state} color={STATE_COLORS[t.state]} />
              {hasChildren && (
                <span
                  className="shrink-0 text-xs font-medium tabular-nums text-zinc-500"
                  title={`${children.length} subtask(s)`}
                >
                  ×{children.length}
                </span>
              )}
              <span
                className="min-w-0 flex-1 truncate text-sm font-medium text-zinc-100"
                title={t.title || t.request}
              >
                {t.title || t.request}
              </span>
              <span className="flex shrink-0 items-center gap-1.5 text-xs tabular-nums text-zinc-500">
                <span
                  title={`Created ${new Date(t.created_at).toLocaleString()}`}
                >
                  {fmtTime(t.created_at)}
                </span>
                {terminal && (
                  <span
                    title={`Updated ${new Date(t.updated_at).toLocaleString()}`}
                  >
                    → {fmtTime(t.updated_at)}
                  </span>
                )}
              </span>
            </div>

            {/* Row 2: live hint (running) OR elapsed + token metrics */}
            <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-zinc-500">
              {running ? (
                <span
                  className="flex items-center gap-1.5 text-amber-400"
                  title="Latest streamed activity"
                >
                  <Spinner size={12} />
                  {hint ? (
                    <>
                      {hint.agent && (
                        <span className="font-medium text-amber-300">
                          [{hint.agent}]
                        </span>
                      )}
                      <span className="text-amber-400/90">{hint.text}</span>
                    </>
                  ) : (
                    <span className="text-amber-400/80">running…</span>
                  )}
                </span>
              ) : (
                <>
                  {terminal && (
                    <span title={ELAPSED_TITLE} className="text-zinc-500">
                      {fmtElapsed(t.created_at, t.updated_at)}{" "}
                      <span className="text-zinc-600">elapsed*</span>
                    </span>
                  )}
                  {usage && <UsageMetrics usage={usage} />}
                  {!usage && usageLoading && (
                    <span
                      role="status"
                      aria-label="Loading usage"
                      className="flex items-center gap-1.5 text-zinc-600"
                    >
                      <Spinner size={12} /> usage…
                    </span>
                  )}
                </>
              )}

              <button
                type="button"
                aria-expanded={showDetails}
                aria-label={
                  showDetails ? "Collapse task details" : "Expand task details"
                }
                onClick={(e) => {
                  e.stopPropagation();
                  setShowDetails((v) => !v);
                }}
                className="ml-auto inline-flex min-h-[28px] items-center rounded px-1.5 text-zinc-500 hover:text-zinc-300 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-indigo-500/50"
              >
                {showDetails ? "less" : "more"}
              </button>
            </div>

            {/* result_summary: full-width, collapsible (no fixed px width). */}
            {t.result_summary && (
              <CollapsibleText
                text={t.result_summary}
                className="mt-1.5 text-xs text-zinc-400"
              />
            )}

            {/* Details drawer */}
            {showDetails && (
              <div className="mt-2 space-y-1 rounded-lg bg-zinc-950/60 p-3 text-xs text-zinc-400">
                <Detail label="ID">
                  <span className="font-mono break-all text-zinc-500">
                    {t.id}
                  </span>
                </Detail>
                <Detail label="Created">
                  {new Date(t.created_at).toLocaleString()}
                </Detail>
                <Detail label="Updated">
                  {new Date(t.updated_at).toLocaleString()}
                </Detail>
                <Detail label="Elapsed">
                  <span title={ELAPSED_TITLE}>
                    {fmtElapsed(t.created_at, t.updated_at)}{" "}
                    <span className="text-zinc-600">(incl. queue wait)</span>
                  </span>
                </Detail>
                {t.level != null && <Detail label="Level">{t.level}</Detail>}
                {t.branch && (
                  <Detail label="Branch">
                    <span className="font-mono break-all">{t.branch}</span>
                  </Detail>
                )}
                {usage && (
                  <Detail label="Tokens">
                    <UsageMetrics usage={usage} />
                  </Detail>
                )}
                {t.request && t.request !== t.title && (
                  <div className="mt-1">
                    <CollapsibleText
                      text={t.request}
                      className="text-zinc-500"
                    />
                  </div>
                )}
              </div>
            )}

            {/* Actions */}
            {(t.state === "inbox" || t.state === "approved") && (
              <div className="mt-2 flex flex-wrap gap-2">
                {t.state === "inbox" && hasChildren && (
                  <Button
                    variant="primary"
                    onClick={handleCascade}
                    disabled={actioning}
                  >
                    {actioning ? (
                      <span className="flex items-center gap-2">
                        <Spinner size={14} /> Approving…
                      </span>
                    ) : (
                      "Approve tree"
                    )}
                  </Button>
                )}
                {t.state === "inbox" && !hasChildren && (
                  <Button
                    variant="primary"
                    onClick={handleApprove}
                    disabled={actioning}
                  >
                    {actioning ? (
                      <span className="flex items-center gap-2">
                        <Spinner size={14} /> Approving…
                      </span>
                    ) : (
                      "Approve"
                    )}
                  </Button>
                )}
                <Button
                  variant="ghost"
                  onClick={handleCancel}
                  disabled={actioning}
                >
                  Cancel
                </Button>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Children */}
      {expanded &&
        children.map((child) => (
          <div key={child.task.id} className="mt-1.5">
            <TaskTreeItem
              node={child}
              childrenOf={childrenOf}
              selectedId={selectedId}
              onSelect={onSelect}
              onReload={onReload}
              liveHint={liveHint}
              depth={depth + 1}
            />
          </div>
        ))}
    </div>
  );
}

function Detail({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex gap-2">
      <span className="w-16 shrink-0 text-zinc-600">{label}</span>
      <span className="min-w-0 flex-1">{children}</span>
    </div>
  );
}

/** Aligned token readout: in / out / cached / cache-write · turns · agents. */
function UsageMetrics({
  usage,
}: {
  usage: {
    total_turns: number;
    input_tokens: number;
    output_tokens: number;
    cache_read_tokens: number;
    cache_creation_tokens: number;
    agent_count: number;
  };
}) {
  return (
    <span className="inline-flex flex-wrap items-center gap-x-2.5 gap-y-0.5 tabular-nums text-zinc-500">
      <span title="Input tokens">
        <span aria-hidden="true">↓</span>
        <span className="sr-only">input </span>
        {fmtTokens(usage.input_tokens)}
      </span>
      <span title="Output tokens">
        <span aria-hidden="true">↑</span>
        <span className="sr-only">output </span>
        {fmtTokens(usage.output_tokens)}
      </span>
      <span title="Cache-read tokens">
        <span aria-hidden="true">⚡</span>
        <span className="sr-only">cache read </span>
        {fmtTokens(usage.cache_read_tokens)}
      </span>
      <span title="Cache-write tokens">
        <span aria-hidden="true">+</span>
        <span className="sr-only">cache write </span>
        {fmtTokens(usage.cache_creation_tokens)}
      </span>
      <span className="text-zinc-600" aria-hidden="true">
        ·
      </span>
      <span title="Total turns">{usage.total_turns}t</span>
      <span title="Agent invocations">{usage.agent_count}a</span>
    </span>
  );
}
