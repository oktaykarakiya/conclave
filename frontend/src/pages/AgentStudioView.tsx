import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "../api";
import { WorkflowCanvas, autoLayoutWorkflow } from "../components/WorkflowCanvas";
import type {
  AgentPersona,
  EdgeCondition,
  NodeKind,
  OutputContract,
  StoreItem,
  WorkflowEdge,
  WorkflowGraph,
  WorkflowNode,
} from "../types";
import { Badge, Button, NODE_KIND_COLORS, Spinner, input } from "../ui";

const BUILTIN_TEMPLATES: { id: string; label: string; hint: string }[] = [
  { id: "standard", label: "Standard", hint: "Plan → Develop → Review → Gate → Merge" },
  { id: "fast_track", label: "Fast Track", hint: "Develop → Reviewer → Gate → Merge" },
  { id: "tdd_loop", label: "TDD Loop", hint: "Plan → Test-First → Develop → Gate → Merge" },
  { id: "deep_audit", label: "Deep Audit", hint: "Full specialist audit + retry loops" },
];

const NODE_KINDS: NodeKind[] = [
  "planner",
  "developer",
  "reviewer",
  "gate",
  "merge",
  "custom",
];

const OUTPUT_CONTRACTS: OutputContract[] = [
  "text",
  "json",
  "verdict",
  "plan",
  "diff",
];

const EDGE_CONDITIONS: EdgeCondition[] = [
  "always",
  "on_pass",
  "on_fail",
  "on_diff_trigger",
];

const COMMON_TRIGGERS: string[] = [
  "new_files",
  "files_gt_5",
  "complexity_medium_plus",
  "api_change",
  "db_change",
  "auth_change",
  "new_dependency",
  "dockerfile",
  "migration",
];

const EFFORT_TIERS = ["low", "medium", "high", "xhigh", "max"] as const;

const AGENT_ROLES = [
  "planning",
  "developer",
  "mandatory",
  "conditional",
  "analyst",
  "hunter",
  "repro",
  "postmortem",
] as const;

/** Check whether adding a forward edge `source -> target` would create a cycle in `graph`. */
function wouldCreateForwardCycle(
  graph: WorkflowGraph,
  source: string,
  target: string,
): boolean {
  if (source === target) return true;
  const adj = new Map<string, string[]>();
  for (const n of graph.nodes) adj.set(n.id, []);
  for (const e of graph.edges) {
    if (e.condition === "on_fail") continue;
    adj.get(e.source)?.push(e.target);
  }
  // Check if `source` is reachable from `target` via existing forward edges
  const visited = new Set<string>();
  const stack = [target];
  while (stack.length > 0) {
    const cur = stack.pop()!;
    if (cur === source) return true;
    if (visited.has(cur)) continue;
    visited.add(cur);
    for (const nxt of adj.get(cur) ?? []) {
      stack.push(nxt);
    }
  }
  return false;
}

function inferKindAndContract(role: string): {
  kind: NodeKind;
  contract: OutputContract;
} {
  switch (role) {
    case "planning":
      return { kind: "planner", contract: "plan" };
    case "developer":
      return { kind: "developer", contract: "diff" };
    case "mandatory":
    case "conditional":
      return { kind: "reviewer", contract: "verdict" };
    default:
      return { kind: "custom", contract: "text" };
  }
}

