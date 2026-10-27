import type React from "react";
import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./api";
import { HistoryView } from "./pages/HistoryView";
import {
  AgentStudioView as AgentStudioViewBase,
  Button,
  SettingsView,
  Spinner,
  WorkspaceView,
  input,
} from "./panels";
import type {
  EventRow,
  Project,
  ProjectMode,
  StoreItem,
  TaskCheckpoint,
} from "./types";
import { useStream } from "./useStream";

export type MainView = "workspace" | "studio" | "history" | "settings";

const MAIN_VIEWS: { id: MainView; label: string }[] = [
  { id: "workspace", label: "Workspace" },
  { id: "studio", label: "Agent Studio" },
  { id: "history", label: "History & Logs" },
  { id: "settings", label: "Settings" },
];

const AgentStudioView = AgentStudioViewBase as React.ComponentType<{
  projectId?: string | null;
  project?: Project | null;
  lastEvent?: EventRow | null;
  liveNodeStatuses?: Record<string, TaskCheckpoint["status"]>;
  nodeStatuses?: Record<string, TaskCheckpoint["status"]>;
}>;

export default function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [mainView, setMainView] = useState<MainView>("workspace");
  const [globalStudioOpen, setGlobalStudioOpen] = useState(false);
  const [paused, setPaused] = useState(false);
  const [showAttach, setShowAttach] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastMcpCall, setLastMcpCall] = useState<{ tool: string; ts: number } | null>(
    null,
  );
  const [liveNodeStatuses, setLiveNodeStatuses] = useState<
    Record<string, TaskCheckpoint["status"]>
  >({});

  const stream = useStream(selectedId);

  const refreshProjects = useCallback(async () => {
    try {
      const ps = await api.listProjects();
      setProjects(ps);
      setSelectedId((cur) => {
        if (cur && ps.some((p) => p.id === cur)) return cur;
        return ps[0]?.id ?? null;
      });
    } catch {
      /* ignore background refresh errors */
    }
  }, []);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const ps = await api.listProjects();
      setProjects(ps);
      setSelectedId((cur) => {
        if (cur && ps.some((p) => p.id === cur)) return cur;
        return ps[0]?.id ?? null;
      });
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  useEffect(() => {
    setLiveNodeStatuses({});
  }, [selectedId]);

  useEffect(() => {
    const lastEvent = stream.lastEvent;
    if (!lastEvent) return;

    if (lastEvent.type === "project.created" && lastEvent.payload?.project) {
      const newProject = lastEvent.payload.project as unknown as Project;
      if (newProject && typeof newProject.id === "string") {
        setProjects((prev) => {
          const exists = prev.some((p) => p.id === newProject.id);
          return exists
            ? prev.map((p) => (p.id === newProject.id ? newProject : p))
            : [...prev, newProject];
        });
        setSelectedId((cur) => (cur === null ? newProject.id : cur));
      }
    } else if (
      lastEvent.type === "project.deleted" &&
      typeof lastEvent.payload?.project_id === "string"
    ) {
      const deletedId = lastEvent.payload.project_id;
      setProjects((prev) => {
        const next = prev.filter((p) => p.id !== deletedId);
        setSelectedId((cur) =>
          cur === deletedId ? (next[0]?.id ?? null) : cur,
        );
        return next;
      });
    } else if (lastEvent.type === "project.updated" || lastEvent.type === "workflow.evolved") {
      if (lastEvent.payload?.project) {
        const updated = lastEvent.payload.project as unknown as Project;
        if (updated && typeof updated.id === "string") {
          setProjects((prev) =>
            prev.map((p) => (p.id === updated.id ? updated : p)),
          );
        }
      }
      void refreshProjects();
    } else if (lastEvent.type === "system.reloaded") {
      if (lastEvent.payload?.frontend_rebuilt === true) {
        window.location.reload();
        return;
      }
      void refreshProjects();
    } else if (lastEvent.type === "mcp.tool_called") {
      const toolName =
        typeof lastEvent.payload?.tool === "string"
          ? lastEvent.payload.tool
          : "tool";
      setLastMcpCall({ tool: toolName, ts: Date.now() });
    } else if (lastEvent.type === "node.started") {
      const nodeId =
        typeof lastEvent.payload?.workflow_node_id === "string"
          ? lastEvent.payload.workflow_node_id
          : typeof lastEvent.payload?.node_id === "string"
            ? lastEvent.payload.node_id
            : null;
      if (nodeId) {
        setLiveNodeStatuses((prev) => ({
          ...prev,
          [nodeId]: "running",
        }));
      }
    } else if (
      lastEvent.type === "node.waiting_for_approval" ||
      lastEvent.type === "hitl.waiting"
    ) {
      const nodeId =
        typeof lastEvent.payload?.node_id === "string"
          ? lastEvent.payload.node_id
          : null;
      if (nodeId) {
        setLiveNodeStatuses((prev) => ({
          ...prev,
          [nodeId]: "waiting_for_approval",
        }));
      }
    } else if (
      lastEvent.type === "node.completed" ||
      lastEvent.type === "hitl.resolved"
    ) {
      const nodeId =
        typeof lastEvent.payload?.workflow_node_id === "string"
          ? lastEvent.payload.workflow_node_id
          : typeof lastEvent.payload?.node_id === "string"
            ? lastEvent.payload.node_id
            : null;
      const rawStatus = lastEvent.payload?.status;
      const status: TaskCheckpoint["status"] =
        rawStatus === "failed" ||
        rawStatus === "skipped" ||
        rawStatus === "running" ||
        rawStatus === "waiting_for_approval"
          ? rawStatus
          : "completed";
      if (nodeId) {
        setLiveNodeStatuses((prev) => ({
          ...prev,
          [nodeId]: status,
        }));
      }
    }
    if (Array.isArray(stream.events) && stream.events.length > 0) {
      setLiveNodeStatuses((prev) => {
        const next: Record<string, TaskCheckpoint["status"]> = { ...prev };
        for (const ev of stream.events) {
          const nid =
            typeof ev.payload?.workflow_node_id === "string"
              ? ev.payload.workflow_node_id
              : typeof ev.payload?.node_id === "string"
                ? ev.payload.node_id
                : null;
          if (!nid) continue;
          if (ev.type === "node.started") {
            next[nid] = "running";
          } else if (
            ev.type === "node.waiting_for_approval" ||
            ev.type === "hitl.waiting"
          ) {
            next[nid] = "waiting_for_approval";
          } else if (
            ev.type === "node.completed" ||
            ev.type === "hitl.resolved"
          ) {
            const rs = ev.payload?.status;
            next[nid] =
              rs === "failed" ||
              rs === "skipped" ||
              rs === "running" ||
              rs === "waiting_for_approval"
                ? rs
                : "completed";
          }
        }
        return next;
      });
    }
  }, [stream.lastEvent, stream.events, refreshProjects]);

  useEffect(() => {
    if (!lastMcpCall) return;
    const timer = setTimeout(() => {
      setLastMcpCall(null);
    }, 5000);
    return () => clearTimeout(timer);
  }, [lastMcpCall]);

  useEffect(() => {
    if (!selectedId) return;
    let active = true;
    api
      .projectStatus(selectedId)
      .then((s) => {
        if (active) setPaused(s.paused);
      })
      .catch(() => {
        /* ignore */
      });
    return () => {
      active = false;
    };
  }, [selectedId]);

  useEffect(() => {
    if (!drawerOpen) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setDrawerOpen(false);
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [drawerOpen]);

  const selectedProject = projects.find((p) => p.id === selectedId) ?? null;
  const selected = selectedProject;

  async function togglePause() {
    if (!selectedProject) return;
    try {
      if (paused) {
        await api.resume(selectedProject.id);
        setPaused(false);
      } else {
        await api.pause(selectedProject.id);
        setPaused(true);
      }
    } catch {
      /* ignore */
    }
  }

  async function toggleMode() {
    if (!selectedProject) return;
    const nextMode: ProjectMode =
      selectedProject.mode === "autonomous_bug_fixer"
        ? "task_queue"
        : "autonomous_bug_fixer";
    try {
      const updated = await api.setMode(selectedProject.id, nextMode);
      setProjects((prev) => prev.map((p) => (p.id === updated.id ? updated : p)));
    } catch {
      /* ignore */
    }
  }

  function renderView() {
    if (!selectedProject) return null;
    switch (mainView) {
      case "workspace":
        return (
          <WorkspaceView
            project={selectedProject}
            lastEvent={stream.lastEvent}
            liveNodeStatuses={liveNodeStatuses}
          />
        );
      case "studio":
        return (
          <AgentStudioView
            projectId={selectedProject.id}
            project={selectedProject}
            lastEvent={stream.lastEvent}
            liveNodeStatuses={liveNodeStatuses}
            nodeStatuses={liveNodeStatuses}
          />
        );
      case "history":
        return <HistoryView project={selectedProject} lastEvent={stream.lastEvent} />;
      case "settings":
        return (
          <SettingsView projectId={selectedProject.id} onProjectDeleted={reload} />
        );
    }
  }

  return (
    <div className="flex h-dvh flex-col overflow-hidden bg-zinc-950 text-zinc-100 md:flex-row">
      {/* Mobile top bar */}
      <div className="flex items-center justify-between border-b border-zinc-800/80 bg-zinc-900/90 px-4 py-2.5 md:hidden">
        <button
          type="button"
          onClick={() => setDrawerOpen(true)}
          aria-label="Open project menu"
          className="flex h-8 w-8 items-center justify-center rounded-lg text-zinc-300 hover:bg-zinc-800"
        >
          <svg viewBox="0 0 20 20" fill="currentColor" className="h-5 w-5" aria-hidden="true">
            <path
              fillRule="evenodd"
              d="M2 4.75A.75.75 0 0 1 2.75 4h14.5a.75.75 0 0 1 0 1.5H2.75A.75.75 0 0 1 2 4.75Zm0 5A.75.75 0 0 1 2.75 9h14.5a.75.75 0 0 1 0 1.5H2.75A.75.75 0 0 1 2 9.75Zm0 5A.75.75 0 0 1 2.75 14h14.5a.75.75 0 0 1 0 1.5H2.75A.75.75 0 0 1 2 14.75Z"
              clipRule="evenodd"
            />
          </svg>
        </button>
        <div className="truncate text-sm font-semibold tracking-tight">Conclave</div>
        <div className="w-8" />
      </div>

      {drawerOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/60 md:hidden"
          onClick={() => setDrawerOpen(false)}
          aria-hidden="true"
        />
      )}

      {/* Minimalist Left Sidebar */}
      <aside
        className={`fixed inset-y-0 left-0 z-50 flex w-56 flex-col border-r border-zinc-800/80 bg-zinc-950 transition-transform duration-200 md:static md:z-auto md:translate-x-0 ${
          drawerOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="flex items-center justify-between border-b border-zinc-800/80 px-4 py-3.5">
          <div className="flex items-center gap-2.5">
            <span className="flex h-6 w-6 items-center justify-center rounded-md bg-indigo-600/20 text-xs font-bold text-indigo-400 ring-1 ring-indigo-500/40">
              ◈
            </span>
            <div>
              <div className="text-sm font-semibold tracking-tight text-zinc-100">Conclave</div>
              <div className="text-[10px] tracking-wide text-zinc-500 uppercase">Agent Studio</div>
            </div>
          </div>
          <button
            type="button"
            onClick={() => setDrawerOpen(false)}
            aria-label="Close menu"
            className="flex h-7 w-7 items-center justify-center rounded-lg text-zinc-400 hover:bg-zinc-800 md:hidden"
          >
            ✕
          </button>
        </div>

        <div className="flex-1 space-y-1 overflow-y-auto p-2">
          <div className="px-2 py-1 text-[10px] font-semibold tracking-wider text-zinc-500 uppercase">
            Repositories
          </div>
          {loading && (
            <div className="space-y-1.5 p-1">
              {[0, 1].map((i) => (
                <div key={i} className="h-10 animate-pulse rounded-lg bg-zinc-900" />
              ))}
            </div>
          )}

          {!loading && error && (
            <div className="m-1 rounded-lg border border-rose-900/60 bg-rose-950/30 p-2.5 text-xs text-rose-300">
              <div className="mb-2">Failed to load repositories.</div>
              <Button variant="ghost" size="sm" onClick={reload}>
                Retry
              </Button>
            </div>
          )}

          {!loading &&
            !error &&
            projects.map((p) => {
              const active = selectedId === p.id;
              return (
                <button
                  key={p.id}
                  type="button"
                  onClick={() => {
                    setSelectedId(p.id);
                    setDrawerOpen(false);
                  }}
                  className={`group flex w-full flex-col rounded-lg px-2.5 py-2 text-left transition-colors ${
                    active
                      ? "bg-zinc-800/90 text-white ring-1 ring-zinc-700"
                      : "text-zinc-400 hover:bg-zinc-900 hover:text-zinc-200"
                  }`}
                >
                  <div className="flex items-center justify-between gap-1.5">
                    <span className="truncate text-xs font-medium">{p.name}</span>
                    {p.mode === "autonomous_bug_fixer" && (
                      <span className="rounded bg-amber-950/80 px-1.5 py-0.5 text-[9px] font-medium text-amber-300">
                        Sweep
                      </span>
                    )}
                  </div>
                  <div className="truncate text-[11px] text-zinc-500">{p.path}</div>
                </button>
              );
            })}

          {!loading && !error && projects.length === 0 && (
            <div className="px-2.5 py-4 text-xs text-zinc-500">No repositories attached.</div>
          )}
        </div>

        <div className="space-y-1.5 border-t border-zinc-800/80 p-2.5">
          {!selected && (
            <Button
              variant={globalStudioOpen ? "primary" : "ghost"}
              className="w-full"
              onClick={() => setGlobalStudioOpen((o) => !o)}
            >
              {globalStudioOpen ? "Close Global Studio" : "◈ Global Agent Store"}
            </Button>
          )}
          <Button
            variant="default"
            className="w-full"
            onClick={() => setShowAttach(true)}
          >
            + Attach Repository
          </Button>
        </div>
      </aside>

      {/* Main Content Area */}
      <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {loading && !selected ? (
          <div className="flex flex-1 items-center justify-center gap-2.5 text-xs text-zinc-500">
            <Spinner size={16} />
            Loading workspace…
          </div>
        ) : selected ? (
          <>
            {/* Unified Minimalist Top Bar: Project Context + 3 View Switcher + Worker Controls */}
            <header className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-zinc-800/80 bg-zinc-950/90 px-4 py-2.5">
              <div className="flex min-w-0 items-center gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm font-semibold text-zinc-100">
                      {selected.name}
                    </span>
                    <span className="rounded bg-zinc-900 px-1.5 py-0.5 font-mono text-[11px] text-zinc-400 ring-1 ring-zinc-800">
                      {selected.config?.execution?.target_branch ?? selected.default_branch}
                    </span>
                  </div>
                  <div className="truncate text-[11px] text-zinc-500">{selected.path}</div>
                </div>
              </div>

              {/* Center 4-View Segmented Navigation */}
              <nav className="flex items-center rounded-lg border border-zinc-800/90 bg-zinc-900/70 p-0.5">
                {MAIN_VIEWS.map((v) => {
                  const active = mainView === v.id;
                  return (
                    <button
                      key={v.id}
                      type="button"
                      onClick={() => setMainView(v.id)}
                      className={`rounded-md px-3 py-1 text-xs font-medium transition-colors ${
                        active
                          ? "bg-indigo-600 text-white shadow-xs"
                          : "text-zinc-400 hover:text-zinc-200"
                      }`}
                    >
                      {v.label}
                    </button>
                  );
                })}
              </nav>

              {/* Right Worker & Mode Status Pills */}
              <div className="flex items-center gap-2">
                {lastMcpCall && (
                  <span
                    role="status"
                    className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/50 bg-indigo-950/80 px-2.5 py-1 font-mono text-xs font-medium text-emerald-300 shadow-xs"
                  >
                    <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-ping" />
                    MCP: {lastMcpCall.tool}
                  </span>
                )}

                <button
                  type="button"
                  onClick={toggleMode}
                  title="Switch execution mode between Task Queue and Autonomous Bug Sweep"
                  className={`inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs font-medium transition ${
                    selected.mode === "autonomous_bug_fixer"
                      ? "border-amber-800/70 bg-amber-950/60 text-amber-300 hover:bg-amber-950"
                      : "border-zinc-800 bg-zinc-900 text-zinc-300 hover:border-zinc-700"
                  }`}
                >
                  <span
                    className={`h-1.5 w-1.5 rounded-full ${
                      selected.mode === "autonomous_bug_fixer" ? "bg-amber-400" : "bg-indigo-400"
                    }`}
                  />
                  {selected.mode === "autonomous_bug_fixer" ? "Mode: Bug Sweep" : "Mode: Task Queue"}
                </button>

                <button
                  type="button"
                  onClick={togglePause}
                  className={`inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs font-medium transition ${
                    paused
                      ? "border-amber-800/70 bg-amber-950/50 text-amber-300 hover:bg-amber-900/50"
                      : "border-emerald-800/70 bg-emerald-950/40 text-emerald-300 hover:bg-emerald-900/40"
                  }`}
                >
                  <span
                    className={`h-2 w-2 rounded-full ${
                      paused ? "bg-amber-400" : "bg-emerald-400 animate-pulse"
                    }`}
                  />
                  {paused ? "Paused · Resume" : "Active · Pause"}
                </button>
              </div>
            </header>

            <section className="min-h-0 flex-1 overflow-hidden p-3 sm:p-4">
              {renderView()}
            </section>
          </>
        ) : globalStudioOpen ? (
          <>
            <header className="flex shrink-0 items-center justify-between gap-3 border-b border-zinc-800/80 bg-zinc-950/90 px-4 py-2.5">
              <div>
                <div className="text-sm font-semibold text-zinc-100">
                  Global Agent & Workflow Store
                </div>
                <div className="text-[11px] text-zinc-500">
                  Design reusable workflows, agent personas, and config presets ready for any repository or MCP client.
                </div>
              </div>
              <div className="flex items-center gap-2">
                {lastMcpCall && (
                  <span
                    role="status"
                    className="inline-flex items-center gap-1.5 rounded-full border border-emerald-500/50 bg-indigo-950/80 px-2.5 py-1 font-mono text-xs font-medium text-emerald-300 shadow-xs"
                  >
                    <span className="h-1.5 w-1.5 rounded-full bg-emerald-400 animate-ping" />
                    MCP: {lastMcpCall.tool}
                  </span>
                )}
                <Button variant="primary" onClick={() => setShowAttach(true)}>
                  + Attach Repository
                </Button>
              </div>
            </header>
            <section className="min-h-0 flex-1 overflow-hidden p-3 sm:p-4">
              <AgentStudioView
                projectId={null}
                project={null}
                lastEvent={stream.lastEvent}
                liveNodeStatuses={liveNodeStatuses}
                nodeStatuses={liveNodeStatuses}
              />
            </section>
          </>
        ) : (
          <div className="flex flex-1 flex-col items-center justify-center px-6 text-center">
            <div className="mb-3 flex h-12 w-12 items-center justify-center rounded-2xl border border-zinc-800 bg-zinc-900 text-xl text-indigo-400">
              ◈
            </div>
            <div className="text-sm font-semibold text-zinc-200">No repository attached</div>
            <div className="mt-1 max-w-xs text-xs text-zinc-500">
              Attach a local git repository via the UI or MCP (`conclave_attach_project`), or open the Global Store to pre-configure agents and workflows.
            </div>
            <div className="mt-4 flex items-center gap-2">
              <Button variant="default" onClick={() => setGlobalStudioOpen(true)}>
                Open Global Agent & Workflow Store
              </Button>
              <Button variant="primary" onClick={() => setShowAttach(true)}>
                + Attach Repository
              </Button>
            </div>
          </div>
        )}
      </main>

      {showAttach && (
        <AttachModal
          onClose={() => setShowAttach(false)}
          onDone={async (id) => {
            setShowAttach(false);
            await reload();
            setSelectedId(id);
          }}
        />
      )}
    </div>
  );
}

function AttachModal({ onClose, onDone }: { onClose: () => void; onDone: (id: string) => void }) {
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [branch, setBranch] = useState("main");
  const [workflowTemplate, setWorkflowTemplate] = useState("standard");
  const [configPreset, setConfigPreset] = useState("default-balanced");
  const [storeItems, setStoreItems] = useState<StoreItem[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const nameRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    api
      .listStore()
      .then(setStoreItems)
      .catch(() => {
        /* ignore */
      });
  }, []);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", onKey);
    nameRef.current?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const workflowChoices = storeItems.filter((i) => i.kind === "workflow");
  const presetChoices = storeItems.filter((i) => i.kind === "config_preset");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim() || !path.trim()) return;
    setBusy(true);
    setError("");
    try {
      const project = await api.createProject(
        {
          name: name.trim(),
          path: path.trim(),
          default_branch: branch.trim() || "main",
          workflow_template: workflowTemplate || undefined,
          config_preset: configPreset || undefined,
        },
        `proj-${name.trim()}-${Date.now()}`,
      );
      onDone(project.id);
    } catch (err) {
      setError(String(err));
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-xs"
      onClick={onClose}
    >
      <form
        role="dialog"
        aria-modal="true"
        aria-labelledby="attach-modal-title"
        onSubmit={submit}
        className="w-full max-w-sm rounded-xl border border-zinc-800 bg-zinc-900 p-5 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 id="attach-modal-title" className="mb-3 text-sm font-semibold text-zinc-100">
          Attach Git Repository
        </h2>
        <div className="space-y-3">
          <label className="block">
            <span className="mb-1 block text-xs text-zinc-400">Name</span>
            <input
              ref={nameRef}
              className={input}
              placeholder="my-service"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-zinc-400">Local Repository Path</span>
            <input
              className={input}
              placeholder="/path/to/repo"
              value={path}
              onChange={(e) => setPath(e.target.value)}
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-zinc-400">Target Branch</span>
            <input
              className={input}
              placeholder="main"
              value={branch}
              onChange={(e) => setBranch(e.target.value)}
            />
          </label>
          <div className="grid grid-cols-2 gap-2.5">
            <label className="block">
              <span className="mb-1 block text-xs text-zinc-400">Workflow Blueprint</span>
              <select
                className={input}
                value={workflowTemplate}
                onChange={(e) => setWorkflowTemplate(e.target.value)}
              >
                {workflowChoices.length > 0 ? (
                  workflowChoices.map((w) => (
                    <option key={w.id} value={w.name}>
                      {w.name}
                    </option>
                  ))
                ) : (
                  <option value="standard">standard</option>
                )}
              </select>
            </label>
            <label className="block">
              <span className="mb-1 block text-xs text-zinc-400">Config Preset</span>
              <select
                className={input}
                value={configPreset}
                onChange={(e) => setConfigPreset(e.target.value)}
              >
                {presetChoices.length > 0 ? (
                  presetChoices.map((c) => (
                    <option key={c.id} value={c.name}>
                      {c.name}
                    </option>
                  ))
                ) : (
                  <option value="default-balanced">default-balanced</option>
                )}
              </select>
            </label>
          </div>
        </div>
        {error && (
          <div className="mt-3 rounded-lg border border-rose-800/60 bg-rose-950/40 p-2 text-xs text-rose-300">
            {error}
          </div>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" disabled={busy || !name.trim() || !path.trim()}>
            {busy ? "Attaching…" : "Attach"}
          </Button>
        </div>
      </form>
    </div>
  );
}
