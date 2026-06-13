import type {
  EngineProfile,
  EventRow,
  Integrity,
  ProfileTestResult,
  Project,
  Quarantine,
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

export const api = {
  listProjects: () => req<Project[]>("GET", "/api/projects"),
  createProject: (b: NewProject) => req<Project>("POST", "/api/projects", b),
  detachProject: (id: string) => req<unknown>("DELETE", `/api/projects/${id}`),
  pause: (id: string) => req<unknown>("POST", `/api/projects/${id}/pause`),
  resume: (id: string) => req<unknown>("POST", `/api/projects/${id}/resume`),
  reonboard: (id: string) => req<unknown>("POST", `/api/projects/${id}/onboard`),
  getConfig: (id: string) => req<Record<string, unknown>>("GET", `/api/projects/${id}/config`),
  patchConfig: (id: string, config: unknown) =>
    req<unknown>("PATCH", `/api/projects/${id}/config`, { config }),
  usage: (id: string) =>
    req<{ calls: number; total_cost_usd: number }>("GET", `/api/projects/${id}/usage`),

  listTasks: (id: string, state?: string) =>
    req<Task[]>("GET", `/api/projects/${id}/tasks${state ? `?state=${state}` : ""}`),
  createTask: (id: string, b: NewTask) => req<Task>("POST", `/api/projects/${id}/tasks`, b),
  approve: (tid: string) => req<unknown>("POST", `/api/tasks/${tid}/approve`),
  cancel: (tid: string) => req<unknown>("POST", `/api/tasks/${tid}/cancel`),
  taskEvents: (tid: string) => req<EventRow[]>("GET", `/api/tasks/${tid}/events`),
  taskVerdicts: (tid: string) => req<Verdict[]>("GET", `/api/tasks/${tid}/verdicts`),

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
};
