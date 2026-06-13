export interface Project {
  id: string;
  name: string;
  path: string;
  default_branch: string;
  mode: string;
  created_at: string;
}

export interface Task {
  id: string;
  project_id: string;
  title: string;
  request: string;
  state: string;
  level: number | null;
  branch: string | null;
  result_summary: string | null;
  created_at: string;
  updated_at: string;
}

export interface EventRow {
  id: number;
  project_id: string | null;
  task_id: string | null;
  agent: string | null;
  type: string;
  payload: Record<string, unknown>;
  ts: string;
}

export interface Verdict {
  id: string;
  task_id: string;
  attempt: number;
  agent: string;
  verdict: string;
  reason: string;
  grounded_count: number;
}

export interface EngineProfile {
  id: string;
  project_id: string | null;
  name: string;
  arg_mode: string;
  base_url: string | null;
  model: string | null;
  subagent_model: string | null;
  effort: string | null;
  auth_secret_id: string | null;
}

export interface ProfileTestResult {
  ok: boolean;
  model_reported: string | null;
  latency_ms: number | null;
  cost_usd: number | null;
  error: string | null;
}

export interface Quarantine {
  id: string;
  pattern: string;
  reason: string;
  until: string;
}

export interface Integrity {
  total: number;
  active: number;
  expired: number;
  expired_patterns: string[];
  healthy: boolean;
}
