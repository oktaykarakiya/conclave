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
  parent_task_id: string | null;
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

export interface RepoKnowledge {
  languages: string[];
  frameworks: string[];
  commands: Record<string, string>;
  architecture_summary: string;
  conventions: string[];
  layout: Record<string, string[]>;
  protected_globs: string[];
  ai_enriched: boolean;
}

export interface PlanningSession {
  id: string;
  project_id: string;
  title: string;
  prompt: string;
  status: "active" | "stable" | "completed" | "cancelled";
  turn_number: number;
  max_rounds: number;
  created_at: string;
  completed_at: string | null;
}

export interface PlanningMessage {
  id: string;
  session_id: string;
  agent: string;
  role: "agent" | "human";
  content: string;
  turn_number: number;
  parent_id: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface PlanningTaskNode {
  id: string;
  session_id: string;
  parent_id: string | null;
  title: string;
  description: string;
  status: "proposed" | "refined" | "approved";
  level: number;
  sort_order: number;
  task_id: string | null;
  created_at: string;
  updated_at: string;
}
