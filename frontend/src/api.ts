import type {
  EngineProfile,
  EventRow,
  Integrity,
  PlanningMessage,
  PlanningSession,
  PlanningTaskNode,
  ProfileTestResult,
  Project,
  Quarantine,
  RepoKnowledge,
  Task,
  Verdict,
} from "./types";

async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
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
}

export interface NewTask {
  request: string;
  title: string;
  use_planner: boolean | null;
  auto_approve: boolean;
}

export interface ProfileBody {
  name: string;
  project_id: string | null;
  arg_mode: string;
  base_url: string | null;
  model: string | null;
  subagent_model: string | null;
  effort: string | null;
  auth_token: string | null;
  extra_env: Record<string, string>;
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
  createProject: (b: NewProject) => req<Project>("POST", "/api/projects", b),
  detachProject: (id: string) => req<unknown>("DELETE", `/api/projects/${id}`),
  pause: (id: string) => req<unknown>("POST", `/api/projects/${id}/pause`),
  resume: (id: string) => req<unknown>("POST", `/api/projects/${id}/resume`),
  reonboard: (id: string) => req<unknown>("POST", `/api/projects/${id}/onboard`),
  getKnowledge: (id: string) => req<RepoKnowledge>("GET", `/api/projects/${id}/knowledge`),
  aiAnalyze: (id: string) => req<RepoKnowledge>("POST", `/api/projects/${id}/ai-analyze`),
  getConfig: (id: string) => req<Record<string, unknown>>("GET", `/api/projects/${id}/config`),
  patchConfig: (id: string, config: unknown) =>
    req<unknown>("PATCH", `/api/projects/${id}/config`, { config }),
  usage: (id: string) =>
    req<{ calls: number; total_cost_usd: number }>("GET", `/api/projects/${id}/usage`),

  listTasks: (id: string, opts?: string | ListTasksOptions) => {
    const o: ListTasksOptions = typeof opts === "string" ? { state: opts } : opts ?? {};
    return req<Task[]>(
      "GET",
      `/api/projects/${id}/tasks${qs({ state: o.state, limit: o.limit, offset: o.offset })}`,
    );
  },
  createTask: (id: string, b: NewTask) => req<Task>("POST", `/api/projects/${id}/tasks`, b),
  approve: (tid: string) => req<unknown>("POST", `/api/tasks/${tid}/approve`),
  cancel: (tid: string) => req<unknown>("POST", `/api/tasks/${tid}/cancel`),
  cascadeApprove: (tid: string) =>
    req<{ approved: boolean; cascade: boolean; task_ids: string[]; count: number }>(
      "POST",
      `/api/tasks/${tid}/cascade-approve`,
    ),
  taskEvents: (tid: string) => req<EventRow[]>("GET", `/api/tasks/${tid}/events`),
  taskVerdicts: (tid: string) => req<Verdict[]>("GET", `/api/tasks/${tid}/verdicts`),
  taskUsage: (tid: string) =>
    req<{
      task_id: string;
      entries: {
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

  listProfiles: () => req<EngineProfile[]>("GET", "/api/profiles"),
  saveProfile: (b: ProfileBody) => req<EngineProfile>("POST", "/api/profiles", b),
  testProfile: (b: ProfileBody) => req<ProfileTestResult>("POST", "/api/profiles/test", b),
  deleteProfile: (id: string) => req<unknown>("DELETE", `/api/profiles/${id}`),

  listQuarantine: (id: string) => req<Quarantine[]>("GET", `/api/projects/${id}/quarantine`),
  quarantineIntegrity: (id: string) =>
    req<Integrity>("GET", `/api/projects/${id}/quarantine/integrity`),
  addQuarantine: (id: string, b: { pattern: string; reason: string; until: string }) =>
    req<Quarantine>("POST", `/api/projects/${id}/quarantine`, b),
  delQuarantine: (qid: string) => req<unknown>("DELETE", `/api/quarantine/${qid}`),

  // Agent-ception planning sessions
  listPlanningSessions: (pid: string) =>
    req<PlanningSession[]>("GET", `/api/projects/${pid}/planning/sessions`),
  createPlanningSession: (pid: string, b: NewPlanningSession) =>
    req<PlanningSession>("POST", `/api/projects/${pid}/planning/sessions`, b),
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
};
