import type {
  AgentPersona,
  BugCandidate,
  EventRow,
  Integrity,
  InteractionLog,
  ModelCatalog,
  ObservabilityStats,
  PlanningMessage,
  PlanningSession,
  PlanningTaskNode,
  Project,
  ProjectMode,
  ProjectStatus,
  Quarantine,
  SqlQueryResult,
  StoreItem,
  StoreItemKind,
  SwarmBatchResponse,
  Task,
  TaskCheckpoint,
  Verdict,
  WorkflowEnvelope,
  WorkflowGraph,
} from "./types";

async function req<T>(
  method: string,
  url: string,
  body?: unknown,
  idempotencyKey?: string,
): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (idempotencyKey) {
    headers["Idempotency-Key"] = idempotencyKey;
  }
  const res = await fetch(url, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail: unknown = res.statusText;
    try {
      detail = (await res.json()).detail;
    } catch {
      /* ignore */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export interface NewProject {
  name: string;
  path: string;
  default_branch: string;
  workflow_template?: string | null;
  config_preset?: string | null;
  max_parallel_tasks?: number | null;
}

export interface NewTask {
  request: string;
  title: string;
  use_planner: boolean | null;
  auto_approve: boolean;
  source_branch?: string | null;
}

export interface NewPlanningSession {
  title: string;
  prompt: string;
  max_rounds?: number;
}

export interface ListTasksOptions {
  state?: string;
  limit?: number;
  offset?: number;
}

/** Build a `?a=1&b=2` query string, skipping undefined/empty values. */
function qs(params: Record<string, string | number | undefined>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== "") sp.append(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

export const api = {
  listProjects: () => req<Project[]>("GET", "/api/projects"),
  createProject: (b: NewProject, idempotencyKey?: string) =>
    req<Project>("POST", "/api/projects", b, idempotencyKey),
  detachProject: (id: string) => req<unknown>("DELETE", `/api/projects/${id}`),
  pause: (id: string) => req<{ paused: boolean }>("POST", `/api/projects/${id}/pause`),
  resume: (id: string) => req<{ running: boolean }>("POST", `/api/projects/${id}/resume`),
  projectStatus: (id: string) =>
    req<ProjectStatus>("GET", `/api/projects/${id}/status`),
  getConfig: (id: string) => req<Record<string, unknown>>("GET", `/api/projects/${id}/config`),
  patchConfig: (id: string, config: unknown, idempotencyKey?: string) =>
    req<unknown>("PATCH", `/api/projects/${id}/config`, { config }, idempotencyKey),
  configSchema: () => req<Record<string, unknown>>("GET", "/api/config/schema"),
  mcpInfo: () =>
    req<{
      mcp_url: string;
      tool_count: number;
      tools: string[];
      opencode_remote_json: Record<string, unknown>;
      opencode_stdio_json: Record<string, unknown>;
      claude_code_http_cli: string;
      claude_code_stdio_cli: string;
      claude_mcp_json: Record<string, unknown>;
    }>("GET", "/api/mcp/info"),
  setMode: (id: string, mode: ProjectMode) =>
    req<Project>("POST", `/api/projects/${id}/mode`, { mode }),

  // Workflow Graph & Agent Studio
  getWorkflow: (id: string) =>
    req<WorkflowEnvelope>("GET", `/api/projects/${id}/workflow`),
  putWorkflow: (
    id: string,
    payload: { workflow?: WorkflowGraph | null; template_id?: string | null },
    idempotencyKey?: string,
  ) =>
    req<WorkflowEnvelope>(
      "PUT",
      `/api/projects/${id}/workflow`,
      payload,
      idempotencyKey,
    ),
  listAgents: (projectId?: string) =>
    req<AgentPersona[]>(
      "GET",
      `/api/agents${qs({ project_id: projectId, limit: 200 })}`,
    ),
  upsertAgent: (
    name: string,
    b: { role: string; persona_md: string; project_id?: string | null },
    idempotencyKey?: string,
  ) =>
    req<AgentPersona>(
      "PUT",
      `/api/agents/${encodeURIComponent(name)}`,
      b,
      idempotencyKey,
    ),
  deleteAgent: (name: string, projectId?: string) =>
    req<{ deleted?: string; reset_to_default?: string }>(
      "DELETE",
      `/api/agents/${encodeURIComponent(name)}${qs({ project_id: projectId })}`,
    ),

  // Bug-Fixer ledger
  bugCandidates: (id: string) =>
    req<BugCandidate[]>("GET", `/api/projects/${id}/bug-candidates`),
  needsHuman: (id: string) => req<BugCandidate[]>("GET", `/api/projects/${id}/needs-human`),

  listTasks: (id: string, opts?: string | ListTasksOptions) => {
    const o: ListTasksOptions = typeof opts === "string" ? { state: opts } : opts ?? {};
    return req<Task[]>(
      "GET",
      `/api/projects/${id}/tasks${qs({ state: o.state, limit: o.limit, offset: o.offset })}`,
    );
  },
  createTask: (id: string, b: NewTask, idempotencyKey?: string) =>
    req<Task>("POST", `/api/projects/${id}/tasks`, b, idempotencyKey),
  approve: (tid: string, idempotencyKey?: string) =>
    req<unknown>("POST", `/api/tasks/${tid}/approve`, undefined, idempotencyKey),
  cancel: (tid: string) => req<unknown>("POST", `/api/tasks/${tid}/cancel`),
  steer: (
    tid: string,
    message: string,
    opts?: { node_id?: string | null; edge_id?: string | null },
  ) =>
    req<{ steered: boolean; node_id?: string | null; edge_id?: string | null }>(
      "POST",
      `/api/tasks/${tid}/steer`,
      {
        message,
        node_id: opts?.node_id ?? undefined,
        edge_id: opts?.edge_id ?? undefined,
      },
    ),
  completeNode: (
    tid: string,
    nodeId: string,
    b: {
      status?: "completed" | "failed" | "skipped";
      message?: string;
      verdict?: string;
      output?: string;
      edge_id?: string | null;
      attempt?: number;
    },
  ) =>
    req<{
      ok: boolean;
      signaled: boolean;
      checkpoint: TaskCheckpoint;
      hitl: { armed: string[]; waiting: string[] };
    }>(
      "POST",
      `/api/tasks/${encodeURIComponent(tid)}/nodes/${encodeURIComponent(nodeId)}/complete`,
      b,
    ),
  getTaskHitl: (tid: string) =>
    req<{ task_id: string; armed: string[]; waiting: string[] }>(
      "GET",
      `/api/tasks/${encodeURIComponent(tid)}/hitl`,
    ),
  toggleTaskHitl: (tid: string, targetId: string, enabled: boolean) =>
    req<{ task_id: string; armed: string[]; waiting: string[] }>(
      "POST",
      `/api/tasks/${encodeURIComponent(tid)}/hitl`,
      { target_id: targetId, enabled },
    ),
  deleteTask: (tid: string) => req<{ deleted: string }>("DELETE", `/api/tasks/${tid}`),
  cascadeApprove: (tid: string) =>
    req<{ approved: boolean; cascade: boolean; task_ids: string[]; count: number }>(
      "POST",
      `/api/tasks/${tid}/cascade-approve`,
    ),
  taskEvents: (tid: string) => req<EventRow[]>("GET", `/api/tasks/${tid}/events`),
  taskVerdicts: (tid: string) => req<Verdict[]>("GET", `/api/tasks/${tid}/verdicts`),
  taskCheckpoints: (tid: string) =>
    req<TaskCheckpoint[]>("GET", `/api/tasks/${tid}/checkpoints`),
  taskUsage: (tid: string) =>
    req<{
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
    }>("GET", `/api/tasks/${tid}/usage`),

  listQuarantine: (id: string) => req<Quarantine[]>("GET", `/api/projects/${id}/quarantine`),
  quarantineIntegrity: (id: string) =>
    req<Integrity>("GET", `/api/projects/${id}/quarantine/integrity`),
  addQuarantine: (id: string, b: { pattern: string; reason: string; until: string }) =>
    req<Quarantine>("POST", `/api/projects/${id}/quarantine`, b),
  delQuarantine: (qid: string) => req<unknown>("DELETE", `/api/quarantine/${qid}`),

  // Agent-ception planning sessions
  listPlanningSessions: (pid: string) =>
    req<PlanningSession[]>("GET", `/api/projects/${pid}/planning/sessions`),
  createPlanningSession: (pid: string, b: NewPlanningSession, idempotencyKey?: string) =>
    req<PlanningSession>(
      "POST",
      `/api/projects/${pid}/planning/sessions`,
      b,
      idempotencyKey,
    ),
  getPlanningSession: (sid: string) =>
    req<PlanningSession>("GET", `/api/planning/sessions/${sid}`),
  listPlanningMessages: (sid: string) =>
    req<PlanningMessage[]>("GET", `/api/planning/sessions/${sid}/messages`),
  addPlanningMessage: (sid: string, content: string) =>
    req<PlanningMessage>("POST", `/api/planning/sessions/${sid}/messages`, { content }),
  listPlanningTaskNodes: (sid: string) =>
    req<PlanningTaskNode[]>("GET", `/api/planning/sessions/${sid}/tasks`),
  approvePlanningSession: (sid: string) =>
    req<{ approved: boolean; task_ids: string[]; count: number }>(
      "POST",
      `/api/planning/sessions/${sid}/approve`,
    ),
  cancelPlanningSession: (sid: string) =>
    req<{ cancelled: boolean }>("POST", `/api/planning/sessions/${sid}/cancel`),

  // Global Store (Workflows, Agents, Config Presets)
  listStore: (kind?: StoreItemKind) =>
    req<StoreItem[]>("GET", `/api/store${qs({ kind, limit: 200 })}`),
  publishStoreItem: (
    b: {
      kind: StoreItemKind;
      name: string;
      description?: string;
      tags?: string[];
      payload: Record<string, unknown>;
    },
    idempotencyKey?: string,
  ) => req<StoreItem>("POST", "/api/store", b, idempotencyKey),
  deleteStoreItem: (itemId: string) =>
    req<{ deleted: string }>("DELETE", `/api/store/${encodeURIComponent(itemId)}`),
  installStoreItem: (
    projectId: string,
    b: { item_id?: string; kind?: StoreItemKind; name?: string },
  ) =>
    req<{ installed: string; kind: string }>(
      "POST",
      `/api/projects/${projectId}/store/install`,
      b,
    ),

  // Parallel Swarms
  createSwarm: (
    projectId: string,
    b: {
      units?: { title?: string; request: string; use_planner?: boolean }[];
      prompt?: string;
      concurrency?: number;
      auto_approve?: boolean;
    },
    idempotencyKey?: string,
  ) =>
    req<SwarmBatchResponse>(
      "POST",
      `/api/projects/${projectId}/swarms`,
      b,
      idempotencyKey,
    ),
  swarmStatus: (projectId: string, swarmId: string) =>
    req<{
      swarm_id: string;
      project_id: string;
      total: number;
      counts: Record<string, number>;
      tasks: Task[];
    }>("GET", `/api/projects/${projectId}/swarms/${encodeURIComponent(swarmId)}`),

  // Model & Skill Catalog
  listModels: () => req<ModelCatalog>("GET", "/api/models"),

  // Context Compaction
  compactTask: (taskId: string) =>
    req<{
      task_id: string;
      chars_before: number;
      chars_after: number;
      tokens_saved_est: number;
      compacted: boolean;
    }>("POST", `/api/tasks/${encodeURIComponent(taskId)}/compact`),
  compactPlanningSession: (sessionId: string) =>
    req<{
      session_id: string;
      messages_before: number;
      chars_before: number;
      chars_after: number;
      tokens_saved_est: number;
    }>("POST", `/api/planning/sessions/${encodeURIComponent(sessionId)}/compact`),

  // Observability & Interaction Logs (SQLite + INTERACTION_LOGS.parquet)
  observabilityStats: (projectId?: string) =>
    req<ObservabilityStats>(
      "GET",
      `/api/observability/stats${qs({ project_id: projectId })}`,
    ),
  observabilitySessions: (opts?: {
    project_id?: string;
    task_id?: string;
    agent?: string;
    model?: string;
    search?: string;
    limit?: number;
    offset?: number;
  }) =>
    req<InteractionLog[]>(
      "GET",
      `/api/observability/sessions${qs(opts ?? {})}`,
    ),
  observabilitySession: (sessionId: string) =>
    req<InteractionLog>(
      "GET",
      `/api/observability/sessions/${encodeURIComponent(sessionId)}`,
    ),
  observabilityQuery: (sql: string, limit = 200) =>
    req<SqlQueryResult>("POST", "/api/observability/query", { sql, limit }),
  exportParquet: () =>
    req<{ ok: boolean; path: string; rows: number; size_bytes: number }>(
      "POST",
      "/api/observability/export-parquet",
    ),
  reloadStatus: () =>
    req<{
      reload_count: number;
      reload_pending: boolean;
      quiescent: boolean;
      changed_files: string[];
      frontend_stale: boolean;
      discoverable_modules: number;
      version: string;
    }>("GET", "/api/system/reload-status"),
  reloadSystem: (force = false) =>
    req<{
      reloaded: boolean;
      deferred: boolean;
      reload_pending: boolean;
      quiescent: boolean;
      reload_count: number;
      modules_reloaded: number;
      frontend_rebuilt: boolean;
      version: string;
    }>("POST", "/api/system/reload", { force }),
  evolveWorkflow: (projectId: string) =>
    req<Record<string, unknown>>(
      "POST",
      `/api/projects/${encodeURIComponent(projectId)}/workflow/evolve`,
    ),
};
