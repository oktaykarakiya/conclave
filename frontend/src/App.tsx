import { useCallback, useEffect, useState } from "react";

import { api } from "./api";
import {
  AgentCeptionPanel,
  Button,
  ConfigPanel,
  KnowledgePanel,
  LivePanel,
  ProfilesPanel,
  QuarantinePanel,
  Spinner,
  TasksPanel,
  input,
} from "./panels";
import type { Project } from "./types";

// Dedicated page per menu item, in order. Agent-ception is first + the default.
const TABS = [
  "Agent-ception",
  "Tasks",
  "Live",
  "Config",
  "Profiles",
  "Quarantine",
  "Knowledge",
] as const;
type Tab = (typeof TABS)[number];

export default function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("Agent-ception");
  const [showAttach, setShowAttach] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const ps = await api.listProjects();
      setProjects(ps);
      setSelectedId((cur) => cur ?? ps[0]?.id ?? null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  const selected = projects.find((p) => p.id === selectedId) ?? null;

  // Each page fills exactly the content viewport and scrolls INTERNALLY — the page
  // itself never exceeds the screen (the section below is overflow-hidden).
  function renderPage() {
    if (!selected) return null;
    switch (tab) {
      case "Agent-ception":
        return <AgentCeptionPanel projectId={selected.id} />;
      case "Tasks":
        return <TasksPanel projectId={selected.id} />;
      case "Live":
        return <LivePanel projectId={selected.id} />;
      case "Config":
        return <ConfigPanel projectId={selected.id} />;
      case "Profiles":
        return <ProfilesPanel />;
      case "Quarantine":
        return <QuarantinePanel projectId={selected.id} />;
      case "Knowledge":
        return <KnowledgePanel projectId={selected.id} />;
    }
  }

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-zinc-950 text-zinc-100 md:flex-row">
      {/* Mobile top bar with hamburger */}
      <div className="flex items-center gap-3 border-b border-zinc-800 bg-zinc-900 px-4 py-3 md:hidden">
        <button
          type="button"
          onClick={() => setDrawerOpen(true)}
          aria-label="Open project menu"
          className="flex h-9 w-9 items-center justify-center rounded-lg text-zinc-300 transition-colors hover:bg-zinc-800 focus-visible:ring-2 focus-visible:ring-indigo-400 focus-visible:outline-none"
        >
          <svg viewBox="0 0 20 20" fill="currentColor" className="h-5 w-5" aria-hidden="true">
            <path
              fillRule="evenodd"
              d="M2 4.75A.75.75 0 0 1 2.75 4h14.5a.75.75 0 0 1 0 1.5H2.75A.75.75 0 0 1 2 4.75Zm0 5A.75.75 0 0 1 2.75 9h14.5a.75.75 0 0 1 0 1.5H2.75A.75.75 0 0 1 2 9.75Zm0 5A.75.75 0 0 1 2.75 14h14.5a.75.75 0 0 1 0 1.5H2.75A.75.75 0 0 1 2 14.75Z"
              clipRule="evenodd"
            />
          </svg>
        </button>
        <div className="min-w-0 flex-1">
          <div className="truncate text-base font-bold tracking-tight">Conclave</div>
        </div>
      </div>

      {/* Mobile drawer overlay */}
      {drawerOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/60 md:hidden"
          onClick={() => setDrawerOpen(false)}
          aria-hidden="true"
        />
      )}

      {/* Sidebar — drawer on mobile, fixed rail on md+ */}
      <aside
        className={`fixed inset-y-0 left-0 z-50 flex w-64 flex-col border-r border-zinc-800 bg-zinc-900 transition-transform duration-200 md:static md:z-auto md:translate-x-0 ${
          drawerOpen ? "translate-x-0" : "-translate-x-full"
        }`}
      >
        <div className="flex items-center justify-between border-b border-zinc-800 p-4">
          <div>
            <div className="text-lg font-bold tracking-tight">Conclave</div>
            <div className="text-xs text-zinc-500">autonomous coding team</div>
          </div>
          <button
            type="button"
            onClick={() => setDrawerOpen(false)}
            aria-label="Close project menu"
            className="flex h-8 w-8 items-center justify-center rounded-lg text-zinc-400 transition-colors hover:bg-zinc-800 focus-visible:ring-2 focus-visible:ring-indigo-400 focus-visible:outline-none md:hidden"
          >
            <svg viewBox="0 0 20 20" fill="currentColor" className="h-5 w-5" aria-hidden="true">
              <path d="M6.28 5.22a.75.75 0 0 0-1.06 1.06L8.94 10l-3.72 3.72a.75.75 0 1 0 1.06 1.06L10 11.06l3.72 3.72a.75.75 0 1 0 1.06-1.06L11.06 10l3.72-3.72a.75.75 0 0 0-1.06-1.06L10 8.94 6.28 5.22Z" />
            </svg>
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-2">
          {loading && (
            <div className="space-y-2 p-1">
              {[0, 1, 2].map((i) => (
                <div key={i} className="h-11 animate-pulse rounded-lg bg-zinc-800" />
              ))}
            </div>
          )}

          {!loading && error && (
            <div className="m-1 rounded-lg bg-rose-950/40 p-3 text-sm text-rose-300">
              <div className="mb-2">Failed to load projects.</div>
              <Button variant="ghost" onClick={reload}>
                Retry
              </Button>
            </div>
          )}

          {!loading &&
            !error &&
            projects.map((p) => (
              <button
                key={p.id}
                onClick={() => {
                  setSelectedId(p.id);
                  setDrawerOpen(false);
                }}
                className={`mb-1 min-h-[44px] w-full rounded-lg px-3 py-2.5 text-left text-sm transition-colors ${
                  selectedId === p.id ? "bg-indigo-700 text-white" : "text-zinc-200 hover:bg-zinc-800"
                }`}
              >
                <div className="font-medium">{p.name}</div>
                <div className="truncate text-xs opacity-70">{p.path}</div>
              </button>
            ))}

          {!loading && !error && projects.length === 0 && (
            <div className="px-3 py-2 text-sm text-zinc-500">No projects attached.</div>
          )}
        </div>

        <div className="border-t border-zinc-800 p-3">
          <Button variant="primary" onClick={() => setShowAttach(true)}>
            + Attach project
          </Button>
        </div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col overflow-hidden">
        {loading && !selected ? (
          <div className="flex flex-1 items-center justify-center gap-3 text-zinc-500">
            <Spinner size={18} />
            Loading projects…
          </div>
        ) : selected ? (
          <>
            <header className="flex flex-col gap-3 border-b border-zinc-800 px-4 py-3 sm:flex-row sm:items-center sm:justify-between sm:px-6">
              <div className="min-w-0">
                <div className="truncate text-base font-semibold">{selected.name}</div>
                <div className="truncate text-xs text-zinc-500">
                  {selected.path} · target:{" "}
                  {selected.config?.execution?.target_branch ?? selected.default_branch}
                </div>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button onClick={() => api.resume(selected.id)}>Resume</Button>
                <Button variant="ghost" onClick={() => api.pause(selected.id)}>
                  Pause
                </Button>
                <Button
                  variant="ghost"
                  title="Re-runs AI repo analysis (takes a few minutes)"
                  onClick={() => api.reonboard(selected.id)}
                >
                  Re-analyze
                </Button>
              </div>
            </header>

            {/* Tab nav — one dedicated page per item */}
            <nav className="flex shrink-0 gap-1 overflow-x-auto border-b border-zinc-800 px-2 sm:px-4">
              {TABS.map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  className={`shrink-0 border-b-2 px-3 py-2.5 text-sm font-medium transition-colors focus-visible:outline-none ${
                    tab === t
                      ? "border-indigo-500 text-white"
                      : "border-transparent text-zinc-400 hover:text-zinc-200"
                  }`}
                >
                  {t}
                </button>
              ))}
            </nav>

            {/* Content viewport — fixed height; the active page scrolls internally
                so no page is ever taller than the screen. */}
            <section className="min-h-0 flex-1 overflow-hidden p-4 sm:p-6">{renderPage()}</section>
          </>
        ) : (
          <div className="flex flex-1 flex-col items-center justify-center px-6 text-center">
            <div className="mb-3 text-4xl text-zinc-700" aria-hidden="true">
              ◇
            </div>
            <div className="text-base font-semibold text-zinc-300">No project selected</div>
            <div className="mt-1 max-w-sm text-sm text-zinc-500">
              Attach a git repo to start an autonomous coding session.
            </div>
            <div className="mt-4">
              <Button variant="primary" onClick={() => setShowAttach(true)}>
                + Attach project
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
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit() {
    setBusy(true);
    setError("");
    try {
      const project = await api.createProject({ name, path, default_branch: branch });
      onDone(project.id);
    } catch (e) {
      setError(String(e));
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={onClose}
    >
      <div
        className="w-full max-w-sm rounded-xl border border-zinc-800 bg-zinc-900 p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="mb-3 text-lg font-semibold">Attach project</h2>
        <div className="space-y-3">
          <label className="block">
            <span className="mb-1 block text-xs text-zinc-400">Name</span>
            <input
              className={input}
              placeholder="my-project"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-zinc-400">Repo path</span>
            <input
              className={input}
              placeholder="absolute path to a git repo"
              value={path}
              onChange={(e) => setPath(e.target.value)}
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs text-zinc-400">Target branch</span>
            <input
              className={input}
              placeholder="main"
              value={branch}
              onChange={(e) => setBranch(e.target.value)}
            />
          </label>
        </div>
        {error && (
          <div className="mt-2 rounded-lg bg-rose-950/40 p-2 text-sm text-rose-300">{error}</div>
        )}
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" onClick={submit} disabled={busy || !name || !path}>
            {busy ? "Attaching…" : "Attach"}
          </Button>
        </div>
      </div>
    </div>
  );
}
