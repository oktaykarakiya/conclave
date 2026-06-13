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

MIGRATIONS: list[Migration] = [
    Migration(version=1, name="initial_schema", sql=_SQL_001),
]
