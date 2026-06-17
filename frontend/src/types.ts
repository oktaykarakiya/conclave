/** The two project execution modes (mirrors the backend `ProjectMode` enum). */
export type ProjectMode = "task_queue" | "autonomous_bug_fixer";

export interface Project {
  id: string;
  name: string;
  path: string;
  default_branch: string;
  mode: string;
  created_at: string;
  config?: { execution?: { target_branch?: string } };
}

/**
 * A row in the Bug-Fixer ledger — one suspected bug tracked through the 7-state
 * `BugStatus` machine (mirrors the backend `BugCandidate` model). Only the
 * fields the UI renders are typed here.
 */
export type BugStatus =
  | "discovered"
  | "reproduced"
  | "fixing"
  | "fixed"
  | "dismissed_false_positive"
  | "declined_needs_human"
  | "deferred";

export interface BugCandidate {
  id: string;
  project_id: string;
  fingerprint: string;
  file: string | null;
  symbol: string | null;
  region: string | null;
  claim: string;
  severity: string | null;
  status: BugStatus;
  attempts: number;
  decline_reason: string | null;
  task_id: string | null;
  notes: string | null;
  discovered_at: string;
  last_examined_at: string;
  fixed_at: string | null;
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
