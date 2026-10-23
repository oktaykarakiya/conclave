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

export interface ProjectStatus {
  project_id: string;
  paused: boolean;
  mode: ProjectMode;
}

/**
 * A row in the Bug-Fixer ledger — one suspected bug tracked through the 7-state
 * `BugStatus` machine (mirrors the backend `BugCandidate` model).
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
  source_branch?: string | null;
  result_summary: string | null;
  parent_task_id: string | null;
  swarm_id?: string | null;
  plan?: Record<string, unknown> | null;
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

// --- Dynamic Workflow Graph & Agent Studio types ----------------------------

export type NodeKind =
  | "planner"
  | "developer"
  | "reviewer"
  | "gate"
  | "merge"
  | "custom";

export type EdgeCondition =
  | "always"
  | "on_pass"
  | "on_fail"
  | "on_diff_trigger";

export type OutputContract = "text" | "json" | "verdict" | "plan" | "diff";

export interface WorkflowNode {
  id: string;
  label: string;
  kind: NodeKind;
  agent: string | null;
  output_contract: OutputContract;
  prompt_template: string | null;
  triggers: string[];
  optional: boolean;
  enabled: boolean;
  position_x: number;
  position_y: number;
  model?: string | null;
  effort?: string | null;
  cli_agent?: string | null;
  skills?: string[];
  thinking?: boolean | null;
  compact_before_start?: boolean | null;
  pause_after?: boolean;
  await_human_approval?: boolean;
}

export interface WorkflowEdge {
  id: string;
  source: string;
  target: string;
  condition: EdgeCondition;
  max_iterations: number;
  label: string;
  require_human?: boolean;
}

export interface WorkflowGraph {
  version: number;
  name: string;
  description: string;
  template_id: string | null;
  parallel_reviewers: boolean;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
}

export interface WorkflowEnvelope {
  workflow: WorkflowGraph;
  is_custom: boolean;
  templates: Record<string, WorkflowGraph>;
}

export interface AgentPersona {
  id: string;
  project_id: string | null;
  name: string;
  role: string;
  persona_md: string;
  created_at: string;
}

export interface TaskCheckpoint {
  task_id: string;
  node_id: string;
  attempt: number;
  status: "running" | "waiting_for_approval" | "completed" | "failed" | "skipped";
  git_tree_sha: string | null;
  artifact: Record<string, unknown>;
  updated_at: string;
}

export type StoreItemKind = "workflow" | "agent" | "config_preset";

export interface StoreItem {
  id: string;
  kind: StoreItemKind;
  name: string;
  description: string;
  tags: string[];
  payload: Record<string, unknown>;
  built_in: boolean;
  created_at: string;
  updated_at: string;
}

export interface SwarmBatchResponse {
  swarm_id: string;
  project_id: string;
  concurrency: number;
  count: number;
  tasks: Task[];
}

export interface ModelCatalog {
  default_model: string;
  models: { id: string; label?: string; provider?: string; tier?: string }[];
  efforts: string[];
  cli_agents: string[];
  skill_templates: string[];
}

export interface InteractionMessage {
  role: string;
  time?: string;
  model?: string;
  tokens?: string;
  text: string;
  tools?: { name: string; count?: number; input?: string }[];
}

export interface InteractionLog {
  session_id: string;
  title: string;
  directory: string;
  path: string;
  slug: string;
  model: string;
  agent: string;
  cost: number;
  tokens_input: number;
  tokens_output: number;
  tokens_reasoning: number;
  tokens_cache_read: number;
  tokens_cache_write: number;
  time_created: number;
  time_updated: number;
  created_at_str: string;
  updated_at_str: string;
  duration: number;
  tool_calls_count: number;
  message_count: number;
  user_prompt: string;
  tool_breakdown: Record<string, number>;
  messages: InteractionMessage[];
  project_id: string | null;
  task_id: string | null;
  node_id: string | null;
  source: string;
}

export interface ObservabilityStats {
  total_sessions: number;
  total_cost: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_reasoning_tokens: number;
  total_cache_read_tokens: number;
  total_cache_write_tokens: number;
  total_tool_calls: number;
  total_messages: number;
  avg_duration: number;
  models: { model: string; count: number; cost: number; tokens_input?: number; tokens_output?: number }[];
  agents: { agent: string; count: number; cost: number; tool_calls?: number }[];
}

export interface SqlQueryResult {
  columns: string[];
  rows: Record<string, unknown>[];
  row_count: number;
}
