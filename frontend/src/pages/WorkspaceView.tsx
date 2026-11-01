import type React from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "../api";
import { WorkflowCanvas } from "../components/WorkflowCanvas";
import type {
  BugCandidate,
  BugStatus,
  EventRow,
  PlanningMessage,
  PlanningSession,
  PlanningTaskNode,
  Project,
  ProjectMode,
  Task,
  TaskCheckpoint,
  Verdict,
  WorkflowEdge,
  WorkflowGraph,
  WorkflowNode,
} from "../types";
import {
  Badge,
  Button,
  NODE_KIND_COLORS,
  Spinner,
  StatusDot,
  STATE_COLORS,
  VERDICT_COLORS,
  fmtRelative,
  fmtTime,
  fmtTokens,
  input,
} from "../ui";
import type { StreamStatus } from "../useStream";
import { useStream } from "../useStream";

// ============================================================================
// Constants & Types
// ============================================================================

type ComposerMode = "task" | "swarm" | "plan" | "bug_sweep";
type FeedTab = "tasks" | "plans" | "bugs";
type TaskSubFilter = "all" | "active" | "failed" | "done";
type TaskInspectorTab = "workflow" | "trace" | "verdicts" | "details";

type SelectedItem =
  | { kind: "task"; id: string }
  | { kind: "plan"; id: string }
  | null;

interface TaskUsageData {
  task_id: string;
  entries?: {
    agent: string;
    model_reported: string | null;
    num_turns: number | null;
    input_tokens: number | null;
    output_tokens: number | null;
    cache_read_tokens: number | null;
    cache_creation_tokens: number | null;
    ts: string;
  }[];
  total_turns: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_creation_tokens: number;
  agent_count: number;
}

const TASK_SUB_FILTERS: {
  key: TaskSubFilter;
  label: string;
  states: string[] | null;
}[] = [
  { key: "all", label: "All", states: null },
  {
    key: "active",
    label: "Active",
    states: ["in_progress", "waiting_for_approval", "approved"],
  },
  { key: "failed", label: "Failed", states: ["failed"] },
  { key: "done", label: "Done", states: ["done"] },
];

const STATE_RANK: Record<string, number> = {
  waiting_for_approval: 0,
  in_progress: 1,
  approved: 2,
  inbox: 3,
  failed: 4,
  done: 5,
  cancelled: 6,
};

const CANONICAL_PIPELINE_STEPS = [
  "baseline",
  "planner",
  "developer",
  "review",
  "gate",
  "merge",
];

const PLAN_STATUS_COLORS: Record<string, string> = {
  active: "bg-amber-950/70 text-amber-300 border border-amber-700/70",
  stable: "bg-sky-950/70 text-sky-300 border border-sky-700/70",
  completed: "bg-emerald-950/70 text-emerald-300 border border-emerald-800/70",
  cancelled: "bg-zinc-900 text-zinc-500 border border-zinc-800",
};

const PLAN_NODE_COLORS: Record<string, string> = {
  proposed: "bg-zinc-800 text-zinc-300 border border-zinc-700",
  refined: "bg-amber-950/70 text-amber-300 border border-amber-800/70",
  approved: "bg-emerald-950/70 text-emerald-300 border border-emerald-800/70",
};

const BUG_STATUS_COLORS: Record<BugStatus, string> = {
  discovered: "bg-zinc-800 text-zinc-300 border border-zinc-700",
  reproduced: "bg-sky-950/70 text-sky-300 border border-sky-800/70",
  fixing: "bg-amber-950/70 text-amber-300 border border-amber-700/70",
  fixed: "bg-emerald-950/70 text-emerald-300 border border-emerald-800/70",
  dismissed_false_positive: "bg-zinc-900 text-zinc-500 border border-zinc-800",
  declined_needs_human: "bg-rose-950/80 text-rose-300 border border-rose-700/80",
  deferred: "bg-violet-950/70 text-violet-300 border border-violet-800/70",
};

const BUG_SEVERITY_COLORS: Record<string, string> = {
  critical: "bg-rose-950/80 text-rose-300 border border-rose-800/70",
  high: "bg-rose-950/60 text-rose-300 border border-rose-800/50",
  medium: "bg-amber-950/60 text-amber-300 border border-amber-800/50",
  low: "bg-zinc-800 text-zinc-400 border border-zinc-700",
};

const MAX_STREAM_ROWS = 300;
const INDENT_STEP = 14;
const INDENT_MAX = 42;
const DEFAULT_MODEL_ID = "google/gemini-2.5-flash-lite";

const TASK_LIFECYCLE_EVENTS = new Set([
  "task.created",
  "task.approved",
  "task.started",
  "task.committed",
  "task.merged",
  "task.done",
  "task.failed",
  "task.cancelled",
  "task.deleted",
]);

export interface Props {
  project: Project;
  lastEvent?: EventRow | null;
  liveNodeStatuses?: Record<string, TaskCheckpoint["status"]>;
}

// ============================================================================
// WorkspaceView
// ============================================================================

