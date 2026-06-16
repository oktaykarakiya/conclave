export interface Project {
  id: string;
  name: string;
  path: string;
  default_branch: string;
  mode: string;
  created_at: string;
  config?: { execution?: { target_branch?: string } };
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
  planning_session_id: string | null;
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