export function AgentStudioView({ projectId }: { projectId?: string | null }) {
  const [graph, setGraph] = useState<WorkflowGraph | null>(null);
  const [isCustom, setIsCustom] = useState<boolean>(false);
  const [templates, setTemplates] = useState<Record<string, WorkflowGraph>>({});
  const [agents, setAgents] = useState<AgentPersona[]>([]);
  const [storeItems, setStoreItems] = useState<StoreItem[]>([]);
  const [config, setConfig] = useState<Record<string, unknown>>({});

  const [loading, setLoading] = useState<boolean>(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [connectingFromId, setConnectingFromId] = useState<string | null>(null);

  const [drawerOpen, setDrawerOpen] = useState<boolean>(true);
  const [inspectorTab, setInspectorTab] = useState<"inspector" | "personas" | "store">(
    "inspector",
  );

  // Status banners for workflow save, persona save, and agent override save
  const [dirty, setDirty] = useState<boolean>(false);
  const [saveStatus, setSaveStatus] = useState<
    "idle" | "saving" | "saved" | "error"
  >("idle");
  const [statusMessage, setStatusMessage] = useState<string | null>(null);

  // Custom trigger input in Node Inspector
  const [customTriggerInput, setCustomTriggerInput] = useState<string>("");

  // Inline create persona inside Node Inspector
  const [showInlineNewPersona, setShowInlineNewPersona] =
    useState<boolean>(false);
  const [inlinePersonaName, setInlinePersonaName] = useState<string>("");
  const [inlinePersonaRole, setInlinePersonaRole] =
    useState<string>("conditional");
  const [inlinePersonaMd, setInlinePersonaMd] = useState<string>("");

  // Selected Node's Agent Persona markdown editor state
  const [nodePersonaRole, setNodePersonaRole] = useState<string>("conditional");
  const [nodePersonaMd, setNodePersonaMd] = useState<string>("");
  const [personaSaveState, setPersonaSaveState] = useState<
    "idle" | "saving" | "saved" | "error"
  >("idle");

  // Selected Node's Per-Agent Model & Effort Overrides state
  const [overrideModel, setOverrideModel] = useState<string>("");
  const [overrideEffort, setOverrideEffort] = useState<string>("");
  const [overrideTimeout, setOverrideTimeout] = useState<string>("");
  const [overrideRetries, setOverrideRetries] = useState<string>("");
  const [overrideSaveState, setOverrideSaveState] = useState<
    "idle" | "saving" | "saved" | "error"
  >("idle");

  // Personas Library sub-tab state
  const [editingPersonaName, setEditingPersonaName] = useState<string | null>(
    null,
  );
  const [showNewPersonaForm, setShowNewPersonaForm] = useState<boolean>(false);
  const [libPersonaName, setLibPersonaName] = useState<string>("");
  const [libPersonaRole, setLibPersonaRole] = useState<string>("conditional");
  const [libPersonaMd, setLibPersonaMd] = useState<string>("");
  const [libBusy, setLibBusy] = useState<boolean>(false);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      if (projectId) {
        const [wfEnv, agentList, cfg, storeList] = await Promise.all([
          api.getWorkflow(projectId),
          api.listAgents(projectId),
          api.getConfig(projectId),
          api.listStore().catch(() => [] as StoreItem[]),
        ]);
        setGraph(wfEnv.workflow);
        setIsCustom(wfEnv.is_custom);
        setTemplates(wfEnv.templates ?? {});
        setAgents(agentList);
        setConfig(cfg);
        setStoreItems(storeList);
        setDirty(false);
      } else {
        const [storeList, agentList] = await Promise.all([
          api.listStore(),
          api.listAgents(),
        ]);
        setStoreItems(storeList);
        setAgents(agentList);
        const wfMap: Record<string, WorkflowGraph> = {};
        for (const item of storeList) {
          if (item.kind === "workflow") {
            wfMap[item.name] = item.payload as unknown as WorkflowGraph;
          }
        }
        setTemplates(wfMap);
        const initialWf =
          wfMap.standard ?? Object.values(wfMap)[0] ?? null;
        setGraph(initialWf);
        setIsCustom(false);
        setDirty(false);
      }
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  const selectedNode = useMemo<WorkflowNode | null>(() => {
    if (!graph || !selectedNodeId) return null;
    return graph.nodes.find((n) => n.id === selectedNodeId) ?? null;
  }, [graph, selectedNodeId]);

  const selectedEdge = useMemo<WorkflowEdge | null>(() => {
    if (!graph || !selectedEdgeId) return null;
    return graph.edges.find((e) => e.id === selectedEdgeId) ?? null;
  }, [graph, selectedEdgeId]);

  // Synchronize node persona & per-agent override inputs when the selected node's agent changes
  const activeAgentName = selectedNode?.agent ?? null;

  useEffect(() => {
    setPersonaSaveState("idle");
    setOverrideSaveState("idle");
    if (!activeAgentName) {
      setNodePersonaRole("conditional");
      setNodePersonaMd("");
      setOverrideModel("");
      setOverrideEffort("");
      setOverrideTimeout("");
      setOverrideRetries("");
      return;
    }

    const matchedPersona = agents.find((a) => a.name === activeAgentName);
    setNodePersonaRole(matchedPersona?.role ?? "conditional");
    setNodePersonaMd(matchedPersona?.persona_md ?? "");

    const overridesMap =
      config.agent_overrides &&
      typeof config.agent_overrides === "object" &&
      !Array.isArray(config.agent_overrides)
        ? (config.agent_overrides as Record<string, Record<string, unknown>>)
        : {};
    const agentOv = overridesMap[activeAgentName] ?? {};

    setOverrideModel(
      typeof agentOv.model === "string" ? agentOv.model : "",
    );
    setOverrideEffort(
      typeof agentOv.effort === "string" ? agentOv.effort : "",
    );
    setOverrideTimeout(
      typeof agentOv.timeout_minutes === "number"
        ? String(agentOv.timeout_minutes)
        : "",
    );
    setOverrideRetries(
      typeof agentOv.max_retries === "number"
        ? String(agentOv.max_retries)
        : "",
    );
  }, [activeAgentName, agents, config.agent_overrides]);

  function updateGraph(updater: (prev: WorkflowGraph) => WorkflowGraph) {
    setGraph((prev) => {
      if (!prev) return prev;
      return updater(prev);
    });
    setDirty(true);
    if (saveStatus !== "idle") setSaveStatus("idle");
    setStatusMessage(null);
  }

  function updateNode(nodeId: string, patch: Partial<WorkflowNode>) {
    updateGraph((prev) => ({
      ...prev,
      nodes: prev.nodes.map((n) => (n.id === nodeId ? { ...n, ...patch } : n)),
    }));
  }

  function updateEdge(edgeId: string, patch: Partial<WorkflowEdge>) {
    updateGraph((prev) => ({
      ...prev,
      edges: prev.edges.map((e) => (e.id === edgeId ? { ...e, ...patch } : e)),
    }));
  }

  const handleMoveNode = useCallback(
    (nodeId: string, x: number, y: number) => {
      setGraph((prev) => {
        if (!prev) return prev;
        return {
          ...prev,
          nodes: prev.nodes.map((n) =>
            n.id === nodeId ? { ...n, position_x: x, position_y: y } : n,
          ),
        };
      });
      setDirty(true);
    },
    [],
  );

  const handleSelectNode = useCallback((nodeId: string | null) => {
    setSelectedNodeId(nodeId);
    if (nodeId) {
      setSelectedEdgeId(null);
      setDrawerOpen(true);
      setInspectorTab("inspector");
    }
  }, []);

  const handleSelectEdge = useCallback((edgeId: string | null) => {
    setSelectedEdgeId(edgeId);
    if (edgeId) {
      setSelectedNodeId(null);
      setDrawerOpen(true);
      setInspectorTab("inspector");
    }
  }, []);

  const handleCompleteConnect = useCallback(
    (sourceId: string, targetId: string) => {
      if (!graph || sourceId === targetId) {
        setConnectingFromId(null);
        return;
      }

      const sourceNode = graph.nodes.find((n) => n.id === sourceId);
      const targetNode = graph.nodes.find((n) => n.id === targetId);
      if (!sourceNode || !targetNode) {
        setConnectingFromId(null);
        return;
      }

      const createsCycle = wouldCreateForwardCycle(graph, sourceId, targetId);
      let defaultCondition: EdgeCondition = "always";
      if (createsCycle) {
        defaultCondition = "on_fail";
      } else if (targetNode.triggers.length > 0) {
        defaultCondition = "on_diff_trigger";
      } else if (
        sourceNode.kind === "reviewer" ||
        sourceNode.kind === "gate"
      ) {
        defaultCondition = "on_pass";
      }

      const edgeId = `e_${sourceId}_${targetId}_${Date.now().toString(36).slice(-4)}`.slice(
        0,
        64,
      );
      const newEdge: WorkflowEdge = {
        id: edgeId,
        source: sourceId,
        target: targetId,
        condition: defaultCondition,
        max_iterations: 3,
        label: defaultCondition === "on_fail" ? "retry" : "",
      };

      setGraph((prev) =>
        prev ? { ...prev, edges: [...prev.edges, newEdge] } : prev,
      );
      setDirty(true);
      setConnectingFromId(null);
      setSelectedNodeId(null);
      setSelectedEdgeId(newEdge.id);
      setDrawerOpen(true);
      setInspectorTab("inspector");
    },
    [graph],
  );

  function handleAddNode(presetPersona?: AgentPersona) {
    if (!graph) return;
    const existingIds = new Set(graph.nodes.map((n) => n.id));
    const baseId = presetPersona
      ? presetPersona.name.replace(/[^a-zA-Z0-9_-]/g, "_")
      : "node";
    let candidateId = baseId;
    let idx = 1;
    while (existingIds.has(candidateId)) {
      candidateId = `${baseId}_${idx}`;
      idx += 1;
    }

    const inferred = presetPersona
      ? inferKindAndContract(presetPersona.role)
      : { kind: "reviewer" as NodeKind, contract: "verdict" as OutputContract };

    const maxX = graph.nodes.reduce(
      (acc, n) => Math.max(acc, n.position_x),
      120,
    );
    const newNode: WorkflowNode = {
      id: candidateId.slice(0, 64),
      label: presetPersona
        ? presetPersona.name
            .replace(/[-_]/g, " ")
            .replace(/\b\w/g, (c) => c.toUpperCase())
        : `Stage ${graph.nodes.length + 1}`,
      kind: inferred.kind,
      agent: presetPersona?.name ?? agents[0]?.name ?? "reviewer",
      output_contract: inferred.contract,
      prompt_template: null,
      triggers: [],
      optional: false,
      enabled: true,
      position_x: Math.min(maxX + 60, 780),
      position_y: 140 + ((graph.nodes.length * 48) % 240),
    };

    updateGraph((prev) => ({
      ...prev,
      nodes: [...prev.nodes, newNode],
    }));
    setSelectedNodeId(newNode.id);
    setSelectedEdgeId(null);
    setDrawerOpen(true);
    setInspectorTab("inspector");
  }

  function handleDeleteSelectedNode() {
    if (!selectedNodeId) return;
    const targetId = selectedNodeId;
    updateGraph((prev) => ({
      ...prev,
      nodes: prev.nodes.filter((n) => n.id !== targetId),
      edges: prev.edges.filter(
        (e) => e.source !== targetId && e.target !== targetId,
      ),
    }));
    setSelectedNodeId(null);
  }

  function handleDeleteSelectedEdge() {
    if (!selectedEdgeId) return;
    const targetEdgeId = selectedEdgeId;
    updateGraph((prev) => ({
      ...prev,
      edges: prev.edges.filter((e) => e.id !== targetEdgeId),
    }));
    setSelectedEdgeId(null);
  }

  function handleAutoLayout() {
    updateGraph((prev) => autoLayoutWorkflow(prev));
  }

  async function handleSelectTemplate(templateId: string) {
    setSaveStatus("saving");
    setStatusMessage(null);
    try {
      if (projectId) {
        const env = await api.putWorkflow(projectId, { template_id: templateId });
        setGraph(env.workflow);
        setIsCustom(env.is_custom);
        setTemplates(env.templates ?? templates);
        setSelectedNodeId(null);
        setSelectedEdgeId(null);
        setConnectingFromId(null);
        setDirty(false);
        setSaveStatus("saved");
        const freshCfg = await api.getConfig(projectId);
        setConfig(freshCfg);
      } else {
        const fallback = templates[templateId];
        if (fallback) {
          setGraph(fallback);
          setSelectedNodeId(null);
          setSelectedEdgeId(null);
          setConnectingFromId(null);
          setDirty(false);
          setSaveStatus("saved");
        }
      }
    } catch (e) {
      const fallback = templates[templateId];
      if (fallback) {
        setGraph(fallback);
        setDirty(true);
      }
      setSaveStatus("error");
      setStatusMessage(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleSaveWorkflow() {
    if (!graph) return;
    setSaveStatus("saving");
    setStatusMessage(null);
    try {
      if (projectId) {
        const env = await api.putWorkflow(projectId, { workflow: graph });
        setGraph(env.workflow);
        setIsCustom(env.is_custom);
        setTemplates(env.templates ?? templates);
        setDirty(false);
        setSaveStatus("saved");
        const freshCfg = await api.getConfig(projectId);
        setConfig(freshCfg);
      } else {
        await handlePublishWorkflowToStore();
      }
    } catch (e) {
      setSaveStatus("error");
      setStatusMessage(e instanceof Error ? e.message : String(e));
    }
  }

  async function handlePublishWorkflowToStore() {
    if (!graph) return;
    setSaveStatus("saving");
    setStatusMessage(null);
    try {
      const slug =
        graph.name
          .trim()
          .toLowerCase()
          .replace(/[^a-z0-9-_]+/g, "-")
          .replace(/^-+|-+$/g, "") || "custom-workflow";
      await api.publishStoreItem({
        kind: "workflow",
        name: slug,
        description: graph.description || `Reusable workflow (${graph.nodes.length} nodes)`,
        tags: ["workflow", "custom"],
        payload: { ...graph, template_id: slug } as unknown as Record<string, unknown>,
      });
      const updatedStore = await api.listStore();
      setStoreItems(updatedStore);
      setDirty(false);
      setSaveStatus("saved");
    } catch (e) {
      setSaveStatus("error");
      setStatusMessage(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleSaveNodePersona() {
    if (!activeAgentName) return;
    setPersonaSaveState("saving");
    setStatusMessage(null);
    try {
      const saved = await api.upsertAgent(activeAgentName, {
        role: nodePersonaRole,
        persona_md: nodePersonaMd,
        project_id: projectId ?? null,
      });
      setAgents((prev) => {
        const idx = prev.findIndex((a) => a.name === saved.name);
        if (idx >= 0) {
          const copy = [...prev];
          copy[idx] = saved;
          return copy;
        }
        return [...prev, saved];
      });
      setPersonaSaveState("saved");
    } catch (e) {
      setPersonaSaveState("error");
      setStatusMessage(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleCreateInlinePersona() {
    const cleanName = inlinePersonaName.trim().toLowerCase().replace(/\s+/g, "-");
    if (!cleanName || !selectedNode) return;
    setPersonaSaveState("saving");
    try {
      const created = await api.upsertAgent(cleanName, {
        role: inlinePersonaRole,
        persona_md:
          inlinePersonaMd.trim() ||
          `# ${cleanName}\n\nYou are the ${cleanName} agent.`,
        project_id: projectId ?? null,
      });
      setAgents((prev) => [
        ...prev.filter((a) => a.name !== created.name),
        created,
      ]);
      updateNode(selectedNode.id, { agent: created.name });
      setShowInlineNewPersona(false);
      setInlinePersonaName("");
      setInlinePersonaMd("");
      setPersonaSaveState("saved");
    } catch (e) {
      setPersonaSaveState("error");
      setStatusMessage(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleSaveAgentOverrides() {
    if (!activeAgentName || !projectId) return;
    setOverrideSaveState("saving");
    setStatusMessage(null);
    try {
      const prevOverrides =
        config.agent_overrides &&
        typeof config.agent_overrides === "object" &&
        !Array.isArray(config.agent_overrides)
          ? (config.agent_overrides as Record<string, Record<string, unknown>>)
          : {};

      const prevEntry = prevOverrides[activeAgentName] ?? {};
      const nextEntry: Record<string, unknown> = { ...prevEntry };

      if (overrideModel.trim()) {
        nextEntry.model = overrideModel.trim();
      } else {
        delete nextEntry.model;
      }

      if (overrideEffort) {
        nextEntry.effort = overrideEffort;
      } else {
        delete nextEntry.effort;
      }

      const parsedTimeout = Number(overrideTimeout);
      if (overrideTimeout.trim() !== "" && Number.isFinite(parsedTimeout) && parsedTimeout >= 1) {
        nextEntry.timeout_minutes = Math.round(parsedTimeout);
      } else {
        delete nextEntry.timeout_minutes;
      }

      const parsedRetries = Number(overrideRetries);
      if (overrideRetries.trim() !== "" && Number.isFinite(parsedRetries) && parsedRetries >= 1) {
        nextEntry.max_retries = Math.round(parsedRetries);
      } else {
        delete nextEntry.max_retries;
      }

      const nextOverrides = { ...prevOverrides };
      if (Object.keys(nextEntry).length > 0) {
        nextOverrides[activeAgentName] = nextEntry;
      } else {
        delete nextOverrides[activeAgentName];
      }

      const nextConfig: Record<string, unknown> = {
        ...config,
        agent_overrides: nextOverrides,
      };
      await api.patchConfig(projectId, nextConfig);
      setConfig(nextConfig);
      setOverrideSaveState("saved");
    } catch (e) {
      setOverrideSaveState("error");
      setStatusMessage(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleUpsertLibraryPersona() {
    const cleanName = (editingPersonaName ?? libPersonaName)
      .trim()
      .toLowerCase()
      .replace(/\s+/g, "-");
    if (!cleanName || !libPersonaMd.trim()) return;
    setLibBusy(true);
    setStatusMessage(null);
    try {
      const saved = await api.upsertAgent(cleanName, {
        role: libPersonaRole,
        persona_md: libPersonaMd,
        project_id: projectId ?? null,
      });
      const updatedStore = await api.listStore().catch(() => storeItems);
      setStoreItems(updatedStore);
      setAgents((prev) => {
        const idx = prev.findIndex((a) => a.name === saved.name);
        if (idx >= 0) {
          const copy = [...prev];
          copy[idx] = saved;
          return copy;
        }
        return [...prev, saved];
      });
      setEditingPersonaName(null);
      setShowNewPersonaForm(false);
      setLibPersonaName("");
      setLibPersonaMd("");
    } catch (e) {
      setStatusMessage(e instanceof Error ? e.message : String(e));
    } finally {
      setLibBusy(false);
    }
  }

  async function handleDeleteLibraryPersona(persona: AgentPersona) {
    setLibBusy(true);
    setStatusMessage(null);
    try {
      await api.deleteAgent(
        persona.name,
        persona.project_id && projectId ? projectId : undefined,
      );
      const refreshed = await api.listAgents(projectId ?? undefined);
      setAgents(refreshed);
      if (editingPersonaName === persona.name) {
        setEditingPersonaName(null);
      }
    } catch (e) {
      setStatusMessage(e instanceof Error ? e.message : String(e));
    } finally {
      setLibBusy(false);
    }
  }

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center gap-2.5 text-sm text-zinc-400">
        <Spinner size={18} />
        Loading Agent Studio & workflow graph…
      </div>
    );
  }

  if (loadError || !graph) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 rounded-xl border border-rose-900/60 bg-rose-950/30 p-6 text-center">
        <div className="text-sm font-semibold text-rose-300">
          Failed to load workflow studio
        </div>
        <div className="max-w-md font-mono text-xs text-rose-200/80">
          {loadError ?? "No workflow graph available"}
        </div>
        <Button variant="default" onClick={() => void loadAll()}>
          Retry
        </Button>
      </div>
    );
  }

  const activeTemplateId = graph.template_id ?? (isCustom ? "custom" : "standard");

  return (
    <div className="flex h-full min-h-0 flex-col gap-2.5">
      {/* Top Toolbar (Minimalist, single row) */}
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 rounded-xl border border-zinc-800/90 bg-zinc-900/80 px-3 py-2">
        {/* Left: Workflow Name & Custom/Default Badge + Preset Templates */}
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="text"
            value={graph.name}
            onChange={(e) =>
              updateGraph((prev) => ({ ...prev, name: e.target.value }))
            }
            aria-label="Workflow name"
            className="w-40 rounded-md border border-transparent bg-transparent px-1.5 py-0.5 text-sm font-semibold text-zinc-100 transition-colors hover:border-zinc-800 focus:border-indigo-500 focus:bg-zinc-950 focus:outline-none"
          />
          <Badge
            text={isCustom ? "Custom Graph" : "Default Policy"}
            color={
              isCustom
                ? "bg-indigo-950/80 text-indigo-300 border border-indigo-800/70"
                : "bg-zinc-800 text-zinc-300 border border-zinc-700"
            }
          />
          {dirty && (
            <Badge
              text="Unsaved"
              color="bg-amber-950/80 text-amber-300 border border-amber-800/70"
            />
          )}

          <span className="hidden h-4 w-px bg-zinc-800 lg:inline-block" />

          {/* Preset Templates Selector Pills */}
          <div className="flex items-center gap-1 rounded-lg border border-zinc-800 bg-zinc-950/80 p-0.5">
            {BUILTIN_TEMPLATES.map((tpl) => {
              const active = activeTemplateId === tpl.id;
              return (
                <button
                  key={tpl.id}
                  type="button"
                  title={tpl.hint}
                  onClick={() => void handleSelectTemplate(tpl.id)}
                  className={`rounded-md px-2 py-1 text-[11px] font-medium transition-colors ${
                    active
                      ? "bg-indigo-600 text-white"
                      : "text-zinc-400 hover:bg-zinc-800/80 hover:text-zinc-200"
                  }`}
                >
                  {tpl.label}
                </button>
              );
            })}
          </div>
        </div>

        {/* Right: Parallel Reviewers toggle + Actions */}
        <div className="flex flex-wrap items-center gap-1.5">
          <label
            title="Run independent reviewer nodes concurrently"
            className="flex cursor-pointer items-center gap-1.5 rounded-lg border border-zinc-800 bg-zinc-950/70 px-2.5 py-1 text-xs text-zinc-300 transition-colors hover:border-zinc-700"
          >
            <input
              type="checkbox"
              checked={graph.parallel_reviewers}
              onChange={(e) =>
                updateGraph((prev) => ({
                  ...prev,
                  parallel_reviewers: e.target.checked,
                }))
              }
              className="h-3.5 w-3.5 accent-indigo-500"
            />
            <span>Parallel Reviewers</span>
          </label>

          <Button size="sm" onClick={() => handleAddNode()}>
            + Add Node
          </Button>

          <Button
            size="sm"
            variant={connectingFromId ? "primary" : "default"}
            onClick={() => {
              if (connectingFromId) {
                setConnectingFromId(null);
              } else {
                const startId = selectedNodeId ?? graph.nodes[0]?.id ?? null;
                setConnectingFromId(startId);
              }
            }}
            title={
              connectingFromId
                ? "Cancel connection mode"
                : "Connect selected node to another node"
            }
          >
            {connectingFromId ? "Cancel Connect" : "Connect Nodes"}
          </Button>

          <Button size="sm" variant="ghost" onClick={handleAutoLayout}>
            Auto-Layout
          </Button>

          <Button
            size="sm"
            variant="default"
            disabled={saveStatus === "saving"}
            onClick={() => void handlePublishWorkflowToStore()}
            title="Publish this workflow diagram to the Global Store so any repository can use it"
          >
            Publish to Store
          </Button>

          {projectId && (
            <Button
              size="sm"
              variant="primary"
              disabled={saveStatus === "saving"}
              onClick={() => void handleSaveWorkflow()}
            >
              {saveStatus === "saving"
                ? "Saving…"
                : saveStatus === "saved" && !dirty
                  ? "Saved ✓"
                  : "Save to Repo"}
            </Button>
          )}

          <Button
            size="sm"
            variant="ghost"
            onClick={() => setDrawerOpen((o) => !o)}
            title={drawerOpen ? "Hide Inspector" : "Show Inspector"}
          >
            {drawerOpen ? "Hide Panel" : `Inspector (${agents.length})`}
          </Button>
        </div>
      </div>

      {/* Error / Status Notification Banner */}
      {statusMessage && (
        <div className="flex shrink-0 items-center justify-between gap-2 rounded-lg border border-rose-800/70 bg-rose-950/50 px-3 py-1.5 text-xs text-rose-200">
          <span className="truncate font-mono">{statusMessage}</span>
          <button
            type="button"
            onClick={() => setStatusMessage(null)}
            className="text-rose-400 hover:text-rose-200"
          >
            Dismiss
          </button>
        </div>
      )}

      {/* Main Split Area: Center Canvas + Right Inspector Drawer */}
      <div className="flex min-h-0 flex-1 gap-3 overflow-hidden">
        <WorkflowCanvas
          graph={graph}
          selectedNodeId={selectedNodeId}
          selectedEdgeId={selectedEdgeId}
          connectingFromId={connectingFromId}
          onSelectNode={handleSelectNode}
          onSelectEdge={handleSelectEdge}
          onMoveNode={handleMoveNode}
          onCompleteConnect={handleCompleteConnect}
          onAutoLayout={handleAutoLayout}
        />

        {drawerOpen && (
          <aside className="flex h-full w-80 shrink-0 flex-col overflow-hidden rounded-xl border border-zinc-800/90 bg-zinc-900/90 lg:w-96">
            {/* Inspector Sub-Tabs Header */}
            <div className="flex shrink-0 items-center border-b border-zinc-800 px-2 pt-1.5">
              <button
                type="button"
                onClick={() => setInspectorTab("inspector")}
                className={`flex-1 border-b-2 py-2 text-xs font-semibold transition-colors ${
                  inspectorTab === "inspector"
                    ? "border-indigo-500 text-white"
                    : "border-transparent text-zinc-400 hover:text-zinc-200"
                }`}
              >
                Inspector
              </button>
              <button
                type="button"
                onClick={() => setInspectorTab("personas")}
                className={`flex-1 border-b-2 py-2 text-xs font-semibold transition-colors ${
                  inspectorTab === "personas"
                    ? "border-indigo-500 text-white"
                    : "border-transparent text-zinc-400 hover:text-zinc-200"
                }`}
              >
                Agents ({agents.length})
              </button>
              <button
                type="button"
                onClick={() => setInspectorTab("store")}
                className={`flex-1 border-b-2 py-2 text-xs font-semibold transition-colors ${
                  inspectorTab === "store"
                    ? "border-indigo-500 text-white"
                    : "border-transparent text-zinc-400 hover:text-zinc-200"
                }`}
              >
                Store ({storeItems.length})
              </button>
            </div>

            {/* Drawer Body */}
            <div className="min-h-0 flex-1 overflow-y-auto p-3.5">
              {inspectorTab === "inspector" ? (
                selectedNode ? (
                  <div className="space-y-4">
                    {/* Selected Node Header + Quick Actions */}
                    <div className="flex items-center justify-between gap-2 border-b border-zinc-800/80 pb-2.5">
                      <div className="min-w-0">
                        <div className="flex items-center gap-1.5">
                          <span
                            className={`inline-block h-2 w-2 rounded-full ${
                              (
                                NODE_KIND_COLORS[selectedNode.kind] ??
                                NODE_KIND_COLORS.custom
                              ).dot
                            }`}
                          />
                          <span className="truncate font-mono text-xs font-semibold text-zinc-200">
                            {selectedNode.id}
                          </span>
                        </div>
                        <div className="text-[11px] text-zinc-500">
                          Position ({selectedNode.position_x},{" "}
                          {selectedNode.position_y})
                        </div>
                      </div>
                      <div className="flex items-center gap-1.5">
                        <Button
                          size="sm"
                          variant={
                            connectingFromId === selectedNode.id
                              ? "primary"
                              : "default"
                          }
                          onClick={() =>
                            setConnectingFromId(
                              connectingFromId === selectedNode.id
                                ? null
                                : selectedNode.id,
                            )
                          }
                        >
                          {connectingFromId === selectedNode.id
                            ? "Connecting…"
                            : "Connect to..."}
                        </Button>
                        <Button
                          size="sm"
                          variant="danger"
                          onClick={handleDeleteSelectedNode}
                        >
                          Delete Node
                        </Button>
                      </div>
                    </div>

                    {/* Core Node Fields */}
                    <div className="space-y-2.5">
                      <label className="block">
                        <span className="mb-1 block text-xs font-medium text-zinc-400">
                          Label
                        </span>
                        <input
                          type="text"
                          className={input}
                          value={selectedNode.label}
                          onChange={(e) =>
                            updateNode(selectedNode.id, {
                              label: e.target.value,
                            })
                          }
                        />
                      </label>

                      <div className="grid grid-cols-2 gap-2">
                        <label className="block">
                          <span className="mb-1 block text-xs font-medium text-zinc-400">
                            Node Kind
                          </span>
                          <select
                            className={input}
                            value={selectedNode.kind}
                            onChange={(e) =>
                              updateNode(selectedNode.id, {
                                kind: e.target.value as NodeKind,
                              })
                            }
                          >
                            {NODE_KINDS.map((k) => (
                              <option key={k} value={k}>
                                {k}
                              </option>
                            ))}
                          </select>
                        </label>

                        <label className="block">
                          <span className="mb-1 block text-xs font-medium text-zinc-400">
                            Output Contract
                          </span>
                          <select
                            className={input}
                            value={selectedNode.output_contract}
                            onChange={(e) =>
                              updateNode(selectedNode.id, {
                                output_contract: e.target
                                  .value as OutputContract,
                              })
                            }
                          >
                            {OUTPUT_CONTRACTS.map((c) => (
                              <option key={c} value={c}>
                                {c}
                              </option>
                            ))}
                          </select>
                        </label>
                      </div>

                      {/* Agent Persona Selector + Inline Create */}
                      <div>
                        <div className="mb-1 flex items-center justify-between">
                          <span className="text-xs font-medium text-zinc-400">
                            Agent Persona
                          </span>
                          <button
                            type="button"
                            onClick={() =>
                              setShowInlineNewPersona((v) => !v)
                            }
                            className="text-[11px] font-medium text-indigo-400 hover:text-indigo-300"
                          >
                            {showInlineNewPersona
                              ? "Cancel"
                              : "+ New Persona"}
                          </button>
                        </div>
                        <select
                          className={input}
                          value={selectedNode.agent ?? ""}
                          onChange={(e) =>
                            updateNode(selectedNode.id, {
                              agent: e.target.value || null,
                            })
                          }
                        >
                          <option value="">(None — built-in stage)</option>
                          {agents.map((a) => (
                            <option key={a.name} value={a.name}>
                              @{a.name} ({a.role})
                            </option>
                          ))}
                        </select>

                        {showInlineNewPersona && (
                          <div className="mt-2 space-y-2 rounded-lg border border-indigo-800/50 bg-indigo-950/20 p-2.5">
                            <div className="text-[11px] font-semibold text-indigo-300">
                              Create & Assign New Persona
                            </div>
                            <div className="grid grid-cols-2 gap-2">
                              <input
                                type="text"
                                placeholder="persona-name"
                                className={input}
                                value={inlinePersonaName}
                                onChange={(e) =>
                                  setInlinePersonaName(e.target.value)
                                }
                              />
                              <select
                                className={input}
                                value={inlinePersonaRole}
                                onChange={(e) =>
                                  setInlinePersonaRole(e.target.value)
                                }
                              >
                                {AGENT_ROLES.map((r) => (
                                  <option key={r} value={r}>
                                    {r}
                                  </option>
                                ))}
                              </select>
                            </div>
                            <textarea
                              rows={3}
                              placeholder="System instructions (Markdown)…"
                              className={`${input} font-mono text-xs`}
                              value={inlinePersonaMd}
                              onChange={(e) =>
                                setInlinePersonaMd(e.target.value)
                              }
                            />
                            <Button
                              size="sm"
                              variant="primary"
                              disabled={!inlinePersonaName.trim()}
                              onClick={() => void handleCreateInlinePersona()}
                            >
                              Create & Assign
                            </Button>
                          </div>
                        )}
                      </div>

                      {/* Enabled, Optional & Human-in-the-Loop Toggles */}
                      <div className="flex flex-wrap items-center gap-4 pt-1">
                        <label className="flex cursor-pointer items-center gap-2 text-xs text-zinc-200">
                          <input
                            type="checkbox"
                            className="h-3.5 w-3.5 accent-indigo-500"
                            checked={selectedNode.enabled}
                            onChange={(e) =>
                              updateNode(selectedNode.id, {
                                enabled: e.target.checked,
                              })
                            }
                          />
                          <span>Enabled</span>
                        </label>
                        <label className="flex cursor-pointer items-center gap-2 text-xs text-zinc-200">
                          <input
                            type="checkbox"
                            className="h-3.5 w-3.5 accent-indigo-500"
                            checked={selectedNode.optional}
                            onChange={(e) =>
                              updateNode(selectedNode.id, {
                                optional: e.target.checked,
                              })
                            }
                          />
                          <span>Optional (conditional)</span>
                        </label>
                        <label
                          title="Pause background worker at waiting_for_approval after this node finishes"
                          className="flex cursor-pointer items-center gap-2 text-xs text-sky-300"
                        >
                          <input
                            type="checkbox"
                            className="h-3.5 w-3.5 accent-sky-500"
                            checked={Boolean(
                              selectedNode.pause_after ||
                                selectedNode.await_human_approval,
                            )}
                            onChange={(e) =>
                              updateNode(selectedNode.id, {
                                pause_after: e.target.checked,
                                await_human_approval: e.target.checked,
                              })
                            }
                          />
                          <span>Pause after node (HITL approval)</span>
                        </label>
                      </div>
                    </div>

                    {/* Diff Triggers (shown for reviewer/custom or nodes with triggers) */}
                    {(selectedNode.kind === "reviewer" ||
                      selectedNode.kind === "custom" ||
                      selectedNode.triggers.length > 0) && (
                      <div className="space-y-2 rounded-lg border border-zinc-800 bg-zinc-950/60 p-2.5">
                        <div className="flex items-center justify-between">
                          <span className="text-xs font-semibold text-zinc-300">
                            Diff Triggers
                          </span>
                          <span className="text-[10px] text-zinc-500">
                            {selectedNode.triggers.length} active
                          </span>
                        </div>
                        <div className="flex flex-wrap gap-1">
                          {COMMON_TRIGGERS.map((trig) => {
                            const active =
                              selectedNode.triggers.includes(trig);
                            return (
                              <button
                                key={trig}
                                type="button"
                                onClick={() => {
                                  const next = active
                                    ? selectedNode.triggers.filter(
                                        (t) => t !== trig,
                                      )
                                    : [...selectedNode.triggers, trig];
                                  updateNode(selectedNode.id, {
                                    triggers: next,
                                    optional:
                                      next.length > 0
                                        ? true
                                        : selectedNode.optional,
                                  });
                                }}
                                className={`rounded-md border px-2 py-0.5 font-mono text-[10px] transition-colors ${
                                  active
                                    ? "border-amber-600/80 bg-amber-950/80 text-amber-200"
                                    : "border-zinc-800 bg-zinc-900 text-zinc-400 hover:border-zinc-700"
                                }`}
                              >
                                {trig}
                              </button>
                            );
                          })}
                        </div>
                        {/* Custom triggers not in COMMON_TRIGGERS */}
                        {selectedNode.triggers.filter(
                          (t) => !COMMON_TRIGGERS.includes(t),
                        ).length > 0 && (
                          <div className="flex flex-wrap gap-1 pt-1">
                            {selectedNode.triggers
                              .filter((t) => !COMMON_TRIGGERS.includes(t))
                              .map((trig) => (
                                <button
                                  key={trig}
                                  type="button"
                                  onClick={() =>
                                    updateNode(selectedNode.id, {
                                      triggers: selectedNode.triggers.filter(
                                        (t) => t !== trig,
                                      ),
                                    })
                                  }
                                  className="rounded-md border border-indigo-700/70 bg-indigo-950/70 px-2 py-0.5 font-mono text-[10px] text-indigo-200"
                                >
                                  {trig} ×
                                </button>
                              ))}
                          </div>
                        )}
                        <div className="flex gap-1.5 pt-1">
                          <input
                            type="text"
                            placeholder="Custom keyword or regex trigger…"
                            className={`${input} text-xs`}
                            value={customTriggerInput}
                            onChange={(e) =>
                              setCustomTriggerInput(e.target.value)
                            }
                            onKeyDown={(e) => {
                              if (
                                e.key === "Enter" &&
                                customTriggerInput.trim()
                              ) {
                                e.preventDefault();
                                const t = customTriggerInput.trim();
                                if (!selectedNode.triggers.includes(t)) {
                                  updateNode(selectedNode.id, {
                                    triggers: [...selectedNode.triggers, t],
                                    optional: true,
                                  });
                                }
                                setCustomTriggerInput("");
                              }
                            }}
                          />
                          <Button
                            size="sm"
                            onClick={() => {
                              const t = customTriggerInput.trim();
                              if (!t) return;
                              if (!selectedNode.triggers.includes(t)) {
                                updateNode(selectedNode.id, {
                                  triggers: [...selectedNode.triggers, t],
                                  optional: true,
                                });
                              }
                              setCustomTriggerInput("");
                            }}
                          >
                            Add
                          </Button>
                        </div>
                      </div>
                    )}

                    {/* Per-Agent Model & Effort Overrides */}
                    {selectedNode.agent && (
                      <div className="space-y-2.5 rounded-lg border border-zinc-800 bg-zinc-950/60 p-2.5">
                        <div className="flex items-center justify-between">
                          <span className="text-xs font-semibold text-zinc-300">
                            Model & Effort Overrides (@{selectedNode.agent})
                          </span>
                          {overrideSaveState === "saved" && (
                            <span className="text-[10px] font-medium text-emerald-400">
                              Saved ✓
                            </span>
                          )}
                        </div>
                        <label className="block">
                          <span className="mb-1 block text-[11px] text-zinc-400">
                            Model Override
                          </span>
                          <input
                            type="text"
                            placeholder="e.g. deepseek/deepseek-chat (blank = default)"
                            className={`${input} font-mono text-xs`}
                            value={overrideModel}
                            onChange={(e) => setOverrideModel(e.target.value)}
                          />
                        </label>
                        <div className="grid grid-cols-3 gap-2">
                          <label className="block">
                            <span className="mb-1 block text-[11px] text-zinc-400">
                              Effort
                            </span>
                            <select
                              className={`${input} text-xs`}
                              value={overrideEffort}
                              onChange={(e) =>
                                setOverrideEffort(e.target.value)
                              }
                            >
                              <option value="">Default</option>
                              {EFFORT_TIERS.map((ef) => (
                                <option key={ef} value={ef}>
                                  {ef}
                                </option>
                              ))}
                            </select>
                          </label>
                          <label className="block">
                            <span className="mb-1 block text-[11px] text-zinc-400">
                              Timeout (m)
                            </span>
                            <input
                              type="number"
                              min={1}
                              max={600}
                              placeholder="120"
                              className={`${input} text-xs`}
                              value={overrideTimeout}
                              onChange={(e) =>
                                setOverrideTimeout(e.target.value)
                              }
                            />
                          </label>
                          <label className="block">
                            <span className="mb-1 block text-[11px] text-zinc-400">
                              Max Retries
                            </span>
                            <input
                              type="number"
                              min={1}
                              max={50}
                              placeholder="3"
                              className={`${input} text-xs`}
                              value={overrideRetries}
                              onChange={(e) =>
                                setOverrideRetries(e.target.value)
                              }
                            />
                          </label>
                        </div>
                        <Button
                          size="sm"
                          onClick={() => void handleSaveAgentOverrides()}
                          disabled={overrideSaveState === "saving"}
                        >
                          {overrideSaveState === "saving"
                            ? "Saving Overrides…"
                            : "Save Agent Overrides"}
                        </Button>
                      </div>
                    )}

                    {/* Agent System Prompt (persona_md) & Node Prompt Template */}
                    <div className="space-y-2.5 rounded-lg border border-zinc-800 bg-zinc-950/60 p-2.5">
                      {selectedNode.agent && (
                        <>
                          <div className="flex items-center justify-between">
                            <span className="text-xs font-semibold text-zinc-300">
                              Agent System Prompt (@{selectedNode.agent})
                            </span>
                            {personaSaveState === "saved" && (
                              <span className="text-[10px] font-medium text-emerald-400">
                                Saved ✓
                              </span>
                            )}
                          </div>
                          <textarea
                            rows={6}
                            className={`${input} font-mono text-xs leading-relaxed`}
                            placeholder="Markdown system instructions for this agent persona…"
                            value={nodePersonaMd}
                            onChange={(e) => setNodePersonaMd(e.target.value)}
                          />
                          <Button
                            size="sm"
                            variant="primary"
                            disabled={personaSaveState === "saving"}
                            onClick={() => void handleSaveNodePersona()}
                          >
                            {personaSaveState === "saving"
                              ? "Saving Persona…"
                              : "Save Persona"}
                          </Button>
                        </>
                      )}

                      <label className="block pt-1">
                        <span className="mb-1 block text-[11px] font-medium text-zinc-400">
                          Node Prompt Template (Optional per-node instructions)
                        </span>
                        <textarea
                          rows={3}
                          className={`${input} font-mono text-xs`}
                          placeholder="Extra instructions injected specifically for this node stage…"
                          value={selectedNode.prompt_template ?? ""}
                          onChange={(e) =>
                            updateNode(selectedNode.id, {
                              prompt_template: e.target.value || null,
                            })
                          }
                        />
                      </label>
                    </div>
                  </div>
                ) : selectedEdge ? (
                  <div className="space-y-4">
                    {/* Edge Inspector */}
                    <div className="flex items-center justify-between border-b border-zinc-800/80 pb-2.5">
                      <div>
                        <div className="font-mono text-xs font-semibold text-zinc-200">
                          {selectedEdge.source} → {selectedEdge.target}
                        </div>
                        <div className="text-[11px] text-zinc-500">
                          Transition Edge ({selectedEdge.id})
                        </div>
                      </div>
                      <Button
                        size="sm"
                        variant="danger"
                        onClick={handleDeleteSelectedEdge}
                      >
                        Delete Edge
                      </Button>
                    </div>

                    <label className="block">
                      <span className="mb-1 block text-xs font-medium text-zinc-400">
                        Condition
                      </span>
                      <select
                        className={input}
                        value={selectedEdge.condition}
                        onChange={(e) =>
                          updateEdge(selectedEdge.id, {
                            condition: e.target.value as EdgeCondition,
                          })
                        }
                      >
                        {EDGE_CONDITIONS.map((c) => (
                          <option key={c} value={c}>
                            {c}
                          </option>
                        ))}
                      </select>
                    </label>

                    {selectedEdge.condition === "on_fail" && (
                      <label className="block">
                        <span className="mb-1 block text-xs font-medium text-zinc-400">
                          Max Retry Iterations (1–50)
                        </span>
                        <input
                          type="number"
                          min={1}
                          max={50}
                          className={input}
                          value={selectedEdge.max_iterations}
                          onChange={(e) =>
                            updateEdge(selectedEdge.id, {
                              max_iterations: Math.max(
                                1,
                                Math.min(50, Number(e.target.value) || 1),
                              ),
                            })
                          }
                        />
                      </label>
                    )}

                    <label className="block">
                      <span className="mb-1 block text-xs font-medium text-zinc-400">
                        Edge Label
                      </span>
                      <input
                        type="text"
                        placeholder="Optional label on diagram…"
                        className={input}
                        value={selectedEdge.label}
                        onChange={(e) =>
                          updateEdge(selectedEdge.id, {
                            label: e.target.value,
                          })
                        }
                      />
                    </label>

                    <label
                      title="Pause execution on this connector until approved by a human operator or MCP tool"
                      className="flex cursor-pointer items-center gap-2.5 rounded-lg border border-sky-800/50 bg-sky-950/30 p-2.5 text-xs text-sky-200"
                    >
                      <input
                        type="checkbox"
                        className="h-4 w-4 accent-sky-500"
                        checked={Boolean(selectedEdge.require_human)}
                        onChange={(e) =>
                          updateEdge(selectedEdge.id, {
                            require_human: e.target.checked,
                          })
                        }
                      />
                      <div>
                        <div className="font-semibold">
                          Human-in-the-Loop Connector
                        </div>
                        <div className="text-[11px] text-sky-300/80">
                          Pause at waiting_for_approval before advancing to{" "}
                          <span className="font-mono">{selectedEdge.target}</span>
                        </div>
                      </div>
                    </label>
                  </div>
                ) : (
                  <div className="flex h-full flex-col items-center justify-center py-12 text-center">
                    <div className="mb-2 text-2xl text-zinc-700">◇</div>
                    <div className="text-xs font-semibold text-zinc-300">
                      Nothing Selected
                    </div>
                    <p className="mt-1 max-w-[220px] text-xs text-zinc-500">
                      Click any node or edge on the canvas to configure its
                      agent persona, diff triggers, model overrides, or retry
                      conditions.
                    </p>
                  </div>
                )
              ) : inspectorTab === "personas" ? (
                /* Personas Library Sub-Tab */
                <div className="space-y-3">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-semibold text-zinc-300">
                      Available Agent Personas
                    </span>
                    <Button
                      size="sm"
                      variant={showNewPersonaForm ? "ghost" : "primary"}
                      onClick={() => {
                        setEditingPersonaName(null);
                        setShowNewPersonaForm((v) => !v);
                        setLibPersonaName("");
                        setLibPersonaRole("conditional");
                        setLibPersonaMd("");
                      }}
                    >
                      {showNewPersonaForm
                        ? "Cancel"
                        : "+ New Agent Persona"}
                    </Button>
                  </div>

                  {(showNewPersonaForm || editingPersonaName) && (
                    <div className="space-y-2.5 rounded-lg border border-indigo-800/60 bg-indigo-950/20 p-3">
                      <div className="text-xs font-semibold text-indigo-300">
                        {editingPersonaName
                          ? `Edit @${editingPersonaName}`
                          : "New Agent Persona"}
                      </div>
                      {!editingPersonaName && (
                        <label className="block">
                          <span className="mb-1 block text-[11px] text-zinc-400">
                            Name
                          </span>
                          <input
                            type="text"
                            placeholder="e.g. data-privacy-auditor"
                            className={input}
                            value={libPersonaName}
                            onChange={(e) =>
                              setLibPersonaName(e.target.value)
                            }
                          />
                        </label>
                      )}
                      <label className="block">
                        <span className="mb-1 block text-[11px] text-zinc-400">
                          Role
                        </span>
                        <select
                          className={input}
                          value={libPersonaRole}
                          onChange={(e) => setLibPersonaRole(e.target.value)}
                        >
                          {AGENT_ROLES.map((r) => (
                            <option key={r} value={r}>
                              {r}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label className="block">
                        <span className="mb-1 block text-[11px] text-zinc-400">
                          System Prompt (persona_md)
                        </span>
                        <textarea
                          rows={6}
                          placeholder="# Agent Persona&#10;Describe responsibilities and review rules…"
                          className={`${input} font-mono text-xs`}
                          value={libPersonaMd}
                          onChange={(e) => setLibPersonaMd(e.target.value)}
                        />
                      </label>
                      <div className="flex justify-end gap-1.5">
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => {
                            setEditingPersonaName(null);
                            setShowNewPersonaForm(false);
                          }}
                        >
                          Cancel
                        </Button>
                        <Button
                          size="sm"
                          variant="primary"
                          disabled={
                            libBusy ||
                            (!editingPersonaName && !libPersonaName.trim()) ||
                            !libPersonaMd.trim()
                          }
                          onClick={() => void handleUpsertLibraryPersona()}
                        >
                          {libBusy ? "Saving…" : "Save Persona"}
                        </Button>
                      </div>
                    </div>
                  )}

                  <div className="space-y-2">
                    {agents.map((persona) => (
                      <div
                        key={`${persona.project_id ?? "global"}:${persona.name}`}
                        className="rounded-lg border border-zinc-800/90 bg-zinc-950/70 p-2.5"
                      >
                        <div className="flex items-start justify-between gap-2">
                          <div className="min-w-0">
                            <div className="flex items-center gap-1.5">
                              <span className="truncate font-mono text-xs font-semibold text-zinc-100">
                                @{persona.name}
                              </span>
                              <Badge text={persona.role} />
                              {persona.project_id && (
                                <Badge
                                  text="project"
                                  color="bg-indigo-950/80 text-indigo-300 border border-indigo-800/60"
                                />
                              )}
                            </div>
                            <p className="mt-1 line-clamp-2 font-mono text-[11px] text-zinc-400">
                              {persona.persona_md}
                            </p>
                          </div>
                        </div>

                        <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
                          <Button
                            size="sm"
                            variant="primary"
                            onClick={() => handleAddNode(persona)}
                          >
                            Place on Canvas
                          </Button>
                          <Button
                            size="sm"
                            onClick={() => {
                              setShowNewPersonaForm(false);
                              setEditingPersonaName(persona.name);
                              setLibPersonaRole(persona.role);
                              setLibPersonaMd(persona.persona_md);
                            }}
                          >
                            Edit
                          </Button>
                          <Button
                            size="sm"
                            variant="danger"
                            disabled={libBusy}
                            onClick={() =>
                              void handleDeleteLibraryPersona(persona)
                            }
                          >
                            {persona.project_id ? "Delete" : "Reset"}
                          </Button>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="space-y-3">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-semibold text-zinc-300">
                      Global Store (Reusable across all repos)
                    </span>
                    <Button
                      size="sm"
                      variant="default"
                      onClick={() => void handlePublishWorkflowToStore()}
                    >
                      + Save Current Graph
                    </Button>
                  </div>

                  {storeItems.map((item) => (
                    <div
                      key={item.id}
                      className="space-y-2 rounded-lg border border-zinc-800/90 bg-zinc-950/70 p-2.5"
                    >
                      <div className="flex items-center justify-between gap-2">
                        <div className="flex items-center gap-1.5 truncate">
                          <span className="truncate font-mono text-xs font-semibold text-zinc-100">
                            {item.name}
                          </span>
                          <Badge
                            text={item.kind}
                            color={
                              item.kind === "workflow"
                                ? "bg-indigo-950/80 text-indigo-300 border border-indigo-800/70"
                                : item.kind === "config_preset"
                                  ? "bg-emerald-950/80 text-emerald-300 border border-emerald-800/70"
                                  : "bg-zinc-800 text-zinc-300 border border-zinc-700"
                            }
                          />
                        </div>
                        {item.built_in && <Badge text="built-in" />}
                      </div>
                      <p className="text-[11px] text-zinc-400">{item.description}</p>
                      <div className="flex flex-wrap items-center gap-1.5 pt-1">
                        {item.kind === "workflow" && (
                          <Button
                            size="sm"
                            onClick={() => {
                              setGraph(item.payload as unknown as WorkflowGraph);
                              setDirty(true);
                            }}
                          >
                            Load on Canvas
                          </Button>
                        )}
                        {projectId && (
                          <Button
                            size="sm"
                            variant="primary"
                            onClick={async () => {
                              try {
                                await api.installStoreItem(projectId, {
                                  item_id: item.id,
                                });
                                await loadAll();
                              } catch (e) {
                                setStatusMessage(String(e));
                              }
                            }}
                          >
                            Apply to Repo
                          </Button>
                        )}
                        {!item.built_in && (
                          <Button
                            size="sm"
                            variant="danger"
                            onClick={async () => {
                              try {
                                await api.deleteStoreItem(item.id);
                                setStoreItems((prev) =>
                                  prev.filter((x) => x.id !== item.id),
                                );
                              } catch (e) {
                                setStatusMessage(String(e));
                              }
                            }}
                          >
                            Delete
                          </Button>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </aside>
        )}
      </div>
    </div>
  );
}