export function WorkspaceView({
  project,
  lastEvent = null,
  liveNodeStatuses,
}: Props) {
  const projectId = project.id;

  // --- Composer state ---
  const [composerMode, setComposerMode] = useState<ComposerMode>("task");
  const [composerText, setComposerText] = useState("");
  const [sourceBranch, setSourceBranch] = useState("");
  const [composerFocused, setComposerFocused] = useState(false);
  const [autoRun, setAutoRun] = useState(true);
  const [swarmConcurrency, setSwarmConcurrency] = useState<number>(4);
  const [defaultModel, setDefaultModel] = useState<string>(DEFAULT_MODEL_ID);
  const [availableModels, setAvailableModels] = useState<string[]>([
    DEFAULT_MODEL_ID,
    "google/gemini-3.1-flash-lite-preview",
    "google/gemini-2.5-flash",
    "google/gemini-2.5-pro",
    "deepseek/deepseek-chat",
    "anthropic/claude-sonnet-4-5",
  ]);
  const [submitting, setSubmitting] = useState(false);
  const [projectMode, setProjectMode] = useState<ProjectMode>(
    project.mode === "autonomous_bug_fixer" ? "autonomous_bug_fixer" : "task_queue",
  );
  const [switchingMode, setSwitchingMode] = useState(false);

  // --- Master Feed state ---
  const [feedTab, setFeedTab] = useState<FeedTab>("tasks");
  const [taskSubFilter, setTaskSubFilter] = useState<TaskSubFilter>("all");
  const [tasks, setTasks] = useState<Task[]>([]);
  const [plans, setPlans] = useState<PlanningSession[]>([]);
  const [bugs, setBugs] = useState<BugCandidate[]>([]);
  const [workflowGraph, setWorkflowGraph] = useState<WorkflowGraph | null>(null);
  const [showMiniDag, setShowMiniDag] = useState<boolean>(false);
  const [loadingFeed, setLoadingFeed] = useState(true);
  const [error, setError] = useState("");

  // --- Selection & Right Pane Inspector state ---
  const [selected, setSelected] = useState<SelectedItem>(null);
  const [inspectorTab, setInspectorTab] = useState<TaskInspectorTab>("workflow");
  const [selectedWfNodeId, setSelectedWfNodeId] = useState<string | null>(null);
  const [selectedWfEdgeId, setSelectedWfEdgeId] = useState<string | null>(null);
  const [hitlState, setHitlState] = useState<{ armed: string[]; waiting: string[] }>({
    armed: [],
    waiting: [],
  });
  const [nodeInput, setNodeInput] = useState("");
  const [nodeActionBusy, setNodeActionBusy] = useState(false);
  const [nodeBanner, setNodeBanner] = useState<string | null>(null);

  // --- Selected Task inspector data ---
  const [taskEvents, setTaskEvents] = useState<EventRow[]>([]);
  const [checkpoints, setCheckpoints] = useState<TaskCheckpoint[]>([]);
  const [verdicts, setVerdicts] = useState<Verdict[]>([]);
  const [usage, setUsage] = useState<TaskUsageData | null>(null);
  const [showAllProjectEventsInTask, setShowAllProjectEventsInTask] = useState(false);
  const [steerInput, setSteerInput] = useState("");
  const [steering, setSteering] = useState(false);
  const [steerSent, setSteerSent] = useState(false);
  const [taskActionBusy, setTaskActionBusy] = useState(false);
  const [compactingTask, setCompactingTask] = useState(false);
  const [taskCompactBanner, setTaskCompactBanner] = useState<string | null>(null);

  // --- Selected Planning Session inspector data ---
  const [planMessages, setPlanMessages] = useState<PlanningMessage[]>([]);
  const [planNodes, setPlanNodes] = useState<PlanningTaskNode[]>([]);
  const [humanNote, setHumanNote] = useState("");
  const [sendingNote, setSendingNote] = useState(false);
  const [approvingPlan, setApprovingPlan] = useState(false);
  const [cancellingPlan, setCancellingPlan] = useState(false);
  const [compactingPlan, setCompactingPlan] = useState(false);
  const [planCompactBanner, setPlanCompactBanner] = useState<string | null>(null);

  // --- Global stream filter (when nothing selected or in trace) ---
  const [streamFilter, setStreamFilter] = useState("");

  // --- Real-time WebSocket stream ---
  const {
    events: streamEvents,
    lastEvent: streamLastEvent,
    status: streamStatus,
  } = useStream(projectId);

  // Load recommended model catalog once
  useEffect(() => {
    api
      .listModels()
      .then((catalog) => {
        if (catalog.default_model) {
          setDefaultModel(catalog.default_model);
        }
        if (Array.isArray(catalog.models) && catalog.models.length > 0) {
          const ids = catalog.models
            .map((m) => (typeof m === "string" ? m : m.id))
            .filter(Boolean);
          if (ids.length > 0) setAvailableModels(ids);
        }
      })
      .catch(() => {
        /* fallback to default google/gemini-2.5-flash-lite */
      });
  }, []);

  // Load project workflow graph for Mini-DAG
  useEffect(() => {
    api
      .getWorkflow(projectId)
      .then((env) => setWorkflowGraph(env.workflow))
      .catch(() => {});
  }, [projectId]);

  // Keep projectMode synced when project prop changes
  useEffect(() => {
    setProjectMode(
      project.mode === "autonomous_bug_fixer" ? "autonomous_bug_fixer" : "task_queue",
    );
  }, [project.mode]);

  // Reset state when switching projects
  useEffect(() => {
    setSelected(null);
    setTasks([]);
    setPlans([]);
    setBugs([]);
    setLoadingFeed(true);
    setError("");
    setComposerText("");
    setStreamFilter("");
    setTaskCompactBanner(null);
    setPlanCompactBanner(null);
  }, [projectId]);

  // --- Data reloaders ---
  const reloadAll = useCallback(async () => {
    try {
      const [taskList, planList, bugList] = await Promise.all([
        api.listTasks(projectId),
        api.listPlanningSessions(projectId),
        api.bugCandidates(projectId),
      ]);
      setTasks(taskList);
      setPlans(planList);
      setBugs(bugList);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoadingFeed(false);
    }
  }, [projectId]);

  useEffect(() => {
    void reloadAll();
    const id = setInterval(() => {
      void reloadAll();
    }, 3500);
    return () => clearInterval(id);
  }, [reloadAll]);

  // --- Selected Task & Planning Session identifiers ---
  const selectedTaskId = selected?.kind === "task" ? selected.id : null;
  const selectedPlanId = selected?.kind === "plan" ? selected.id : null;
  const activePlanningId = selectedPlanId;

  const selectedTask = useMemo(
    () => (selectedTaskId ? tasks.find((t) => t.id === selectedTaskId) ?? null : null),
    [tasks, selectedTaskId],
  );

  const selectedPlan = useMemo(
    () => (selectedPlanId ? plans.find((p) => p.id === selectedPlanId) ?? null : null),
    [plans, selectedPlanId],
  );
  const activePlanning = selectedPlan;

  const loadSelectedTaskDetails = useCallback(async (tid: string) => {
    try {
      const [evs, cps, vds, usg, hitl] = await Promise.all([
        api.taskEvents(tid),
        api.taskCheckpoints(tid),
        api.taskVerdicts(tid),
        api.taskUsage(tid),
        api.getTaskHitl(tid).catch(() => ({ task_id: tid, armed: [], waiting: [] })),
      ]);
      setTaskEvents(evs);
      setCheckpoints(cps);
      setVerdicts(vds);
      setUsage(usg);
      setHitlState({ armed: hitl.armed ?? [], waiting: hitl.waiting ?? [] });
    } catch {
      /* ignore transient fetch errors */
    }
  }, []);

  useEffect(() => {
    setTaskCompactBanner(null);
    setNodeBanner(null);
    setSelectedWfNodeId(null);
    setSelectedWfEdgeId(null);
    setNodeInput("");
    if (!selectedTaskId) {
      setTaskEvents([]);
      setCheckpoints([]);
      setVerdicts([]);
      setUsage(null);
      setHitlState({ armed: [], waiting: [] });
      setSteerInput("");
      return;
    }
    void loadSelectedTaskDetails(selectedTaskId);
    const id = setInterval(() => {
      void loadSelectedTaskDetails(selectedTaskId);
    }, 2500);
    return () => clearInterval(id);
  }, [selectedTaskId, loadSelectedTaskDetails]);

  // --- Selected Plan data loader ---
  const loadSelectedPlanDetails = useCallback(async (sid: string) => {
    try {
      const [msgs, nodes] = await Promise.all([
        api.listPlanningMessages(sid),
        api.listPlanningTaskNodes(sid),
      ]);
      setPlanMessages(msgs);
      setPlanNodes(nodes);
    } catch {
      /* ignore transient fetch errors */
    }
  }, []);

  useEffect(() => {
    setPlanCompactBanner(null);
    if (!selectedPlanId) {
      setPlanMessages([]);
      setPlanNodes([]);
      setHumanNote("");
      return;
    }
    void loadSelectedPlanDetails(selectedPlanId);
    const id = setInterval(() => {
      void loadSelectedPlanDetails(selectedPlanId);
    }, 3000);
    return () => clearInterval(id);
  }, [selectedPlanId, loadSelectedPlanDetails]);

  // --- Surgical In-Place Live Event Patching (watching lastEvent) ---
  const effectiveLastEvent = lastEvent ?? streamLastEvent;
  useEffect(() => {
    if (!effectiveLastEvent) return;
    const evType = effectiveLastEvent.type ?? "";

    // 1. Task lifecycle events -> refresh tasks smoothly in place & selectedTask details
    if (TASK_LIFECYCLE_EVENTS.has(evType)) {
      void api
        .listTasks(project.id)
        .then((taskList) => {
          setTasks(taskList);
        })
        .catch(() => {});
      if (selectedTask && selectedTask.id === effectiveLastEvent.task_id) {
        void loadSelectedTaskDetails(selectedTask.id);
      }
    }

    // 2. Node execution & HITL events -> refresh selectedTask checkpoints and HITL state in real time
    if (
      evType === "node.started" ||
      evType === "node.completed" ||
      evType === "node.waiting_for_approval" ||
      evType === "hitl.waiting" ||
      evType === "hitl.resolved"
    ) {
      if (selectedTask?.id === effectiveLastEvent.task_id) {
        void loadSelectedTaskDetails(selectedTask.id);
      }
      void api
        .listTasks(project.id)
        .then((taskList) => {
          setTasks(taskList);
        })
        .catch(() => {});
    }

    // 3. Planning session events -> refresh sessions & active planning messages/nodes in place
    if (evType.startsWith("planning.")) {
      void api
        .listPlanningSessions(project.id)
        .then((planList) => {
          setPlans(planList);
        })
        .catch(() => {});
      const evPlanId =
        effectiveLastEvent.planning_session_id ??
        (typeof effectiveLastEvent.payload?.planning_session_id === "string"
          ? effectiveLastEvent.payload.planning_session_id
          : null);
      if (activePlanningId && activePlanningId === evPlanId) {
        void loadSelectedPlanDetails(activePlanningId);
      }
    }

    // 4. Verdict / usage / compaction updates on selected task
    if (
      selectedTask?.id &&
      effectiveLastEvent.task_id === selectedTask.id &&
      (evType === "agent.verdict" ||
        evType === "usage.recorded" ||
        evType === "context.compacted")
    ) {
      void loadSelectedTaskDetails(selectedTask.id);
    }

    // 5. Workflow updates -> refresh Mini-DAG workflow graph in place
    if (
      evType === "workflow.updated" &&
      (!effectiveLastEvent.project_id || effectiveLastEvent.project_id === project.id)
    ) {
      void api
        .getWorkflow(project.id)
        .then((env) => setWorkflowGraph(env.workflow))
        .catch(() => {});
    }
  }, [
    effectiveLastEvent,
    project.id,
    selectedTask,
    activePlanningId,
    loadSelectedTaskDetails,
    loadSelectedPlanDetails,
  ]);

  // --- Composer Actions ---
  const handleModeSwitch = useCallback(
    async (nextMode: ProjectMode) => {
      if (switchingMode || nextMode === projectMode) return;
      setSwitchingMode(true);
      setError("");
      const prev = projectMode;
      setProjectMode(nextMode);
      try {
        const updated = await api.setMode(projectId, nextMode);
        setProjectMode(
          updated.mode === "autonomous_bug_fixer" ? "autonomous_bug_fixer" : "task_queue",
        );
        void reloadAll();
      } catch (e) {
        setProjectMode(prev);
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setSwitchingMode(false);
      }
    },
    [projectId, projectMode, switchingMode, reloadAll],
  );

  const handleComposerSubmit = useCallback(async () => {
    if (composerMode === "bug_sweep" && !composerText.trim()) {
      const next: ProjectMode =
        projectMode === "autonomous_bug_fixer" ? "task_queue" : "autonomous_bug_fixer";
      await handleModeSwitch(next);
      return;
    }
    const text = composerText.trim();
    if (!text || submitting) return;
    setSubmitting(true);
    setError("");
    try {
      if (composerMode === "plan") {
        const session = await api.createPlanningSession(projectId, {
          title: text.slice(0, 72),
          prompt: text,
        });
        setComposerText("");
        setComposerFocused(false);
        setFeedTab("plans");
        setSelected({ kind: "plan", id: session.id });
        await reloadAll();
      } else if (composerMode === "swarm") {
        const lines = text
          .split("\n")
          .map((l) => l.trim())
          .filter(Boolean);
        const batch = await api.createSwarm(projectId, {
          units:
            lines.length > 1
              ? lines.map((l) => ({ title: l.slice(0, 72), request: l }))
              : undefined,
          prompt: lines.length === 1 ? text : undefined,
          concurrency: swarmConcurrency,
          auto_approve: autoRun,
        });
        setComposerText("");
        setComposerFocused(false);
        setFeedTab("tasks");
        if (batch.tasks.length > 0) {
          setSelected({ kind: "task", id: batch.tasks[0].id });
          setInspectorTab("workflow");
        }
        await reloadAll();
      } else {
        const created = await api.createTask(projectId, {
          request: text,
          title: "",
          use_planner: null,
          auto_approve: autoRun,
          source_branch: sourceBranch.trim() || null,
        });
        setComposerText("");
        setSourceBranch("");
        setComposerFocused(false);
        setFeedTab("tasks");
        setSelected({ kind: "task", id: created.id });
        setInspectorTab("workflow");
        await reloadAll();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }, [
    composerMode,
    composerText,
    sourceBranch,
    submitting,
    projectMode,
    handleModeSwitch,
    projectId,
    autoRun,
    swarmConcurrency,
    reloadAll,
  ]);

  // --- Task filtering & tree ---
  const taskCounts = useMemo(() => {
    const c: Record<TaskSubFilter, number> = {
      all: tasks.length,
      active: 0,
      failed: 0,
      done: 0,
    };
    for (const t of tasks) {
      if (
        t.state === "in_progress" ||
        t.state === "waiting_for_approval" ||
        t.state === "approved"
      )
        c.active++;
      if (t.state === "failed") c.failed++;
      if (t.state === "done") c.done++;
    }
    return c;
  }, [tasks]);

  const { roots: taskRoots, childrenOf } = useMemo(() => buildTaskTree(tasks), [tasks]);

  const filteredTaskRoots = useMemo(() => {
    const sub = TASK_SUB_FILTERS.find((f) => f.key === taskSubFilter)!;
    const stateOk = (t: Task) => sub.states === null || sub.states.includes(t.state);

    const subtreeMatches = (id: string): boolean => {
      const t = tasks.find((x) => x.id === id);
      if (t && stateOk(t)) return true;
      return (childrenOf.get(id) ?? []).some((c) => subtreeMatches(c.task.id));
    };

    const passing = taskRoots.filter((r) => subtreeMatches(r.task.id));
    return [...passing].sort((a, b) => {
      const ra = STATE_RANK[a.task.state] ?? 9;
      const rb = STATE_RANK[b.task.state] ?? 9;
      if (taskSubFilter === "all" && ra !== rb) return ra - rb;
      return new Date(b.task.created_at).getTime() - new Date(a.task.created_at).getTime();
    });
  }, [taskRoots, childrenOf, tasks, taskSubFilter]);

  const sortedBugs = useMemo(() => {
    return [...bugs].sort((a, b) => {
      const aHuman = a.status === "declined_needs_human" ? 0 : 1;
      const bHuman = b.status === "declined_needs_human" ? 0 : 1;
      if (aHuman !== bHuman) return aHuman - bHuman;
      return (
        new Date(b.last_examined_at).getTime() - new Date(a.last_examined_at).getTime()
      );
    });
  }, [bugs]);

  const needsHumanCount = useMemo(
    () => bugs.filter((b) => b.status === "declined_needs_human").length,
    [bugs],
  );

  const effectiveNodeStatuses = useMemo(() => {
    const map: Record<string, TaskCheckpoint["status"]> = {};
    if (liveNodeStatuses && checkpoints.length === 0 && taskEvents.length === 0) {
      Object.assign(map, liveNodeStatuses);
    }

    // 1. Replay persisted + live events for the selected task in chronological order
    const eventMeta = new Map<
      string,
      { status: TaskCheckpoint["status"]; attempt: number; ts: string }
    >();
    if (selectedTaskId) {
      const byId = new Map<number, EventRow>();
      for (const ev of taskEvents) {
        if (!ev.task_id || ev.task_id === selectedTaskId) {
          byId.set(ev.id, ev);
        }
      }
      for (const ev of streamEvents) {
        if (ev.task_id === selectedTaskId) {
          byId.set(ev.id, ev);
        }
      }
      const orderedEvents = Array.from(byId.values()).sort((a, b) => a.id - b.id);
      for (const ev of orderedEvents) {
        const nid =
          typeof ev.payload?.workflow_node_id === "string"
            ? ev.payload.workflow_node_id
            : typeof ev.payload?.node_id === "string"
              ? ev.payload.node_id
              : null;
        if (!nid) continue;
        const attempt =
          typeof ev.payload?.attempt === "number" ? ev.payload.attempt : 0;
        if (ev.type === "node.started") {
          map[nid] = "running";
          eventMeta.set(nid, { status: "running", attempt, ts: ev.ts });
        } else if (
          ev.type === "node.waiting_for_approval" ||
          ev.type === "hitl.waiting"
        ) {
          map[nid] = "waiting_for_approval";
          eventMeta.set(nid, {
            status: "waiting_for_approval",
            attempt,
            ts: ev.ts,
          });
        } else if (
          ev.type === "node.completed" ||
          ev.type === "hitl.resolved"
        ) {
          const rs = ev.payload?.status;
          const st: TaskCheckpoint["status"] =
            rs === "failed" ||
            rs === "skipped" ||
            rs === "running" ||
            rs === "waiting_for_approval"
              ? rs
              : "completed";
          map[nid] = st;
          eventMeta.set(nid, { status: st, attempt, ts: ev.ts });
        }
      }
    }

    // 2. Fold persisted checkpoints (keep highest attempt & latest updated_at per node)
    const latestCp = new Map<string, TaskCheckpoint>();
    for (const cp of checkpoints) {
      const prev = latestCp.get(cp.node_id);
      if (
        !prev ||
        cp.attempt > prev.attempt ||
        (cp.attempt === prev.attempt && cp.updated_at >= prev.updated_at)
      ) {
        latestCp.set(cp.node_id, cp);
      }
    }
    for (const [nid, cp] of latestCp) {
      const ev = eventMeta.get(nid);
      if (
        ev &&
        (ev.attempt > cp.attempt ||
          (ev.attempt === cp.attempt &&
            cp.status === "running" &&
            ev.status !== "running"))
      ) {
        map[nid] = ev.status;
      } else {
        map[nid] = cp.status;
      }
    }

    // 3. Apply active HITL waiting overrides when task is paused for human approval
    if (selectedTask?.state === "waiting_for_approval") {
      for (const waitId of hitlState.waiting) {
        if (workflowGraph?.nodes.some((n) => n.id === waitId)) {
          map[waitId] = "waiting_for_approval";
        }
      }
    }

    return map;
  }, [
    checkpoints,
    taskEvents,
    streamEvents,
    selectedTaskId,
    selectedTask?.state,
    hitlState.waiting,
    workflowGraph,
    liveNodeStatuses,
  ]);

  // Task Inspector quick actions (Approve / Cancel / Delete / Compact Context)
  const handleInspectorTaskAction = useCallback(
    async (fn: () => Promise<unknown>) => {
      if (taskActionBusy) return;
      setTaskActionBusy(true);
      setError("");
      try {
        await fn();
        await reloadAll();
        if (selectedTaskId) {
          await loadSelectedTaskDetails(selectedTaskId);
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setTaskActionBusy(false);
      }
    },
    [taskActionBusy, reloadAll, selectedTaskId, loadSelectedTaskDetails],
  );

  const handleCompactTask = useCallback(async () => {
    if (!selectedTask || compactingTask) return;
    setCompactingTask(true);
    setError("");
    try {
      const res = await api.compactTask(selectedTask.id);
      setTaskCompactBanner(
        `Compacted: saved ~${res.tokens_saved_est} tokens (${res.chars_before} -> ${res.chars_after} chars)`,
      );
      await Promise.all([reloadAll(), loadSelectedTaskDetails(selectedTask.id)]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCompactingTask(false);
    }
  }, [selectedTask, compactingTask, reloadAll, loadSelectedTaskDetails]);

  const handleCompactPlanningSession = useCallback(async () => {
    if (!activePlanning || compactingPlan) return;
    setCompactingPlan(true);
    setError("");
    try {
      const res = await api.compactPlanningSession(activePlanning.id);
      setPlanCompactBanner(
        `Compacted: saved ~${res.tokens_saved_est} tokens (${res.chars_before} -> ${res.chars_after} chars)`,
      );
      await loadSelectedPlanDetails(activePlanning.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCompactingPlan(false);
    }
  }, [activePlanning, compactingPlan, loadSelectedPlanDetails]);

  // Queue Fix Task for declined_needs_human bug candidates
  const handleQueueBugFixTask = useCallback(
    async (bug: BugCandidate) => {
      setError("");
      try {
        const loc = bug.file && bug.symbol
          ? `${bug.file}::${bug.symbol}`
          : bug.file ?? bug.symbol ?? "unknown location";
        const title = `Fix bug in ${loc}: ${bug.claim.slice(0, 72)}`;
        const request = [
          `Fix bug candidate (${bug.severity ?? "unspecified"} severity) at ${loc}.`,
          `Claim: ${bug.claim}`,
          bug.decline_reason ? `Prior auto-fix notes: ${bug.decline_reason}` : "",
        ]
          .filter(Boolean)
          .join("\n\n");

        const created = await api.createTask(projectId, {
          title,
          request,
          use_planner: null,
          auto_approve: true,
        });
        setFeedTab("tasks");
        setSelected({ kind: "task", id: created.id });
        setInspectorTab("trace");
        await reloadAll();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [projectId, reloadAll],
  );

  // Operator Steer submit
  const handleSteerSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (!selectedTaskId || !steerInput.trim() || steering) return;
      setSteering(true);
      setError("");
      try {
        await api.steer(selectedTaskId, steerInput.trim());
        setSteerInput("");
        setSteerSent(true);
        setTimeout(() => setSteerSent(false), 2500);
        void loadSelectedTaskDetails(selectedTaskId);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setSteering(false);
      }
    },
    [selectedTaskId, steerInput, steering, loadSelectedTaskDetails],
  );

  // Planning Session actions
  const handleSendPlanNote = useCallback(async () => {
    if (!selectedPlanId || !humanNote.trim() || sendingNote) return;
    setSendingNote(true);
    setError("");
    try {
      await api.addPlanningMessage(selectedPlanId, humanNote.trim());
      setHumanNote("");
      await loadSelectedPlanDetails(selectedPlanId);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSendingNote(false);
    }
  }, [selectedPlanId, humanNote, sendingNote, loadSelectedPlanDetails]);

  const handleApprovePlan = useCallback(async () => {
    if (!selectedPlanId || approvingPlan) return;
    setApprovingPlan(true);
    setError("");
    try {
      const res = await api.approvePlanningSession(selectedPlanId);
      await Promise.all([reloadAll(), loadSelectedPlanDetails(selectedPlanId)]);
      if (res.task_ids.length > 0) {
        setFeedTab("tasks");
        setSelected({ kind: "task", id: res.task_ids[0] });
        setInspectorTab("trace");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setApprovingPlan(false);
    }
  }, [selectedPlanId, approvingPlan, reloadAll, loadSelectedPlanDetails]);

  const handleCancelPlan = useCallback(async () => {
    if (!selectedPlanId || cancellingPlan) return;
    setCancellingPlan(true);
    setError("");
    try {
      await api.cancelPlanningSession(selectedPlanId);
      await Promise.all([reloadAll(), loadSelectedPlanDetails(selectedPlanId)]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCancellingPlan(false);
    }
  }, [selectedPlanId, cancellingPlan, reloadAll, loadSelectedPlanDetails]);

  // Combined + coalesced trace events for selected task
  const mergedTaskTrace = useMemo(() => {
    if (!selectedTaskId) return [];
    const byId = new Map<number, EventRow>();
    for (const ev of taskEvents) {
      byId.set(ev.id, ev);
    }
    for (const ev of streamEvents) {
      if (showAllProjectEventsInTask || ev.task_id === selectedTaskId) {
        byId.set(ev.id, ev);
      }
    }
    const sorted = Array.from(byId.values()).sort((a, b) => a.id - b.id);
    return coalesceOutputChunks(sorted);
  }, [selectedTaskId, taskEvents, streamEvents, showAllProjectEventsInTask]);

  // Coalesced project-wide live stream
  const coalescedProjectStream = useMemo(
    () => coalesceOutputChunks(streamEvents),
    [streamEvents],
  );

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      {/* ================================================================== */}
      {/* 1. Top Unified Composer Bar                                        */}
      {/* ================================================================== */}
      <div className="shrink-0 rounded-xl border border-zinc-800/90 bg-zinc-900/80 p-2.5 shadow-sm">
        <div className="flex flex-col gap-2.5 lg:flex-row lg:items-start">
          {/* Mode Selector Pill Group */}
          <div
            role="group"
            aria-label="Composer mode"
            className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-zinc-800 bg-zinc-950/80 p-1"
          >
            {(
              [
                { key: "task", label: "Task" },
                { key: "swarm", label: "Swarm" },
                { key: "plan", label: "Plan" },
                { key: "bug_sweep", label: "Bug Sweep" },
              ] as const
            ).map((m) => {
              const active = composerMode === m.key;
              return (
                <button
                  key={m.key}
                  type="button"
                  aria-pressed={active}
                  onClick={() => {
                    setComposerMode(m.key);
                    if (m.key === "plan") setFeedTab("plans");
                    if (m.key === "bug_sweep") setFeedTab("bugs");
                    if (m.key === "task" || m.key === "swarm") setFeedTab("tasks");
                  }}
                  className={`flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                    active
                      ? "bg-indigo-600 text-white shadow-xs"
                      : "text-zinc-400 hover:bg-zinc-800/70 hover:text-zinc-200"
                  }`}
                >
                  {m.key === "bug_sweep" && (
                    <span
                      className={`h-1.5 w-1.5 rounded-full ${
                        projectMode === "autonomous_bug_fixer"
                          ? "bg-amber-400 animate-pulse"
                          : "bg-zinc-500"
                      }`}
                    />
                  )}
                  {m.label}
                </button>
              );
            })}
          </div>

          {/* Input / Bug Sweep Controls */}
          <div className="min-w-0 flex-1">
            {composerMode === "bug_sweep" ? (
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-zinc-800/80 bg-zinc-950/60 px-3 py-1.5">
                <div className="flex flex-wrap items-center gap-2.5 text-xs">
                  <span className="font-medium text-zinc-200">Sweep Status:</span>
                  {projectMode === "autonomous_bug_fixer" ? (
                    <Badge
                      text="autonomous_bug_fixer · active"
                      color="bg-amber-950/80 text-amber-300 border border-amber-700/70"
                    />
                  ) : (
                    <Badge
                      text="task_queue · paused"
                      color="bg-zinc-800 text-zinc-400 border border-zinc-700"
                    />
                  )}
                  <span className="text-zinc-500 tabular-nums">
                    {bugs.length} candidates ·{" "}
                    {bugs.filter((b) => b.status === "fixing").length} fixing ·{" "}
                    {bugs.filter((b) => b.status === "fixed").length} fixed
                    {needsHumanCount > 0 ? (
                      <span className="ml-1.5 font-medium text-rose-400">
                        · {needsHumanCount} needs human
                      </span>
                    ) : null}
                  </span>
                </div>
                <div className="flex items-center gap-1.5">
                  <Button
                    size="sm"
                    variant={projectMode === "task_queue" ? "primary" : "default"}
                    disabled={switchingMode}
                    onClick={() => void handleModeSwitch("task_queue")}
                  >
                    {switchingMode && projectMode !== "task_queue" && <Spinner size={12} />}
                    Task Queue
                  </Button>
                  <Button
                    size="sm"
                    variant={
                      projectMode === "autonomous_bug_fixer" ? "primary" : "default"
                    }
                    disabled={switchingMode}
                    onClick={() => void handleModeSwitch("autonomous_bug_fixer")}
                  >
                    {switchingMode && projectMode !== "autonomous_bug_fixer" && (
                      <Spinner size={12} />
                    )}
                    Autonomous Bug Sweep
                  </Button>
                </div>
              </div>
            ) : (
              <textarea
                value={composerText}
                onFocus={() => setComposerFocused(true)}
                onBlur={() => {
                  if (!composerText.trim()) setComposerFocused(false);
                }}
                onChange={(e) => setComposerText(e.target.value)}
                onKeyDown={(e) => {
                  if (
                    (e.key === "Enter" && !e.shiftKey) ||
                    (e.key === "Enter" && (e.metaKey || e.ctrlKey))
                  ) {
                    e.preventDefault();
                    void handleComposerSubmit();
                  }
                }}
                placeholder={
                  composerMode === "plan"
                    ? "Describe an initiative for Planner, Architect, Tester, Security & Senior Reviewer to debate and decompose… (Enter to start)"
                    : composerMode === "swarm"
                      ? "Enter a mass-change objective (or one independent work unit per line) to fan out across dedicated parallel worktrees…"
                      : "Describe a task to implement, fix, or refactor… (Enter or Cmd/Ctrl+Enter to submit)"
                }
                rows={composerFocused || composerText.includes("\n") ? 3 : 1}
                className={`${input} resize-none transition-all duration-150 ${
                  composerFocused || composerText.includes("\n") ? "h-20" : "h-9"
                }`}
              />
            )}
          </div>

          {/* Right Action Controls */}
          {composerMode !== "bug_sweep" && (
            <div className="flex shrink-0 flex-wrap items-center justify-end gap-3 self-end lg:self-center">
              {composerMode === "task" && (
                <label
                  className="flex items-center gap-1.5 text-xs text-zinc-400"
                  title="Optional source branch for PR reviews (diffs against target branch)"
                >
                  <span className="hidden xl:inline text-zinc-500">PR Branch:</span>
                  <input
                    type="text"
                    value={sourceBranch}
                    onChange={(e) => setSourceBranch(e.target.value)}
                    placeholder="source branch (optional)"
                    aria-label="Source Branch"
                    className="w-36 rounded border border-zinc-800 bg-zinc-950 px-2 py-0.5 font-mono text-[11px] text-zinc-200 outline-none placeholder:text-zinc-600 focus:border-indigo-500"
                  />
                </label>
              )}
              {(composerMode === "task" || composerMode === "swarm") && (
                <label
                  className="flex items-center gap-1.5 text-xs text-zinc-400"
                  title="Default Model for launching tasks and swarms"
                >
                  <span className="hidden sm:inline text-zinc-500">Default Model:</span>
                  <select
                    value={defaultModel}
                    onChange={(e) => setDefaultModel(e.target.value)}
                    aria-label="Default Model"
                    className="rounded border border-zinc-800 bg-zinc-950 px-1.5 py-0.5 font-mono text-[11px] text-indigo-300 outline-none focus:border-indigo-500"
                  >
                    {availableModels.map((m) => (
                      <option key={m} value={m}>
                        {m}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              {composerMode === "swarm" && (
                <label className="flex items-center gap-1.5 text-xs text-zinc-400">
                  <span>Sessions:</span>
                  <select
                    value={swarmConcurrency}
                    onChange={(e) => setSwarmConcurrency(Number(e.target.value))}
                    className="rounded border border-zinc-800 bg-zinc-950 px-1.5 py-0.5 text-xs text-zinc-200 outline-none focus:border-indigo-500"
                  >
                    {[2, 4, 6, 8, 12].map((n) => (
                      <option key={n} value={n}>
                        {n}x parallel
                      </option>
                    ))}
                  </select>
                </label>
              )}
              {(composerMode === "task" || composerMode === "swarm") && (
                <label
                  className="flex cursor-pointer items-center gap-1.5 text-xs text-zinc-400 select-none hover:text-zinc-300"
                  title="Automatically approve and run immediately"
                >
                  <input
                    type="checkbox"
                    checked={autoRun}
                    onChange={(e) => setAutoRun(e.target.checked)}
                    className="h-3.5 w-3.5 rounded accent-indigo-500"
                  />
                  Auto-run
                </label>
              )}
              {composerMode === "plan" && (
                <span className="hidden text-[11px] text-zinc-500 xl:inline">
                  5-agent panel debate · {defaultModel}
                </span>
              )}
              <Button
                variant="primary"
                onClick={() => void handleComposerSubmit()}
                disabled={submitting || !composerText.trim()}
              >
                {submitting ? (
                  <>
                    <Spinner size={12} /> Working…
                  </>
                ) : composerMode === "plan" ? (
                  "Start Planning"
                ) : composerMode === "swarm" ? (
                  `Launch ${swarmConcurrency}x Swarm`
                ) : (
                  "Run Task"
                )}
              </Button>
            </div>
          )}
        </div>

        {error && (
          <div
            role="alert"
            className="mt-2 flex items-center justify-between gap-2 rounded-lg border border-rose-800/60 bg-rose-950/50 px-3 py-1.5 text-xs text-rose-300"
          >
            <span className="truncate">{error}</span>
            <button
              type="button"
              onClick={() => setError("")}
              className="shrink-0 text-rose-400 hover:text-rose-200"
            >
              ✕
            </button>
          </div>
        )}
      </div>

      {/* ================================================================== */}
      {/* 2. 2-Pane Minimalist Split Layout                                  */}
      {/* ================================================================== */}
      <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 lg:grid-cols-12">
        {/* ---------------------------------------------------------------- */}
        {/* Left Pane (lg:col-span-5, Master Feed)                           */}
        {/* ---------------------------------------------------------------- */}
        <div className="flex min-h-0 flex-col rounded-xl border border-zinc-800/90 bg-zinc-900/60 lg:col-span-5">
          {/* Segmented Filter Bar */}
          <div className="shrink-0 border-b border-zinc-800/80 p-2.5">
            <div className="grid grid-cols-3 gap-1 rounded-lg bg-zinc-950/80 p-1">
              <button
                type="button"
                onClick={() => setFeedTab("tasks")}
                className={`flex items-center justify-center gap-1.5 rounded-md py-1.5 text-xs font-medium transition-colors ${
                  feedTab === "tasks"
                    ? "bg-zinc-800 text-zinc-100 shadow-xs"
                    : "text-zinc-400 hover:text-zinc-200"
                }`}
              >
                <span>Tasks</span>
                <span className="tabular-nums text-[11px] text-zinc-500">
                  ({tasks.length})
                </span>
              </button>
              <button
                type="button"
                onClick={() => setFeedTab("plans")}
                className={`flex items-center justify-center gap-1.5 rounded-md py-1.5 text-xs font-medium transition-colors ${
                  feedTab === "plans"
                    ? "bg-zinc-800 text-zinc-100 shadow-xs"
                    : "text-zinc-400 hover:text-zinc-200"
                }`}
              >
                <span>Plans</span>
                <span className="tabular-nums text-[11px] text-zinc-500">
                  ({plans.length})
                </span>
              </button>
              <button
                type="button"
                onClick={() => setFeedTab("bugs")}
                className={`flex items-center justify-center gap-1.5 rounded-md py-1.5 text-xs font-medium transition-colors ${
                  feedTab === "bugs"
                    ? "bg-zinc-800 text-zinc-100 shadow-xs"
                    : "text-zinc-400 hover:text-zinc-200"
                }`}
              >
                <span>Bugs</span>
                <span className="tabular-nums text-[11px] text-zinc-500">
                  ({bugs.length})
                </span>
                {needsHumanCount > 0 && (
                  <span
                    className="h-1.5 w-1.5 rounded-full bg-rose-400"
                    title={`${needsHumanCount} declined_needs_human`}
                  />
                )}
              </button>
            </div>

            {/* Sub-filter chips for Tasks view */}
            {feedTab === "tasks" && (
              <div className="mt-2 flex items-center gap-1 overflow-x-auto">
                {TASK_SUB_FILTERS.map((f) => {
                  const active = taskSubFilter === f.key;
                  return (
                    <button
                      key={f.key}
                      type="button"
                      onClick={() => setTaskSubFilter(f.key)}
                      className={`inline-flex items-center gap-1 rounded-md px-2.5 py-1 text-[11px] font-medium transition-colors ${
                        active
                          ? "bg-indigo-500/15 text-indigo-300 ring-1 ring-indigo-500/40"
                          : "text-zinc-400 hover:bg-zinc-800/60 hover:text-zinc-200"
                      }`}
                    >
                      {f.label}
                      <span
                        className={`tabular-nums ${
                          active ? "text-indigo-400" : "text-zinc-600"
                        }`}
                      >
                        {taskCounts[f.key]}
                      </span>
                    </button>
                  );
                })}
              </div>
            )}
          </div>

          {/* Master Feed Scrollable List */}
          <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto p-2.5">
            {loadingFeed ? (
              <div className="flex items-center justify-center gap-2 py-12 text-xs text-zinc-500">
                <Spinner size={14} /> Loading workspace feed…
              </div>
            ) : feedTab === "tasks" ? (
              filteredTaskRoots.length === 0 ? (
                <EmptyFeedState
                  title="No matching tasks"
                  subtitle="Submit a task in the top bar or switch filters."
                />
              ) : (
                filteredTaskRoots.map((node) => (
                  <TaskFeedTreeItem
                    key={node.task.id}
                    node={node}
                    childrenOf={childrenOf}
                    selectedTaskId={selectedTaskId}
                    onSelectTask={(tid) => {
                      setSelected((cur) =>
                        cur?.kind === "task" && cur.id === tid
                          ? null
                          : { kind: "task", id: tid },
                      );
                    }}
                    onReload={reloadAll}
                    depth={0}
                  />
                ))
              )
            ) : feedTab === "plans" ? (
              plans.length === 0 ? (
                <EmptyFeedState
                  title="No planning sessions yet"
                  subtitle="Switch the composer to Plan to launch a multi-agent debate."
                />
              ) : (
                plans.map((session) => {
                  const isSel = selectedPlanId === session.id;
                  return (
                    <div
                      key={session.id}
                      onClick={() =>
                        setSelected((cur) =>
                          cur?.kind === "plan" && cur.id === session.id
                            ? null
                            : { kind: "plan", id: session.id },
                        )
                      }
                      className={`cursor-pointer rounded-lg border p-2.5 transition-colors ${
                        isSel
                          ? "border-indigo-500/60 bg-indigo-500/10"
                          : "border-zinc-800/80 bg-zinc-900/80 hover:bg-zinc-800/50"
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span
                          className="min-w-0 flex-1 truncate text-xs font-medium text-zinc-100"
                          title={session.title || session.prompt}
                        >
                          {session.title || session.prompt}
                        </span>
                        <Badge
                          text={session.status}
                          color={PLAN_STATUS_COLORS[session.status]}
                        />
                      </div>
                      <div className="mt-1.5 flex items-center justify-between text-[11px] text-zinc-500 tabular-nums">
                        <span>
                          turn {session.turn_number}/{session.max_rounds}
                        </span>
                        <span>{fmtRelative(session.created_at)}</span>
                      </div>
                    </div>
                  );
                })
              )
            ) : sortedBugs.length === 0 ? (
              <EmptyFeedState
                title="No bug candidates recorded"
                subtitle="Enable Autonomous Bug Sweep in the composer to hunt and fix bugs."
              />
            ) : (
              sortedBugs.map((bug) => {
                const needsHuman = bug.status === "declined_needs_human";
                const loc =
                  bug.file && bug.symbol
                    ? `${bug.file}::${bug.symbol}`
                    : bug.file ?? bug.symbol ?? "unknown";
                return (
                  <div
                    key={bug.id}
                    className={`rounded-lg border p-2.5 transition-colors ${
                      needsHuman
                        ? "border-rose-500/50 bg-rose-950/20"
                        : "border-zinc-800/80 bg-zinc-900/80"
                    }`}
                  >
                    <div className="flex flex-wrap items-center justify-between gap-1.5">
                      <div className="flex items-center gap-1.5">
                        <Badge
                          text={bug.status}
                          color={BUG_STATUS_COLORS[bug.status]}
                        />
                        {bug.severity && (
                          <Badge
                            text={bug.severity}
                            color={
                              BUG_SEVERITY_COLORS[bug.severity.toLowerCase()] ??
                              "bg-zinc-800 text-zinc-300 border border-zinc-700"
                            }
                          />
                        )}
                      </div>
                      <span className="text-[11px] text-zinc-500 tabular-nums">
                        {fmtRelative(bug.last_examined_at)}
                      </span>
                    </div>

                    <div
                      className="mt-1.5 truncate font-mono text-[11px] text-zinc-300"
                      title={loc}
                    >
                      {loc}
                    </div>

                    <p className="mt-1 line-clamp-2 text-xs text-zinc-400">
                      {bug.claim}
                    </p>

                    {bug.decline_reason && (
                      <p className="mt-1 line-clamp-2 text-[11px] text-rose-300/90">
                        Reason: {bug.decline_reason}
                      </p>
                    )}

                    {needsHuman && (
                      <div className="mt-2 flex items-center justify-end">
                        <Button
                          size="sm"
                          variant="primary"
                          onClick={() => void handleQueueBugFixTask(bug)}
                        >
                          Queue Fix Task
                        </Button>
                      </div>
                    )}
                  </div>
                );
              })
            )}
          </div>
        </div>

        {/* ---------------------------------------------------------------- */}
        {/* Right Pane (lg:col-span-7, Context Inspector & Live Stream)      */}
        {/* ---------------------------------------------------------------- */}
        <div className="flex min-h-0 flex-col rounded-xl border border-zinc-800/90 bg-zinc-900/60 lg:col-span-7">
          {selectedTask ? (
            /* ============================================================== */
            /* Selected Task Inspector                                        */
            /* ============================================================== */
            <div className="flex h-full min-h-0 flex-col">
              {/* Task Header */}
              <div className="shrink-0 border-b border-zinc-800/80 p-3">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <StatusDot state={selectedTask.state} />
                      <h2
                        className="min-w-0 truncate text-sm font-semibold text-zinc-100"
                        title={selectedTask.title || selectedTask.request}
                      >
                        {selectedTask.title || selectedTask.request}
                      </h2>
                      <Badge
                        text={selectedTask.state}
                        color={STATE_COLORS[selectedTask.state]}
                      />
                      {selectedTask.source_branch && (
                        <span
                          title={`PR Source Branch: ${selectedTask.source_branch}`}
                          className="rounded bg-indigo-950/80 px-2 py-0.5 font-mono text-[11px] text-indigo-300 ring-1 ring-indigo-800/70"
                        >
                          PR: {selectedTask.source_branch}
                        </span>
                      )}
                      {selectedTask.branch && (
                        <span className="rounded bg-zinc-800/90 px-2 py-0.5 font-mono text-[11px] text-zinc-300">
                          {selectedTask.branch}
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="flex flex-wrap items-center gap-1.5">
                    {(selectedTask.state === "inbox" ||
                      selectedTask.state === "failed" ||
                      selectedTask.state === "waiting_for_approval") && (
                      <Button
                        size="sm"
                        variant="primary"
                        disabled={taskActionBusy}
                        onClick={() =>
                          void handleInspectorTaskAction(() =>
                            api.approve(selectedTask.id),
                          )
                        }
                      >
                        {selectedTask.state === "waiting_for_approval"
                          ? "Approve & Resume"
                          : "Approve"}
                      </Button>
                    )}
                    {(selectedTask.state === "in_progress" ||
                      selectedTask.state === "waiting_for_approval" ||
                      selectedTask.state === "approved") && (
                      <Button
                        size="sm"
                        variant="default"
                        disabled={taskActionBusy}
                        onClick={() =>
                          void handleInspectorTaskAction(() =>
                            api.cancel(selectedTask.id),
                          )
                        }
                      >
                        Cancel
                      </Button>
                    )}
                    {selectedTask.state !== "in_progress" &&
                      selectedTask.state !== "waiting_for_approval" && (
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={taskActionBusy}
                          onClick={() =>
                            void handleInspectorTaskAction(() =>
                              api.deleteTask(selectedTask.id),
                            )
                          }
                        >
                          Delete
                        </Button>
                      )}
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={compactingTask}
                      onClick={() => void handleCompactTask()}
                      title="Record a deterministic summary of the task context (never changes the request or plan)"
                    >
                      {compactingTask ? (
                        <>
                          <Spinner size={12} /> Compacting…
                        </>
                      ) : (
                        "Compact Context"
                      )}
                    </Button>
                    {workflowGraph && inspectorTab !== "workflow" && (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => setShowMiniDag((v) => !v)}
                        title="Toggle live workflow Mini-DAG"
                      >
                        {showMiniDag ? "Hide DAG" : "Mini-DAG"}
                      </Button>
                    )}
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setSelected(null)}
                      title="Close task inspector and view project stream"
                    >
                      ✕
                    </Button>
                  </div>
                </div>

                {taskCompactBanner && (
                  <div className="mt-2 flex items-center justify-between gap-2 rounded-lg border border-emerald-800/60 bg-emerald-950/50 px-3 py-1.5 text-xs text-emerald-300">
                    <span className="truncate font-mono">{taskCompactBanner}</span>
                    <button
                      type="button"
                      onClick={() => setTaskCompactBanner(null)}
                      className="shrink-0 text-emerald-400 hover:text-emerald-200"
                    >
                      ✕
                    </button>
                  </div>
                )}

                {nodeBanner && (
                  <div className="mt-2 flex items-center justify-between gap-2 rounded-lg border border-sky-800/60 bg-sky-950/50 px-3 py-1.5 text-xs text-sky-200">
                    <span className="truncate font-mono">{nodeBanner}</span>
                    <button
                      type="button"
                      onClick={() => setNodeBanner(null)}
                      className="shrink-0 text-sky-400 hover:text-sky-200"
                    >
                      ✕
                    </button>
                  </div>
                )}

                {/* Inline Operator Steer Bar when task is in_progress or waiting_for_approval */}
                {(selectedTask.state === "in_progress" ||
                  selectedTask.state === "waiting_for_approval") && (
                  <form
                    onSubmit={(e) => void handleSteerSubmit(e)}
                    className="mt-2.5 flex items-center gap-2"
                  >
                    <input
                      type="text"
                      value={steerInput}
                      onChange={(e) => setSteerInput(e.target.value)}
                      placeholder="Operator Steer: send live guidance for the next developer attempt…"
                      className={`${input} text-xs`}
                    />
                    <Button
                      type="submit"
                      size="sm"
                      variant="primary"
                      disabled={steering || !steerInput.trim()}
                    >
                      {steering ? <Spinner size={12} /> : steerSent ? "Steered ✓" : "Steer"}
                    </Button>
                  </form>
                )}

                {/* Step-Level Checkpoint Pipeline Bar (clickable to focus node) */}
                <CheckpointPipelineBar
                  checkpoints={checkpoints}
                  liveNodeStatuses={effectiveNodeStatuses}
                  workflowGraph={workflowGraph}
                  verdicts={verdicts}
                  selectedNodeId={selectedWfNodeId}
                  onSelectNode={(nid) => {
                    setSelectedWfNodeId(nid);
                    setSelectedWfEdgeId(null);
                    setInspectorTab("workflow");
                  }}
                />

                {/* Collapsible Live Workflow Mini-DAG (when not on Workflow tab) */}
                {showMiniDag && workflowGraph && inspectorTab !== "workflow" && (
                  <div className="mt-2.5 h-52 overflow-hidden rounded-lg border border-zinc-800">
                    <WorkflowCanvas
                      graph={workflowGraph}
                      selectedNodeId={selectedWfNodeId}
                      selectedEdgeId={selectedWfEdgeId}
                      connectingFromId={null}
                      nodeStatuses={effectiveNodeStatuses}
                      armedHitlIds={hitlState.armed}
                      waitingHitlIds={hitlState.waiting}
                      onSelectNode={(nid) => {
                        setSelectedWfNodeId(nid);
                        if (nid !== null) setSelectedWfEdgeId(null);
                      }}
                      onSelectEdge={(eid) => {
                        setSelectedWfEdgeId(eid);
                        if (eid !== null) setSelectedWfNodeId(null);
                      }}
                      onMoveNode={() => {}}
                      onCompleteConnect={() => {}}
                    />
                  </div>
                )}

                {/* Inspector Sub-Tabs */}
                <div className="mt-3 flex items-center justify-between gap-2 border-t border-zinc-800/70 pt-2.5">
                  <div className="flex items-center gap-1">
                    {(
                      [
                        { key: "workflow", label: "Workflow & Agents" },
                        { key: "trace", label: "Live Trace (All Text)" },
                        { key: "verdicts", label: `Verdicts (${verdicts.length})` },
                        { key: "details", label: "Details & Usage" },
                      ] as const
                    ).map((tab) => {
                      const active = inspectorTab === tab.key;
                      return (
                        <button
                          key={tab.key}
                          type="button"
                          onClick={() => setInspectorTab(tab.key)}
                          className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                            active
                              ? "bg-indigo-500/20 text-indigo-300 ring-1 ring-indigo-500/40"
                              : "text-zinc-400 hover:bg-zinc-800/60 hover:text-zinc-200"
                          }`}
                        >
                          {tab.label}
                        </button>
                      );
                    })}
                  </div>

                  {inspectorTab === "trace" && (
                    <label className="flex cursor-pointer items-center gap-1.5 text-[11px] text-zinc-400 select-none">
                      <input
                        type="checkbox"
                        checked={showAllProjectEventsInTask}
                        onChange={(e) => setShowAllProjectEventsInTask(e.target.checked)}
                        className="h-3 w-3 rounded accent-indigo-500"
                      />
                      All project events
                    </label>
                  )}
                </div>
              </div>

              {/* Sub-Tab Body */}
              <div className="min-h-0 flex-1 overflow-y-auto p-3">
                {inspectorTab === "workflow" ? (
                  <TaskWorkflowSplitView
                    task={selectedTask}
                    graph={workflowGraph}
                    checkpoints={checkpoints}
                    verdicts={verdicts}
                    events={mergedTaskTrace}
                    nodeStatuses={effectiveNodeStatuses}
                    hitlState={hitlState}
                    selectedNodeId={selectedWfNodeId}
                    selectedEdgeId={selectedWfEdgeId}
                    onSelectNode={(nid) => {
                      setSelectedWfNodeId(nid);
                      if (nid !== null) setSelectedWfEdgeId(null);
                    }}
                    onSelectEdge={(eid) => {
                      setSelectedWfEdgeId(eid);
                      if (eid !== null) setSelectedWfNodeId(null);
                    }}
                    nodeInput={nodeInput}
                    onNodeInputChange={setNodeInput}
                    busy={nodeActionBusy}
                    onCompleteNode={async (targetNodeId, opts) => {
                      setNodeActionBusy(true);
                      setError("");
                      try {
                        const res = await api.completeNode(
                          selectedTask.id,
                          targetNodeId,
                          opts,
                        );
                        setHitlState(res.hitl);
                        setNodeInput("");
                        setNodeBanner(
                          `Node '${targetNodeId}' marked ${opts.status ?? "completed"}${
                            opts.verdict ? ` (${opts.verdict})` : ""
                          }.`,
                        );
                        await Promise.all([
                          reloadAll(),
                          loadSelectedTaskDetails(selectedTask.id),
                        ]);
                      } catch (err) {
                        setError(err instanceof Error ? err.message : String(err));
                      } finally {
                        setNodeActionBusy(false);
                      }
                    }}
                    onParticipate={async (targetNodeId, edgeId, msg) => {
                      if (!msg.trim()) return;
                      setNodeActionBusy(true);
                      setError("");
                      try {
                        await api.steer(selectedTask.id, msg.trim(), {
                          node_id: targetNodeId,
                          edge_id: edgeId,
                        });
                        setNodeInput("");
                        setNodeBanner(
                          `Sent guidance to ${targetNodeId ?? edgeId ?? "agent"} ✓`,
                        );
                        await loadSelectedTaskDetails(selectedTask.id);
                      } catch (err) {
                        setError(err instanceof Error ? err.message : String(err));
                      } finally {
                        setNodeActionBusy(false);
                      }
                    }}
                    onToggleHitl={async (targetId, enabled) => {
                      setNodeActionBusy(true);
                      setError("");
                      try {
                        const res = await api.toggleTaskHitl(
                          selectedTask.id,
                          targetId,
                          enabled,
                        );
                        setHitlState({ armed: res.armed, waiting: res.waiting });
                        setNodeBanner(
                          enabled
                            ? `Armed Human-in-the-Loop pause on '${targetId}'.`
                            : `Cleared Human-in-the-Loop pause on '${targetId}'.`,
                        );
                      } catch (err) {
                        setError(err instanceof Error ? err.message : String(err));
                      } finally {
                        setNodeActionBusy(false);
                      }
                    }}
                  />
                ) : inspectorTab === "trace" ? (
                  <LiveStreamTerminal
                    events={mergedTaskTrace}
                    status={streamStatus}
                    filter={streamFilter}
                    onFilterChange={setStreamFilter}
                    emptyHint="No trace events recorded for this task yet."
                  />
                ) : inspectorTab === "verdicts" ? (
                  verdicts.length === 0 ? (
                    <EmptyFeedState
                      title="No verdicts recorded yet"
                      subtitle={
                        selectedTask.state === "in_progress"
                          ? "Reviewers will emit grounded verdicts once the developer stage finishes."
                          : "No reviewer verdicts were recorded for this task."
                      }
                    />
                  ) : (
                    <div className="space-y-2">
                      {verdicts.map((v) => {
                        const vKey = v.verdict.toLowerCase();
                        const badgeColor =
                          vKey === "pass"
                            ? "bg-emerald-950/70 text-emerald-300 border border-emerald-800/70"
                            : vKey === "fail" || vKey === "block"
                              ? "bg-rose-950/70 text-rose-300 border border-rose-800/70"
                              : "bg-zinc-800 text-zinc-300 border border-zinc-700";
                        return (
                          <div
                            key={v.id}
                            className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-3"
                          >
                            <div className="flex items-center justify-between gap-2">
                              <div className="flex items-center gap-2">
                                <span className="text-xs font-semibold text-zinc-200">
                                  {v.agent}
                                </span>
                                <span className="text-[11px] text-zinc-500 tabular-nums">
                                  attempt #{v.attempt}
                                </span>
                              </div>
                              <div className="flex items-center gap-2">
                                <Badge
                                  text={`grounded: ${v.grounded_count}`}
                                  color="bg-zinc-900 text-zinc-400 border border-zinc-800"
                                />
                                <Badge text={v.verdict} color={badgeColor} />
                              </div>
                            </div>
                            {v.reason && (
                              <p
                                className={`mt-2 whitespace-pre-wrap break-words text-xs leading-relaxed ${
                                  VERDICT_COLORS[vKey] ?? "text-zinc-300"
                                }`}
                              >
                                {v.reason}
                              </p>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )
                ) : (
                  /* Details & Usage sub-tab */
                  <div className="space-y-4 text-xs">
                    <div className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-3">
                      <div className="text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
                        Task Request
                      </div>
                      <p className="mt-1.5 whitespace-pre-wrap break-words text-zinc-200 leading-relaxed">
                        {selectedTask.request}
                      </p>
                    </div>

                    {selectedTask.result_summary && (
                      <div className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-3">
                        <div className="text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
                          Result Summary
                        </div>
                        <p className="mt-1.5 whitespace-pre-wrap break-words text-zinc-300 leading-relaxed">
                          {selectedTask.result_summary}
                        </p>
                      </div>
                    )}

                    {usage && (
                      <div className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-3">
                        <div className="text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
                          Token & Turn Usage
                        </div>
                        <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-3">
                          <div className="rounded border border-zinc-800/80 bg-zinc-900/60 p-2">
                            <div className="text-[10px] text-zinc-500">Input Tokens</div>
                            <div className="mt-0.5 font-mono text-sm font-semibold text-zinc-200">
                              {fmtTokens(usage.input_tokens)}
                            </div>
                          </div>
                          <div className="rounded border border-zinc-800/80 bg-zinc-900/60 p-2">
                            <div className="text-[10px] text-zinc-500">Output Tokens</div>
                            <div className="mt-0.5 font-mono text-sm font-semibold text-zinc-200">
                              {fmtTokens(usage.output_tokens)}
                            </div>
                          </div>
                          <div className="rounded border border-zinc-800/80 bg-zinc-900/60 p-2">
                            <div className="text-[10px] text-zinc-500">Turns / Agents</div>
                            <div className="mt-0.5 font-mono text-sm font-semibold text-zinc-200">
                              {usage.total_turns}t · {usage.agent_count}a
                            </div>
                          </div>
                          <div className="rounded border border-zinc-800/80 bg-zinc-900/60 p-2">
                            <div className="text-[10px] text-zinc-500">Cache Read</div>
                            <div className="mt-0.5 font-mono text-sm text-zinc-300">
                              {fmtTokens(usage.cache_read_tokens)}
                            </div>
                          </div>
                          <div className="rounded border border-zinc-800/80 bg-zinc-900/60 p-2">
                            <div className="text-[10px] text-zinc-500">Cache Write</div>
                            <div className="mt-0.5 font-mono text-sm text-zinc-300">
                              {fmtTokens(usage.cache_creation_tokens)}
                            </div>
                          </div>
                        </div>

                        {usage.entries && usage.entries.length > 0 && (
                          <div className="mt-3 space-y-1 border-t border-zinc-800/80 pt-2">
                            <div className="text-[10px] uppercase tracking-wide text-zinc-500">
                              Agent Invocations
                            </div>
                            {usage.entries.map((entry, idx) => (
                              <div
                                key={`${entry.agent}-${idx}`}
                                className="flex flex-wrap items-center justify-between gap-2 rounded bg-zinc-900/50 px-2 py-1 text-[11px]"
                              >
                                <span className="font-medium text-zinc-300">
                                  {entry.agent}
                                  {entry.model_reported ? (
                                    <span className="ml-1.5 text-zinc-500">
                                      ({entry.model_reported})
                                    </span>
                                  ) : null}
                                </span>
                                <span className="font-mono text-zinc-400 tabular-nums">
                                  ↓{fmtTokens(entry.input_tokens ?? 0)} ↑
                                  {fmtTokens(entry.output_tokens ?? 0)} ·{" "}
                                  {entry.num_turns ?? 0}t
                                </span>
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    )}

                    {selectedTask.plan && Object.keys(selectedTask.plan).length > 0 && (
                      <div className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-3">
                        <div className="text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
                          Plan JSON
                        </div>
                        <pre className="mt-1.5 max-h-64 overflow-auto rounded bg-zinc-900/80 p-2 font-mono text-[11px] text-zinc-300">
                          {JSON.stringify(selectedTask.plan, null, 2)}
                        </pre>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          ) : selectedPlan ? (
            /* ============================================================== */
            /* Selected Planning Session Inspector                            */
            /* ============================================================== */
            <div className="flex h-full min-h-0 flex-col">
              {/* Header */}
              <div className="shrink-0 border-b border-zinc-800/80 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex min-w-0 items-center gap-2">
                    <h2
                      className="truncate text-sm font-semibold text-zinc-100"
                      title={selectedPlan.title || selectedPlan.prompt}
                    >
                      {selectedPlan.title || selectedPlan.prompt}
                    </h2>
                    <Badge
                      text={selectedPlan.status}
                      color={PLAN_STATUS_COLORS[selectedPlan.status]}
                    />
                    <span className="text-xs text-zinc-500 tabular-nums">
                      turn {selectedPlan.turn_number}/{selectedPlan.max_rounds}
                    </span>
                  </div>

                  <div className="flex flex-wrap items-center gap-2">
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={compactingPlan}
                      onClick={() => void handleCompactPlanningSession()}
                      title="Deterministically compact multi-agent planning debate messages"
                    >
                      {compactingPlan ? (
                        <>
                          <Spinner size={12} /> Compacting…
                        </>
                      ) : (
                        "Compact Session"
                      )}
                    </Button>
                    {(selectedPlan.status === "active" ||
                      selectedPlan.status === "stable") && (
                      <>
                        <Button
                          size="sm"
                          variant="primary"
                          disabled={approvingPlan || planNodes.length === 0}
                          onClick={() => void handleApprovePlan()}
                        >
                          {approvingPlan ? (
                            <>
                              <Spinner size={12} /> Materializing…
                            </>
                          ) : (
                            `Approve & Materialize Tasks (${planNodes.length})`
                          )}
                        </Button>
                        <Button
                          size="sm"
                          variant="danger"
                          disabled={cancellingPlan}
                          onClick={() => void handleCancelPlan()}
                        >
                          {cancellingPlan ? <Spinner size={12} /> : "Cancel"}
                        </Button>
                      </>
                    )}
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setSelected(null)}
                      title="Close planning session"
                    >
                      ✕
                    </Button>
                  </div>
                </div>

                {planCompactBanner && (
                  <div className="mt-2 flex items-center justify-between gap-2 rounded-lg border border-emerald-800/60 bg-emerald-950/50 px-3 py-1.5 text-xs text-emerald-300">
                    <span className="truncate font-mono">{planCompactBanner}</span>
                    <button
                      type="button"
                      onClick={() => setPlanCompactBanner(null)}
                      className="shrink-0 text-emerald-400 hover:text-emerald-200"
                    >
                      ✕
                    </button>
                  </div>
                )}
              </div>

              {/* Split Body: Discussion Transcript + Proposed Task Tree */}
              <div className="grid min-h-0 flex-1 grid-cols-1 divide-y divide-zinc-800/80 xl:grid-cols-12 xl:divide-x xl:divide-y-0">
                {/* Multi-agent discussion transcript */}
                <div className="flex min-h-0 flex-col xl:col-span-7">
                  <div className="min-h-0 flex-1 space-y-2.5 overflow-y-auto p-3">
                    {planMessages.length === 0 ? (
                      <EmptyFeedState
                        title="Agents are convening…"
                        subtitle="Planner, Architect, Tester, Security, and Senior Reviewer turns will stream here."
                      />
                    ) : (
                      planMessages.map((msg) => (
                        <div
                          key={msg.id}
                          className={`rounded-lg border p-2.5 text-xs ${
                            msg.role === "human"
                              ? "border-indigo-500/40 bg-indigo-950/30"
                              : "border-zinc-800 bg-zinc-950/50"
                          }`}
                        >
                          <div className="flex items-center justify-between gap-2 text-[11px]">
                            <span className="font-semibold text-indigo-300">
                              {msg.role === "human" ? "You (Operator)" : msg.agent}
                            </span>
                            <span className="text-zinc-500 tabular-nums">
                              turn {msg.turn_number} · {fmtRelative(msg.created_at)}
                            </span>
                          </div>
                          <p className="mt-1.5 whitespace-pre-wrap break-words text-zinc-200 leading-relaxed">
                            {msg.content}
                          </p>
                        </div>
                      ))
                    )}
                  </div>

                  {/* Human interjection input */}
                  {(selectedPlan.status === "active" ||
                    selectedPlan.status === "stable") && (
                    <div className="shrink-0 border-t border-zinc-800/80 p-2.5">
                      <div className="flex items-center gap-2">
                        <input
                          type="text"
                          value={humanNote}
                          onChange={(e) => setHumanNote(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Enter" && !e.shiftKey) {
                              e.preventDefault();
                              void handleSendPlanNote();
                            }
                          }}
                          placeholder="Interject in the planning debate… (Enter to send)"
                          className={`${input} text-xs`}
                        />
                        <Button
                          size="sm"
                          variant="primary"
                          disabled={sendingNote || !humanNote.trim()}
                          onClick={() => void handleSendPlanNote()}
                        >
                          {sendingNote ? <Spinner size={12} /> : "Send"}
                        </Button>
                      </div>
                    </div>
                  )}
                </div>

                {/* Proposed hierarchical PlanningTaskNode breakdown */}
                <div className="flex min-h-0 flex-col xl:col-span-5">
                  <div className="shrink-0 border-b border-zinc-800/80 px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
                    Proposed Task Tree ({planNodes.length})
                  </div>
                  <div className="min-h-0 flex-1 space-y-1.5 overflow-y-auto p-3">
                    {planNodes.length === 0 ? (
                      <EmptyFeedState
                        title="No task nodes yet"
                        subtitle="Nodes appear as the planning panel decomposes the goal."
                      />
                    ) : (
                      buildPlanNodeTree(planNodes).map((root) => (
                        <PlanTaskNodeItem key={root.id} node={root} depth={0} />
                      ))
                    )}
                  </div>
                </div>
              </div>
            </div>
          ) : (
            /* ============================================================== */
            /* Nothing Selected: Live Project-Wide Event Stream               */
            /* ============================================================== */
            <div className="flex h-full min-h-0 flex-col p-3">
              <LiveStreamTerminal
                events={coalescedProjectStream}
                status={streamStatus}
                filter={streamFilter}
                onFilterChange={setStreamFilter}
                emptyHint="Select a task or plan on the left, or watch live project events stream here."
              />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ============================================================================
// Step-Level Checkpoint Pipeline Bar
// ============================================================================

function CheckpointPipelineBar({
  checkpoints,
  liveNodeStatuses,
  workflowGraph,
  verdicts = [],
  selectedNodeId,
  onSelectNode,
}: {
  checkpoints: TaskCheckpoint[];
  liveNodeStatuses?: Record<string, TaskCheckpoint["status"]>;
  workflowGraph?: WorkflowGraph | null;
  verdicts?: Verdict[];
  selectedNodeId?: string | null;
  onSelectNode?: (nodeId: string) => void;
}) {
  const latestVerdictByNode = useMemo(() => {
    const map = new Map<string, string>();
    for (const v of verdicts) {
      map.set(v.agent, v.verdict.toLowerCase());
    }
    return map;
  }, [verdicts]);

  const steps = useMemo(() => {
    // Keep highest attempt per node_id
    const latestByNode = new Map<string, TaskCheckpoint>();
    for (const cp of checkpoints) {
      const existing = latestByNode.get(cp.node_id);
      if (
        !existing ||
        cp.attempt > existing.attempt ||
        (cp.attempt === existing.attempt && cp.updated_at >= existing.updated_at)
      ) {
        latestByNode.set(cp.node_id, cp);
      }
    }

    const enabledWfNodes =
      workflowGraph?.nodes.filter((n) => n.enabled !== false) ?? [];

    let orderedIds: string[] = [];

    if (enabledWfNodes.length > 0) {
      const hasDevNode = enabledWfNodes.some((n) => n.kind === "developer");
      const hasMergeNode = enabledWfNodes.some((n) => n.kind === "merge");
      const gateIds = new Set(
        enabledWfNodes.filter((n) => n.kind === "gate").map((n) => n.id),
      );
      const postGateIds = new Set(
        enabledWfNodes
          .filter(
            (n) =>
              n.kind !== "gate" &&
              n.kind !== "merge" &&
              (workflowGraph?.edges ?? []).some(
                (e) =>
                  gateIds.has(e.source) &&
                  e.target === n.id &&
                  e.condition !== "on_fail",
              ),
          )
          .map((n) => n.id),
      );

      const addUnique = (id: string) => {
        if (!orderedIds.includes(id)) orderedIds.push(id);
      };

      addUnique("baseline");
      for (const n of enabledWfNodes) {
        if (n.kind === "planner") addUnique(n.id);
      }
      if (hasDevNode) {
        for (const n of enabledWfNodes) {
          if (n.kind === "developer") addUnique(n.id);
        }
      }
      for (const n of enabledWfNodes) {
        if (
          (n.kind === "reviewer" || n.kind === "custom") &&
          !postGateIds.has(n.id)
        ) {
          addUnique(n.id);
        }
      }
      for (const n of enabledWfNodes) {
        if (n.kind === "gate") addUnique(n.id);
      }
      for (const n of enabledWfNodes) {
        if (postGateIds.has(n.id)) addUnique(n.id);
      }
      if (hasMergeNode) {
        for (const n of enabledWfNodes) {
          if (n.kind === "merge") addUnique(n.id);
        }
      }

      // Include any extra checkpoint nodes not covered above (excluding hidden synthetic ones)
      const hasExplicitReviewers = enabledWfNodes.some(
        (n) => n.kind === "reviewer",
      );
      for (const nodeId of latestByNode.keys()) {
        if (nodeId === "review" && hasExplicitReviewers) continue;
        if (nodeId === "developer" && !hasDevNode) continue;
        if (nodeId === "merge" && !hasMergeNode) continue;
        addUnique(nodeId);
      }
    } else {
      // Fallback when no workflow graph is loaded yet
      const customNodes: string[] = [];
      for (const nodeId of latestByNode.keys()) {
        if (!CANONICAL_PIPELINE_STEPS.includes(nodeId)) {
          customNodes.push(nodeId);
        }
      }
      if (liveNodeStatuses) {
        for (const nodeId of Object.keys(liveNodeStatuses)) {
          if (
            !CANONICAL_PIPELINE_STEPS.includes(nodeId) &&
            !customNodes.includes(nodeId)
          ) {
            customNodes.push(nodeId);
          }
        }
      }
      orderedIds = [
        "baseline",
        "planner",
        "developer",
        "review",
        ...customNodes,
        "gate",
        "merge",
      ];
    }

    return orderedIds.map((nodeId) => ({
      nodeId,
      checkpoint: latestByNode.get(nodeId) ?? null,
    }));
  }, [checkpoints, liveNodeStatuses, workflowGraph]);

  return (
    <div className="mt-2.5 flex items-center gap-1.5 overflow-x-auto pb-1">
      {steps.map(({ nodeId, checkpoint }, idx) => {
        const status =
          liveNodeStatuses?.[nodeId] ?? checkpoint?.status ?? "pending";
        const sha = checkpoint?.git_tree_sha?.slice(0, 7) ?? null;
        const isSelected = selectedNodeId === nodeId;
        const rawVerdict =
          latestVerdictByNode.get(nodeId) ??
          (typeof checkpoint?.artifact?.verdict === "string"
            ? checkpoint.artifact.verdict.toLowerCase()
            : null);

        let pillStyle = "border-zinc-800 bg-zinc-950/60 text-zinc-500";
        let icon: React.ReactNode = <span className="h-1.5 w-1.5 rounded-full bg-zinc-600" />;

        if (status === "completed") {
          pillStyle =
            "border-emerald-800/70 bg-emerald-950/60 text-emerald-300";
          icon = <span className="font-bold text-emerald-400">✓</span>;
        } else if (status === "waiting_for_approval") {
          pillStyle =
            "border-sky-500/80 bg-sky-950/80 text-sky-200 animate-pulse";
          icon = <span className="h-2 w-2 rounded-full bg-sky-400 animate-ping" />;
        } else if (status === "running") {
          pillStyle =
            "border-amber-600/70 bg-amber-950/70 text-amber-300 animate-pulse";
          icon = <span className="h-2 w-2 rounded-full bg-amber-400 animate-ping" />;
        } else if (status === "failed") {
          pillStyle = "border-rose-800/80 bg-rose-950/70 text-rose-300";
          icon = <span className="font-bold text-rose-400">✕</span>;
        } else if (status === "skipped") {
          pillStyle = "border-zinc-800 bg-zinc-900/40 text-zinc-500 line-through";
        }

        return (
          <div key={nodeId} className="flex shrink-0 items-center gap-1.5">
            {idx > 0 && <span className="text-[10px] text-zinc-600">→</span>}
            <button
              type="button"
              onClick={() => onSelectNode?.(nodeId)}
              className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] font-medium transition-all ${pillStyle} ${
                isSelected ? "ring-2 ring-indigo-400" : "hover:border-zinc-600"
              }`}
              title={
                checkpoint
                  ? `${nodeId}: ${status} (attempt #${checkpoint.attempt})${
                      rawVerdict ? ` · verdict: ${rawVerdict}` : ""
                    } — click to inspect`
                  : `${nodeId}: ${status} — click to inspect`
              }
            >
              {icon}
              <span>{nodeId}</span>
              {rawVerdict && status === "completed" && rawVerdict !== "pass" && (
                <span className="rounded bg-rose-950/90 px-1 py-0.2 font-mono text-[9px] text-rose-300 ring-1 ring-rose-800/70">
                  {rawVerdict}
                </span>
              )}
              {sha && (
                <span className="rounded bg-zinc-900/90 px-1 py-0.2 font-mono text-[10px] text-zinc-300">
                  {sha}
                </span>
              )}
            </button>
          </div>
        );
      })}
    </div>
  );
}

// ============================================================================
// Task Workflow Split View (Interactive Canvas + Node / HITL Connector Panel)
// ============================================================================

function TaskWorkflowSplitView({
  task,
  graph,
  checkpoints,
  verdicts,
  events,
  nodeStatuses,
  hitlState,
  selectedNodeId,
  selectedEdgeId,
  onSelectNode,
  onSelectEdge,
  nodeInput,
  onNodeInputChange,
  busy,
  onCompleteNode,
  onParticipate,
  onToggleHitl,
}: {
  task: Task;
  graph: WorkflowGraph | null;
  checkpoints: TaskCheckpoint[];
  verdicts: Verdict[];
  events: EventRow[];
  nodeStatuses: Record<string, TaskCheckpoint["status"]>;
  hitlState: { armed: string[]; waiting: string[] };
  selectedNodeId: string | null;
  selectedEdgeId: string | null;
  onSelectNode: (nodeId: string | null) => void;
  onSelectEdge: (edgeId: string | null) => void;
  nodeInput: string;
  onNodeInputChange: (val: string) => void;
  busy: boolean;
  onCompleteNode: (
    nodeId: string,
    opts: {
      status?: "completed" | "failed" | "skipped";
      message?: string;
      verdict?: string;
      output?: string;
      edge_id?: string | null;
    },
  ) => Promise<void>;
  onParticipate: (
    nodeId: string | null,
    edgeId: string | null,
    message: string,
  ) => Promise<void>;
  onToggleHitl: (targetId: string, enabled: boolean) => Promise<void>;
}) {
  // Determine which node is active if none was explicitly clicked yet
  const effectiveNodeId = useMemo(() => {
    if (selectedEdgeId) return null;
    if (selectedNodeId) return selectedNodeId;
    if (!graph) return "developer";
    const waitingNode = graph.nodes.find(
      (n) =>
        nodeStatuses[n.id] === "waiting_for_approval" ||
        hitlState.waiting.includes(n.id),
    );
    if (waitingNode) return waitingNode.id;
    const runningNode = graph.nodes.find((n) => nodeStatuses[n.id] === "running");
    if (runningNode) return runningNode.id;
    const failedNode = graph.nodes.find((n) => nodeStatuses[n.id] === "failed");
    if (failedNode) return failedNode.id;
    const devNode = graph.nodes.find((n) => n.kind === "developer");
    return devNode?.id ?? graph.nodes[0]?.id ?? "developer";
  }, [selectedNodeId, selectedEdgeId, graph, nodeStatuses, hitlState.waiting]);

  const activeNode = useMemo<WorkflowNode | null>(() => {
    if (!effectiveNodeId) return null;
    const found = graph?.nodes.find((n) => n.id === effectiveNodeId);
    if (found) return found;
    return {
      id: effectiveNodeId,
      label: effectiveNodeId,
      kind:
        effectiveNodeId === "planner"
          ? "planner"
          : effectiveNodeId === "developer"
            ? "developer"
            : effectiveNodeId === "gate"
              ? "gate"
              : effectiveNodeId === "merge"
                ? "merge"
                : "reviewer",
      agent:
        effectiveNodeId === "gate" ||
        effectiveNodeId === "merge" ||
        effectiveNodeId === "baseline"
          ? null
          : effectiveNodeId,
      output_contract: "text",
      prompt_template: null,
      triggers: [],
      optional: false,
      enabled: true,
      position_x: 0,
      position_y: 0,
    };
  }, [effectiveNodeId, graph]);

  const activeEdge = useMemo<WorkflowEdge | null>(() => {
    if (!selectedEdgeId || !graph) return null;
    return graph.edges.find((e) => e.id === selectedEdgeId) ?? null;
  }, [selectedEdgeId, graph]);

  const activeNodeCheckpoint = useMemo(() => {
    if (!activeNode) return null;
    const matching = checkpoints.filter((cp) => cp.node_id === activeNode.id);
    return matching.length > 0 ? matching[matching.length - 1] : null;
  }, [activeNode, checkpoints]);

  const activeNodeVerdicts = useMemo(() => {
    if (!activeNode) return [];
    const ag = activeNode.agent ?? activeNode.id;
    return verdicts.filter((v) => v.agent === ag || v.agent === activeNode.id);
  }, [activeNode, verdicts]);

  const nodeEvents = useMemo(() => {
    if (!activeNode) {
      if (activeEdge) {
        return events.filter(
          (ev) =>
            ev.payload?.edge_id === activeEdge.id ||
            ev.payload?.node_id === activeEdge.source ||
            ev.payload?.node_id === activeEdge.target ||
            ev.payload?.workflow_node_id === activeEdge.source ||
            ev.payload?.workflow_node_id === activeEdge.target,
        );
      }
      return events;
    }
    const nid = activeNode.id;
    const ag = activeNode.agent ?? activeNode.id;
    return events.filter((ev) => {
      const pNode = ev.payload?.node_id;
      const pWfNode = ev.payload?.workflow_node_id;
      if (pNode === nid || pWfNode === nid) return true;
      if (pNode === `review_${ag}` || pNode === `review_${nid}`) return true;
      if (ev.agent && (ev.agent === ag || ev.agent === nid)) return true;
      if (
        nid === "gate" &&
        (ev.type === "attempt.failed" && ev.payload?.stage === "gate")
      ) {
        return true;
      }
      if (
        nid === "merge" &&
        (ev.type === "task.committed" ||
          ev.type === "task.merged" ||
          ev.type === "task.done")
      ) {
        return true;
      }
      return false;
    });
  }, [activeNode, activeEdge, events]);

  const nodeStatus = activeNode
    ? (nodeStatuses[activeNode.id] ?? activeNodeCheckpoint?.status ?? "pending")
    : "pending";

  const isNodeHitlArmed = Boolean(
    activeNode &&
      (activeNode.pause_after ||
        activeNode.await_human_approval ||
        hitlState.armed.includes(activeNode.id)),
  );
  const isNodeWaiting = Boolean(
    activeNode &&
      (nodeStatus === "waiting_for_approval" ||
        hitlState.waiting.includes(activeNode.id)),
  );

  const isEdgeHitlArmed = Boolean(
    activeEdge &&
      (activeEdge.require_human || hitlState.armed.includes(activeEdge.id)),
  );
  const isEdgeWaiting = Boolean(
    activeEdge &&
      (hitlState.waiting.includes(activeEdge.id) ||
        (hitlState.waiting.includes(activeEdge.source) && isEdgeHitlArmed)),
  );

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      {/* 1. Top Half: Interactive Workflow Canvas */}
      {graph ? (
        <div className="relative h-60 shrink-0 overflow-hidden rounded-xl border border-zinc-800/90 sm:h-68">
          <WorkflowCanvas
            graph={graph}
            selectedNodeId={effectiveNodeId}
            selectedEdgeId={selectedEdgeId}
            connectingFromId={null}
            nodeStatuses={nodeStatuses}
            armedHitlIds={hitlState.armed}
            waitingHitlIds={hitlState.waiting}
            onSelectNode={onSelectNode}
            onSelectEdge={onSelectEdge}
            onMoveNode={() => {}}
            onCompleteConnect={() => {}}
          />
          <div className="pointer-events-none absolute top-2.5 right-2.5 rounded-lg border border-zinc-800 bg-zinc-900/90 px-2.5 py-1 text-[11px] text-zinc-400 backdrop-blur-xs">
            Click any <strong className="text-zinc-200">node</strong> or{" "}
            <strong className="text-sky-300">HITL connector</strong> to inspect,
            participate, or complete
          </div>
        </div>
      ) : null}

      {/* 2. Bottom Half: Selected Node or HITL Connector Live Panel */}
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-zinc-800/90 bg-zinc-950/80">
        {activeEdge ? (
          /* ================================================================ */
          /* Selected HITL Connector (Edge) Panel                             */
          /* ================================================================ */
          <div className="flex h-full min-h-0 flex-col">
            <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 bg-zinc-900/70 px-3 py-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="rounded border border-sky-700/70 bg-sky-950/80 px-2 py-0.5 font-mono text-[11px] font-semibold text-sky-300">
                  Connector: {activeEdge.source} → {activeEdge.target}
                </span>
                <Badge
                  text={`condition: ${activeEdge.condition}`}
                  color="bg-zinc-800 text-zinc-300 border border-zinc-700"
                />
                {isEdgeWaiting && (
                  <Badge
                    text="⏸ Waiting for Human Approval"
                    color="bg-sky-950/90 text-sky-200 border border-sky-500/80"
                  />
                )}
              </div>

              <div className="flex flex-wrap items-center gap-1.5">
                <Button
                  size="sm"
                  variant={isEdgeHitlArmed ? "primary" : "default"}
                  disabled={busy}
                  onClick={() =>
                    void onToggleHitl(activeEdge.id, !isEdgeHitlArmed)
                  }
                  title="Pause execution on this connector until you approve or inject guidance"
                >
                  {isEdgeHitlArmed ? "👤 HITL Armed ✓" : "👤 Arm HITL Pause"}
                </Button>
                <Button
                  size="sm"
                  variant="primary"
                  disabled={busy}
                  onClick={() =>
                    void onCompleteNode(activeEdge.source, {
                      status: "completed",
                      message: nodeInput.trim() || undefined,
                      edge_id: activeEdge.id,
                    })
                  }
                >
                  ✓ Approve & Continue to {activeEdge.target}
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  disabled={busy}
                  onClick={() =>
                    void onCompleteNode(activeEdge.source, {
                      status: "failed",
                      message:
                        nodeInput.trim() ||
                        `Rejected at connector ${activeEdge.source} -> ${activeEdge.target}`,
                      edge_id: activeEdge.id,
                    })
                  }
                >
                  ✕ Reject / Retry
                </Button>
              </div>
            </div>

            <div className="min-h-0 flex-1 space-y-2 overflow-y-auto p-3 font-mono text-xs">
              <div className="rounded-lg border border-zinc-800 bg-zinc-900/50 p-2.5 font-sans text-xs text-zinc-300">
                <div className="font-semibold text-zinc-100">
                  Human-in-the-Loop Transition ({activeEdge.source} →{" "}
                  {activeEdge.target})
                </div>
                <p className="mt-1 text-zinc-400">
                  {isEdgeWaiting
                    ? `Execution is paused on this connector. Review the output from '${activeEdge.source}' below, optionally add guidance for '${activeEdge.target}', and click Approve & Continue.`
                    : `Click 'Arm HITL Pause' to pause the worker at this connector, or send live guidance to '${activeEdge.target}' below.`}
                </p>
              </div>
              {nodeEvents.length === 0 ? (
                <div className="py-6 text-center font-sans text-xs text-zinc-500">
                  No events recorded on {activeEdge.source} → {activeEdge.target}{" "}
                  yet.
                </div>
              ) : (
                nodeEvents.map((ev) => (
                  <div
                    key={ev.id}
                    className="flex items-start gap-2 rounded px-1.5 py-0.5 hover:bg-zinc-900/70"
                  >
                    <span className="shrink-0 text-zinc-600 tabular-nums">
                      {fmtTime(ev.ts)}
                    </span>
                    <span className="w-32 shrink-0 truncate font-semibold text-indigo-300/90">
                      {ev.type}
                    </span>
                    {ev.agent && (
                      <span className="shrink-0 text-zinc-500">
                        [{ev.agent}]
                      </span>
                    )}
                    <span
                      className={`min-w-0 flex-1 whitespace-pre-wrap break-words ${eventColorClass(
                        ev,
                      )}`}
                    >
                      {summarizeEvent(ev)}
                    </span>
                  </div>
                ))
              )}
            </div>

            {/* Connector Participation Input */}
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void onParticipate(
                  activeEdge.target,
                  activeEdge.id,
                  nodeInput,
                );
              }}
              className="flex shrink-0 items-center gap-2 border-t border-zinc-800/80 bg-zinc-900/60 p-2.5"
            >
              <input
                type="text"
                value={nodeInput}
                onChange={(e) => onNodeInputChange(e.target.value)}
                placeholder={`Participate on ${activeEdge.source} → ${activeEdge.target}: inject instructions for ${activeEdge.target}…`}
                className={`${input} text-xs`}
              />
              <Button
                type="submit"
                size="sm"
                variant="default"
                disabled={busy || !nodeInput.trim()}
              >
                Steer {activeEdge.target}
              </Button>
            </form>
          </div>
        ) : activeNode ? (
          /* ================================================================ */
          /* Selected Workflow Node Live Execution & Participation Panel      */
          /* ================================================================ */
          <div className="flex h-full min-h-0 flex-col">
            {/* Node Header + Complete/Override Actions */}
            <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 bg-zinc-900/70 px-3 py-2">
              <div className="flex flex-wrap items-center gap-2">
                <span
                  className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider ${
                    (NODE_KIND_COLORS[activeNode.kind] ?? NODE_KIND_COLORS.custom)
                      .badge
                  }`}
                >
                  {activeNode.kind}
                </span>
                <span className="text-xs font-semibold text-zinc-100">
                  {activeNode.label || activeNode.id}
                </span>
                {activeNode.agent && (
                  <span className="rounded bg-zinc-800 px-1.5 py-0.5 font-mono text-[11px] text-indigo-300">
                    @{activeNode.agent}
                  </span>
                )}
                <Badge
                  text={nodeStatus}
                  color={
                    nodeStatus === "completed"
                      ? "bg-emerald-950/80 text-emerald-300 border border-emerald-800/70"
                      : nodeStatus === "waiting_for_approval"
                        ? "bg-sky-950/90 text-sky-200 border border-sky-500/80"
                        : nodeStatus === "running"
                          ? "bg-amber-950/80 text-amber-300 border border-amber-700/70"
                          : nodeStatus === "failed"
                            ? "bg-rose-950/80 text-rose-300 border border-rose-800/70"
                            : "bg-zinc-800 text-zinc-400 border border-zinc-700"
                  }
                />
                {activeNodeCheckpoint && (
                  <span className="text-[11px] text-zinc-500 tabular-nums">
                    attempt #{activeNodeCheckpoint.attempt}
                    {activeNodeCheckpoint.git_tree_sha
                      ? ` · ${activeNodeCheckpoint.git_tree_sha.slice(0, 7)}`
                      : ""}
                  </span>
                )}
              </div>

              <div className="flex flex-wrap items-center gap-1.5">
                <Button
                  size="sm"
                  variant={isNodeHitlArmed ? "primary" : "default"}
                  disabled={busy}
                  onClick={() =>
                    void onToggleHitl(activeNode.id, !isNodeHitlArmed)
                  }
                  title="Pause worker for human approval after this node finishes"
                >
                  {isNodeHitlArmed ? "👤 HITL Pause On" : "👤 Pause After"}
                </Button>
                <Button
                  size="sm"
                  variant={isNodeWaiting ? "primary" : "default"}
                  disabled={busy}
                  onClick={() =>
                    void onCompleteNode(activeNode.id, {
                      status: "completed",
                      verdict:
                        activeNode.kind === "reviewer" ? "pass" : undefined,
                      message: nodeInput.trim() || undefined,
                      output: nodeInput.trim() || undefined,
                    })
                  }
                  title="Approve waiting node or manually mark this stage as completed/pass"
                >
                  {isNodeWaiting ? "✓ Approve & Continue" : "✓ Complete / Pass"}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={busy}
                  onClick={() =>
                    void onCompleteNode(activeNode.id, {
                      status: "skipped",
                      message: nodeInput.trim() || undefined,
                    })
                  }
                  title="Skip this node"
                >
                  Skip
                </Button>
                <Button
                  size="sm"
                  variant="danger"
                  disabled={busy}
                  onClick={() =>
                    void onCompleteNode(activeNode.id, {
                      status: "failed",
                      verdict:
                        activeNode.kind === "reviewer" ? "fail" : undefined,
                      message:
                        nodeInput.trim() ||
                        `Operator marked ${activeNode.id} failed`,
                    })
                  }
                  title="Mark this node as failed and trigger retry feedback"
                >
                  ✕ Fail
                </Button>
              </div>
            </div>

            {/* Scrollable Body: Full Agent Output/Report + Live Execution Stream */}
            <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3 text-xs">
              {(typeof activeNodeCheckpoint?.artifact?.output === "string" &&
                activeNodeCheckpoint.artifact.output.trim()) ||
              (typeof activeNodeCheckpoint?.artifact?.plan_preamble ===
                "string" &&
                activeNodeCheckpoint.artifact.plan_preamble.trim()) ||
              activeNodeVerdicts.length > 0 ||
              typeof activeNodeCheckpoint?.artifact?.diff_stat === "string" ? (
                <div className="rounded-lg border border-zinc-800 bg-zinc-900/70 p-3">
                  <div className="mb-2 flex flex-wrap items-center justify-between gap-2 border-b border-zinc-800/80 pb-1.5">
                    <span className="text-[11px] font-semibold tracking-wide text-zinc-300 uppercase">
                      {activeNode.label || activeNode.id} Output & Report
                    </span>
                    <div className="flex items-center gap-2">
                      {typeof activeNodeCheckpoint?.artifact?.model ===
                        "string" && (
                        <span className="font-mono text-[11px] text-indigo-300">
                          {activeNodeCheckpoint.artifact.model}
                        </span>
                      )}
                      {typeof activeNodeCheckpoint?.artifact?.diff_stat ===
                        "string" && (
                        <span className="font-mono text-[11px] text-zinc-400">
                          {activeNodeCheckpoint.artifact.diff_stat}
                        </span>
                      )}
                      {activeNodeVerdicts.length > 0 && (
                        <Badge
                          text={`verdict: ${
                            activeNodeVerdicts[activeNodeVerdicts.length - 1]
                              .verdict
                          }`}
                          color={
                            activeNodeVerdicts[
                              activeNodeVerdicts.length - 1
                            ].verdict.toLowerCase() === "pass"
                              ? "bg-emerald-950/70 text-emerald-300 border border-emerald-800/70"
                              : "bg-rose-950/70 text-rose-300 border border-rose-800/70"
                          }
                        />
                      )}
                    </div>
                  </div>
                  <div className="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-zinc-200">
                    {String(
                      activeNodeCheckpoint?.artifact?.output ||
                        activeNodeCheckpoint?.artifact?.plan_preamble ||
                        activeNodeVerdicts[activeNodeVerdicts.length - 1]
                          ?.reason ||
                        "",
                    )}
                  </div>
                </div>
              ) : null}

              <div className="space-y-1 font-mono text-xs">
                {nodeEvents.length === 0 &&
                !activeNodeCheckpoint?.artifact?.output &&
                !activeNodeCheckpoint?.artifact?.plan_preamble ? (
                  <div className="flex h-full flex-col items-center justify-center py-8 text-center font-sans text-xs text-zinc-500">
                    <div>
                      No execution events for{" "}
                      <strong className="text-zinc-300">
                        {activeNode.label || activeNode.id}
                      </strong>{" "}
                      yet (task state: {task.state}).
                    </div>
                    <div className="mt-1 text-[11px] text-zinc-600">
                      You can pre-steer this agent below, arm a HITL pause, or
                      click another node on the workflow graph above.
                    </div>
                  </div>
                ) : (
                  nodeEvents.map((ev) => (
                    <div
                      key={ev.id}
                      className="flex items-start gap-2 rounded px-1.5 py-0.5 hover:bg-zinc-900/70"
                    >
                      <span className="shrink-0 text-zinc-600 tabular-nums">
                        {fmtTime(ev.ts)}
                      </span>
                      <span className="w-32 shrink-0 truncate font-semibold text-indigo-300/90">
                        {ev.type}
                      </span>
                      {ev.agent && (
                        <span className="shrink-0 text-zinc-500">
                          [{ev.agent}]
                        </span>
                      )}
                      <span
                        className={`min-w-0 flex-1 whitespace-pre-wrap break-words ${eventColorClass(
                          ev,
                        )}`}
                      >
                        {summarizeEvent(ev)}
                      </span>
                    </div>
                  ))
                )}
              </div>
            </div>

            {/* Participate Bar (Scoped to this Node) */}
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void onParticipate(activeNode.id, null, nodeInput);
              }}
              className="flex shrink-0 items-center gap-2 border-t border-zinc-800/80 bg-zinc-900/60 p-2.5"
            >
              <input
                type="text"
                value={nodeInput}
                onChange={(e) => onNodeInputChange(e.target.value)}
                placeholder={`Participate in ${
                  activeNode.label || activeNode.id
                }: send live instructions or completion notes…`}
                className={`${input} text-xs`}
              />
              <Button
                type="submit"
                size="sm"
                variant="primary"
                disabled={busy || !nodeInput.trim()}
              >
                {busy ? <Spinner size={12} /> : `Steer ${activeNode.id}`}
              </Button>
            </form>
          </div>
        ) : null}
      </div>
    </div>
  );
}

// ============================================================================
// Task Feed Tree Row
// ============================================================================

interface TaskTreeNode {
  task: Task;
  children: TaskTreeNode[];
}

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
        new Date(a.task.created_at).getTime() - new Date(b.task.created_at).getTime(),
    );
    childrenOf.set(id, n.children);
  }
  return { roots, childrenOf };
}

function TaskFeedTreeItem({
  node,
  childrenOf,
  selectedTaskId,
  onSelectTask,
  onReload,
  depth,
}: {
  node: TaskTreeNode;
  childrenOf: Map<string, TaskTreeNode[]>;
  selectedTaskId: string | null;
  onSelectTask: (id: string) => void;
  onReload: () => Promise<void>;
  depth: number;
}) {
  const task = node.task;
  const children = childrenOf.get(task.id) ?? node.children;
  const hasChildren = children.length > 0;
  const [expanded, setExpanded] = useState(true);
  const [busy, setBusy] = useState(false);

  const isSelected = selectedTaskId === task.id;
  const indent = Math.min(depth * INDENT_STEP, INDENT_MAX);

  const runAction = async (e: React.MouseEvent, fn: () => Promise<unknown>) => {
    e.stopPropagation();
    if (busy) return;
    setBusy(true);
    try {
      await fn();
      await onReload();
    } finally {
      setBusy(false);
    }
  };

  const canApprove =
    task.state === "inbox" ||
    task.state === "failed" ||
    task.state === "waiting_for_approval";
  const canCancel =
    task.state === "in_progress" ||
    task.state === "waiting_for_approval" ||
    task.state === "approved";
  const canDelete =
    task.state !== "in_progress" && task.state !== "waiting_for_approval";

  return (
    <div>
      <div
        onClick={() => onSelectTask(task.id)}
        style={depth > 0 ? { marginLeft: indent } : undefined}
        className={`cursor-pointer rounded-lg border p-2.5 transition-colors ${
          isSelected
            ? "border-indigo-500/60 bg-indigo-500/10"
            : "border-zinc-800/80 bg-zinc-900/80 hover:bg-zinc-800/50"
        } ${depth > 0 ? "border-l-2 border-l-indigo-500/30" : ""}`}
      >
        <div className="flex items-center gap-2">
          {hasChildren && (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                setExpanded((v) => !v);
              }}
              className="text-xs text-zinc-500 hover:text-zinc-300"
              title={expanded ? "Collapse subtasks" : "Expand subtasks"}
            >
              {expanded ? "▾" : "▸"}
            </button>
          )}
          <StatusDot state={task.state} />
          <span
            className="min-w-0 flex-1 truncate text-xs font-medium text-zinc-100"
            title={task.title || task.request}
          >
            {task.title || task.request}
          </span>
          <Badge text={task.state} color={STATE_COLORS[task.state]} />
          <span className="shrink-0 text-[11px] text-zinc-500 tabular-nums">
            {fmtRelative(task.created_at)}
          </span>
        </div>

        {/* Quick Inline Actions */}
        <div className="mt-2 flex flex-wrap items-center justify-end gap-1.5">
          {canApprove && (
            <Button
              size="sm"
              variant="primary"
              disabled={busy}
              onClick={(e) => void runAction(e, () => api.approve(task.id))}
            >
              Approve
            </Button>
          )}
          {hasChildren && canApprove && (
            <Button
              size="sm"
              variant="default"
              disabled={busy}
              onClick={(e) => void runAction(e, () => api.cascadeApprove(task.id))}
            >
              Approve All
            </Button>
          )}
          {canCancel && (
            <Button
              size="sm"
              variant="default"
              disabled={busy}
              onClick={(e) => void runAction(e, () => api.cancel(task.id))}
            >
              Cancel
            </Button>
          )}
          {canDelete && (
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={(e) => void runAction(e, () => api.deleteTask(task.id))}
            >
              Delete
            </Button>
          )}
        </div>
      </div>

      {expanded &&
        children.map((child) => (
          <div key={child.task.id} className="mt-1">
            <TaskFeedTreeItem
              node={child}
              childrenOf={childrenOf}
              selectedTaskId={selectedTaskId}
              onSelectTask={onSelectTask}
              onReload={onReload}
              depth={depth + 1}
            />
          </div>
        ))}
    </div>
  );
}

// ============================================================================
// Planning Task Node Tree
// ============================================================================

interface PlanTreeNode extends PlanningTaskNode {
  children: PlanTreeNode[];
}

function buildPlanNodeTree(nodes: PlanningTaskNode[]): PlanTreeNode[] {
  const ordered = [...nodes].sort(
    (a, b) => a.level - b.level || a.sort_order - b.sort_order,
  );
  const map = new Map<string, PlanTreeNode>();
  const roots: PlanTreeNode[] = [];
  for (const n of ordered) map.set(n.id, { ...n, children: [] });
  for (const n of ordered) {
    const item = map.get(n.id)!;
    if (n.parent_id && map.has(n.parent_id)) {
      map.get(n.parent_id)!.children.push(item);
    } else {
      roots.push(item);
    }
  }
  return roots;
}

function PlanTaskNodeItem({ node, depth }: { node: PlanTreeNode; depth: number }) {
  return (
    <div>
      <div
        style={depth > 0 ? { marginLeft: Math.min(depth * 12, 36) } : undefined}
        className="rounded-lg border border-zinc-800/80 bg-zinc-950/50 p-2"
      >
        <div className="flex items-center justify-between gap-1.5">
          <span className="truncate text-xs font-medium text-zinc-200" title={node.title}>
            {node.title}
          </span>
          <Badge text={node.status} color={PLAN_NODE_COLORS[node.status]} />
        </div>
        {node.description && (
          <p className="mt-1 line-clamp-2 text-[11px] text-zinc-400">
            {node.description}
          </p>
        )}
      </div>
      {node.children.map((child) => (
        <div key={child.id} className="mt-1">
          <PlanTaskNodeItem node={child} depth={depth + 1} />
        </div>
      ))}
    </div>
  );
}

// ============================================================================
// Live Stream & Trace Terminal (coalesces agent.output_chunk)
// ============================================================================

function coalesceOutputChunks(events: EventRow[]): EventRow[] {
  const result: EventRow[] = [];
  for (const ev of events) {
    const prev = result[result.length - 1];
    if (
      ev.type === "agent.output_chunk" &&
      prev &&
      prev.type === "agent.output_chunk" &&
      prev.agent === ev.agent &&
      prev.task_id === ev.task_id
    ) {
      const prevText = String(prev.payload?.text ?? prev.payload?.chunk ?? "");
      const nextText = String(ev.payload?.text ?? ev.payload?.chunk ?? "");
      result[result.length - 1] = {
        ...prev,
        id: ev.id,
        ts: ev.ts,
        payload: {
          ...prev.payload,
          text: prevText + (prevText && nextText ? "\n" : "") + nextText,
        },
      };
    } else {
      result.push(ev);
    }
  }
  return result;
}

function summarizeEvent(ev: EventRow): string {
  const p = ev.payload ?? {};
  switch (ev.type) {
    case "agent.dispatched":
      return `dispatched ${String(p.profile ?? ev.agent ?? "agent")}${
        p.model ? ` (${String(p.model)})` : ""
      }`;
    case "pipeline.derived":
      return `pipeline: ${(p.pipeline as string[] | undefined)?.join(" → ") || "(none)"}`;
    case "agent.verdict":
      return `${String(p.verdict ?? "verdict")}${
        p.reason ? ` — ${String(p.reason)}` : ""
      }`;
    case "agent.output_chunk":
      return String(p.text ?? p.chunk ?? "");
    case "attempt.started":
      return `attempt #${String(p.n ?? "?")} started`;
    case "attempt.failed":
      return `attempt #${String(p.n ?? "?")} failed at ${String(p.stage ?? "?")}${
        p.error ? ` — ${String(p.error)}` : ""
      }`;
    case "node.started":
      return `node ${String(p.node_id ?? "?")} started`;
    case "node.waiting_for_approval":
    case "hitl.waiting":
      return `paused at ${String(p.edge_id ?? p.node_id ?? "?")} — awaiting human approval`;
    case "hitl.resolved":
      return `human resolved ${String(p.edge_id ?? p.node_id ?? "?")} → ${String(p.status ?? "completed")}`;
    case "node.completed":
      return `node ${String(p.node_id ?? "?")} ${String(p.status ?? "completed")}`;
    case "task.created":
      return `created ${p.title ? `"${String(p.title)}"` : ""}`;
    case "task.done":
      return `done (merged=${String(p.merged ?? false)})`;
    case "task.failed":
      return `failed: ${String(p.reason ?? "unknown")}`;
    case "operator.steer":
      return `steer: ${String(p.message ?? p.text ?? "")}`;
    default:
      return Object.keys(p).length > 0 ? JSON.stringify(p) : ev.type;
  }
}

function eventColorClass(ev: EventRow): string {
  if (ev.type === "agent.verdict") {
    const v = String(ev.payload?.verdict ?? "").toLowerCase();
    if (v === "pass") return "text-emerald-400";
    if (v === "fail" || v === "block") return "text-rose-400";
    return "text-amber-400";
  }
  if (ev.type.endsWith(".failed") || ev.type.endsWith(".error")) {
    return "text-rose-400";
  }
  if (ev.type === "task.done" || ev.type === "task.merged") {
    return "text-emerald-400";
  }
  if (ev.type === "pipeline.derived") {
    return "text-violet-300";
  }
  if (ev.type === "agent.dispatched" || ev.type.startsWith("attempt.")) {
    return "text-sky-300";
  }
  if (ev.type === "operator.steer") {
    return "text-amber-300";
  }
  return "text-zinc-300";
}

function LiveStreamTerminal({
  events,
  status,
  filter,
  onFilterChange,
  emptyHint,
}: {
  events: EventRow[];
  status: StreamStatus;
  filter: string;
  onFilterChange: (v: string) => void;
  emptyHint: string;
}) {
  const endRef = useRef<HTMLDivElement>(null);

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    const list = q
      ? events.filter(
          (e) =>
            e.type.toLowerCase().includes(q) ||
            (e.agent ?? "").toLowerCase().includes(q) ||
            summarizeEvent(e).toLowerCase().includes(q),
        )
      : events;
    return list.slice(-MAX_STREAM_ROWS);
  }, [events, filter]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [filtered.length]);

  const isLive = status === "live";

  return (
    <div className="flex h-full min-h-0 flex-col gap-2">
      {/* Stream Status + Filter Bar */}
      <div className="flex shrink-0 items-center justify-between gap-2">
        <span
          className={`inline-flex items-center gap-1.5 text-xs font-medium ${
            isLive ? "text-emerald-400" : "text-amber-400"
          }`}
        >
          <span
            className={`h-2 w-2 rounded-full ${
              isLive ? "bg-emerald-400" : "bg-amber-400 animate-pulse"
            }`}
          />
          {isLive ? "Connected" : "Reconnecting"}
        </span>

        <input
          type="text"
          value={filter}
          onChange={(e) => onFilterChange(e.target.value)}
          placeholder="Filter stream by event, agent, or text…"
          className={`${input} max-w-xs py-1 text-xs`}
        />
      </div>

      {/* Monospace Terminal Feed */}
      <div className="min-h-0 flex-1 space-y-1 overflow-y-auto rounded-lg border border-zinc-800 bg-zinc-950 p-2.5 font-mono text-xs">
        {filtered.length === 0 ? (
          <div className="flex h-full items-center justify-center py-12 text-center font-sans text-xs text-zinc-500">
            {emptyHint}
          </div>
        ) : (
          filtered.map((ev) => (
            <div
              key={ev.id}
              className="flex items-start gap-2 rounded px-1.5 py-0.5 hover:bg-zinc-900/70"
            >
              <span className="shrink-0 text-zinc-600 tabular-nums">
                {fmtTime(ev.ts)}
              </span>
              <span className="w-36 shrink-0 truncate font-semibold text-indigo-300/90">
                {ev.type}
              </span>
              {ev.agent && (
                <span className="shrink-0 text-zinc-500">[{ev.agent}]</span>
              )}
              <span
                className={`min-w-0 flex-1 whitespace-pre-wrap break-words ${eventColorClass(
                  ev,
                )}`}
              >
                {summarizeEvent(ev)}
              </span>
            </div>
          ))
        )}
        <div ref={endRef} />
      </div>
    </div>
  );
}

// ============================================================================
// Empty State Helper
// ============================================================================

function EmptyFeedState({ title, subtitle }: { title: string; subtitle: string }) {
  return (
    <div className="rounded-xl border border-dashed border-zinc-800 bg-zinc-950/30 p-8 text-center">
      <div className="text-xs font-medium text-zinc-400">{title}</div>
      <div className="mt-1 text-[11px] text-zinc-600">{subtitle}</div>
    </div>
  );
}
