import { useCallback, useEffect, useState } from "react";

import { api } from "./api";
import { Button, ConfigPanel, LivePanel, ProfilesPanel, QuarantinePanel, TasksPanel } from "./panels";
import type { Project } from "./types";

const TABS = ["Tasks", "Live", "Config", "Profiles", "Quarantine"] as const;
type Tab = (typeof TABS)[number];

const modalInput =
  "w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm outline-none focus:border-emerald-500";

export default function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("Tasks");
  const [showAttach, setShowAttach] = useState(false);

  const reload = useCallback(async () => {
    const ps = await api.listProjects();
    setProjects(ps);
    setSelectedId((cur) => cur ?? ps[0]?.id ?? null);
  }, []);

  useEffect(() => {
    reload().catch(() => {});
  }, [reload]);

  const selected = projects.find((p) => p.id === selectedId) ?? null;

  return (
    <div className="flex h-screen bg-zinc-950 text-zinc-100">
      <aside className="flex w-64 flex-col border-r border-zinc-800 bg-zinc-900">
        <div className="border-b border-zinc-800 p-4">
          <div className="text-lg font-bold tracking-tight">Conclave</div>
          <div className="text-xs text-zinc-500">autonomous coding team</div>
        </div>
        <div className="flex-1 overflow-y-auto p-2">
          {projects.map((p) => (
            <button
              key={p.id}
              onClick={() => setSelectedId(p.id)}
              className={`mb-1 w-full rounded px-3 py-2 text-left text-sm ${
                selectedId === p.id ? "bg-emerald-700 text-white" : "text-zinc-200 hover:bg-zinc-800"
              }`}
            >
              <div className="font-medium">{p.name}</div>
              <div className="truncate text-xs opacity-70">{p.path}</div>
            </button>
          ))}
          {projects.length === 0 && (
            <div className="px-3 py-2 text-sm text-zinc-500">No projects attached.</div>
          )}
        </div>
        <div className="border-t border-zinc-800 p-3">
          <Button variant="primary" onClick={() => setShowAttach(true)}>
            + Attach project
          </Button>
        </div>
      </aside>

      <main className="flex flex-1 flex-col overflow-hidden">
        {selected ? (
          <>
            <header className="flex items-center justify-between border-b border-zinc-800 px-6 py-3">
              <div>
                <div className="text-base font-semibold">{selected.name}</div>
                <div className="text-xs text-zinc-500">
                  {selected.path} · target: {selected.default_branch}
                </div>
              </div>
              <div className="flex gap-2">
                <Button onClick={() => api.resume(selected.id)}>Resume</Button>
                <Button variant="ghost" onClick={() => api.pause(selected.id)}>
                  Pause
                </Button>
                <Button variant="ghost" onClick={() => api.reonboard(selected.id)}>
                  Re-analyze
                </Button>
              </div>
            </header>
            <nav className="flex gap-1 border-b border-zinc-800 px-4">
              {TABS.map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  className={`px-4 py-2 text-sm ${
                    tab === t
                      ? "border-b-2 border-emerald-500 text-white"
                      : "text-zinc-400 hover:text-zinc-200"
                  }`}
                >
                  {t}
                </button>
              ))}
            </nav>
            <section className="flex-1 overflow-y-auto p-6">
              {tab === "Tasks" && <TasksPanel projectId={selected.id} />}
              {tab === "Live" && <LivePanel projectId={selected.id} />}
              {tab === "Config" && <ConfigPanel projectId={selected.id} />}
              {tab === "Profiles" && <ProfilesPanel />}
              {tab === "Quarantine" && <QuarantinePanel projectId={selected.id} />}
            </section>
          </>
        ) : (
          <div className="flex flex-1 items-center justify-center text-zinc-500">
            Attach a project to begin.
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
    <div className="fixed inset-0 flex items-center justify-center bg-black/60" onClick={onClose}>
      <div
        className="w-96 rounded-lg border border-zinc-700 bg-zinc-900 p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="mb-3 text-lg font-semibold">Attach project</h2>
        <div className="space-y-2">
          <input className={modalInput} placeholder="name" value={name} onChange={(e) => setName(e.target.value)} />
          <input
            className={modalInput}
            placeholder="absolute path to a git repo"
            value={path}
            onChange={(e) => setPath(e.target.value)}
          />
          <input
            className={modalInput}
            placeholder="target branch"
            value={branch}
            onChange={(e) => setBranch(e.target.value)}
          />
        </div>
        {error && <div className="mt-2 rounded bg-red-950 p-2 text-sm text-red-300">{error}</div>}
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
