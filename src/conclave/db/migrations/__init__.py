"""Ordered schema migrations, applied on daemon startup.

Each migration bumps the version and is recorded in ``schema_version``. Migrations
are append-only — never edit a shipped migration; add a new one.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


_SQL_001 = """
CREATE TABLE projects (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  path            TEXT NOT NULL,
  default_branch  TEXT NOT NULL,
  mode            TEXT NOT NULL DEFAULT 'task_queue',
  config_json     TEXT NOT NULL DEFAULT '{}',
  created_at      TEXT NOT NULL
);

CREATE TABLE secrets (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  value       TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE TABLE engine_profiles (
  id              TEXT PRIMARY KEY,
  project_id      TEXT,
  name            TEXT NOT NULL,
  arg_mode        TEXT NOT NULL DEFAULT 'inherit',
  base_url        TEXT,
  model           TEXT,
  subagent_model  TEXT,
  effort          TEXT,
  auth_secret_id  TEXT,
  extra_env_json  TEXT NOT NULL DEFAULT '{}',
  created_at      TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
  FOREIGN KEY(auth_secret_id) REFERENCES secrets(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX idx_engine_profiles_scope_name
  ON engine_profiles(IFNULL(project_id, ''), name);

CREATE TABLE agents (
  id          TEXT PRIMARY KEY,
  project_id  TEXT,
  name        TEXT NOT NULL,
  role        TEXT NOT NULL,
  persona_md  TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX idx_agents_scope_name ON agents(IFNULL(project_id, ''), name);

CREATE TABLE tasks (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL,
  title           TEXT NOT NULL DEFAULT '',
  request         TEXT NOT NULL,
  level           INTEGER,
  state           TEXT NOT NULL DEFAULT 'inbox',
  use_planner     INTEGER,
  plan_json       TEXT,
  branch          TEXT,
  result_summary  TEXT,
  origin          TEXT NOT NULL DEFAULT 'operator',
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX idx_tasks_project_state ON tasks(project_id, state);

CREATE TABLE attempts (
  id          TEXT PRIMARY KEY,
  task_id     TEXT NOT NULL,
  n           INTEGER NOT NULL,
  diff_stat   TEXT,
  started_at  TEXT NOT NULL,
  ended_at    TEXT,
  FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE verdicts (
  id              TEXT PRIMARY KEY,
  task_id         TEXT NOT NULL,
  attempt         INTEGER NOT NULL,
  agent           TEXT NOT NULL,
  verdict         TEXT NOT NULL,
  reason          TEXT NOT NULL DEFAULT '',
  source          TEXT NOT NULL DEFAULT 'none',
  grounded_count  INTEGER NOT NULL DEFAULT 0,
  evidence_json   TEXT NOT NULL DEFAULT '[]',
  created_at      TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

CREATE TABLE events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id    TEXT,
  task_id       TEXT,
  agent         TEXT,
  type          TEXT NOT NULL,
  payload_json  TEXT NOT NULL DEFAULT '{}',
  ts            TEXT NOT NULL
);
CREATE INDEX idx_events_task ON events(task_id, id);
CREATE INDEX idx_events_project ON events(project_id, id);

CREATE TABLE usage (
  id              TEXT PRIMARY KEY,
  project_id      TEXT,
  task_id         TEXT,
  agent           TEXT NOT NULL,
  model_reported  TEXT,
  cost_usd        REAL,
  num_turns       INTEGER,
  ts              TEXT NOT NULL
);
CREATE INDEX idx_usage_project ON usage(project_id);

CREATE TABLE baselines (
  project_id  TEXT NOT NULL,
  sha         TEXT NOT NULL,
  output      TEXT NOT NULL,
  created_at  TEXT NOT NULL,
  PRIMARY KEY(project_id, sha)
);

CREATE TABLE quarantine (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL,
  pattern     TEXT NOT NULL,
  reason      TEXT NOT NULL,
  until       TEXT NOT NULL,
  created_by  TEXT NOT NULL DEFAULT 'operator',
  created_at  TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX idx_quarantine_project ON quarantine(project_id);

CREATE TABLE repo_knowledge (
  id                    TEXT PRIMARY KEY,
  project_id            TEXT NOT NULL,
  version               INTEGER NOT NULL,
  sha                   TEXT,
  manifest_fingerprint  TEXT,
  knowledge_json        TEXT NOT NULL,
  created_at            TEXT NOT NULL,
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX idx_repo_knowledge_project ON repo_knowledge(project_id, version);

CREATE TABLE bug_candidates (
  id                TEXT PRIMARY KEY,
  project_id        TEXT NOT NULL,
  fingerprint       TEXT NOT NULL,
  file              TEXT,
  symbol            TEXT,
  claim             TEXT NOT NULL,
  severity          TEXT,
  status            TEXT NOT NULL DEFAULT 'candidate',
  reproduced        INTEGER NOT NULL DEFAULT 0,
  task_id           TEXT,
  notes             TEXT,
  discovered_at     TEXT NOT NULL,
  last_examined_at  TEXT NOT NULL,
  fixed_at          TEXT,
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX idx_bug_fingerprint ON bug_candidates(project_id, fingerprint);

CREATE TABLE coverage (
  id                TEXT PRIMARY KEY,
  project_id        TEXT NOT NULL,
  region            TEXT NOT NULL,
  last_examined_at  TEXT,
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX idx_coverage_region ON coverage(project_id, region);
"""

_SQL_002 = """
ALTER TABLE repo_knowledge ADD COLUMN ai_enriched INTEGER NOT NULL DEFAULT 0;
"""

_SQL_003 = """
CREATE TABLE planning_sessions (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL,
  title           TEXT NOT NULL DEFAULT '',
  prompt          TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'active',
  turn_number     INTEGER NOT NULL DEFAULT 0,
  max_rounds      INTEGER NOT NULL DEFAULT 5,
  created_at      TEXT NOT NULL,
  completed_at    TEXT,
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE INDEX idx_planning_sessions_project ON planning_sessions(project_id, created_at DESC);

CREATE TABLE planning_messages (
  id              TEXT PRIMARY KEY,
  session_id      TEXT NOT NULL,
  agent           TEXT NOT NULL,
  role            TEXT NOT NULL DEFAULT 'agent',
  content         TEXT NOT NULL,
  turn_number     INTEGER NOT NULL DEFAULT 0,
  parent_id       TEXT,
  metadata_json   TEXT NOT NULL DEFAULT '{}',
  created_at      TEXT NOT NULL,
  FOREIGN KEY(session_id) REFERENCES planning_sessions(id) ON DELETE CASCADE
);
CREATE INDEX idx_planning_messages_session ON planning_messages(session_id, turn_number, id);

CREATE TABLE planning_task_nodes (
  id              TEXT PRIMARY KEY,
  session_id      TEXT NOT NULL,
  parent_id       TEXT,
  title           TEXT NOT NULL,
  description     TEXT NOT NULL DEFAULT '',
  status          TEXT NOT NULL DEFAULT 'proposed',
  level           INTEGER NOT NULL DEFAULT 0,
  sort_order      INTEGER NOT NULL DEFAULT 0,
  task_id         TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  FOREIGN KEY(session_id) REFERENCES planning_sessions(id) ON DELETE CASCADE,
  FOREIGN KEY(parent_id) REFERENCES planning_task_nodes(id) ON DELETE SET NULL
);
CREATE INDEX idx_planning_tasks_session ON planning_task_nodes(session_id, parent_id);

ALTER TABLE events ADD COLUMN planning_session_id TEXT;
CREATE INDEX idx_events_planning_session ON events(planning_session_id, id);
"""

_SQL_004 = """
ALTER TABLE tasks ADD COLUMN parent_task_id TEXT;
CREATE INDEX idx_tasks_parent ON tasks(parent_task_id);
"""

_SQL_005 = """
ALTER TABLE usage ADD COLUMN input_tokens INTEGER;
ALTER TABLE usage ADD COLUMN output_tokens INTEGER;
ALTER TABLE usage ADD COLUMN cache_read_tokens INTEGER;
ALTER TABLE usage ADD COLUMN cache_creation_tokens INTEGER;
"""

MIGRATIONS: list[Migration] = [
    Migration(version=1, name="initial_schema", sql=_SQL_001),
    Migration(version=2, name="add_ai_enriched_to_repo_knowledge", sql=_SQL_002),
    Migration(version=3, name="add_planning_sessions", sql=_SQL_003),
    Migration(version=4, name="add_parent_task_id_to_tasks", sql=_SQL_004),
    Migration(version=5, name="add_tokens_to_usage", sql=_SQL_005),
]
