"""Ordered schema migrations, applied on daemon startup.

Each migration bumps the version and is recorded in ``schema_version``. Migrations
are append-only — never edit a shipped migration; add a new one.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ...config import GLOBAL_DEFAULTS_PRESET, apply_project_overrides

logger = logging.getLogger("conclave.db.migrations")


@dataclass(frozen=True)
class Migration:
    """One step: SQL statements, a Python data step (``run``), or both — SQL runs first.

    ``run`` receives the connection of the migration's open transaction and must use only
    it: the write lock is held, so a :class:`Database` helper or repository call would
    deadlock. A migration with ``run`` must also insert its version into
    ``pending_data_migrations`` in its SQL; the runner clears it after ``run``, so a step a
    runner without data steps skipped still runs on the next start.
    """

    version: int
    name: str
    sql: str = ""
    run: Callable[[Any], Awaitable[None]] | None = None


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

_SQL_006 = """
CREATE INDEX idx_tasks_project_created ON tasks(project_id, created_at DESC);
CREATE INDEX idx_usage_task ON usage(task_id);
"""

_SQL_007 = """
ALTER TABLE planning_sessions ADD COLUMN stabilization_reason TEXT;
"""

# Migration 8 — promote the Phase-2 Bug-Fixer ledger from scaffold to its real shape.
# bug_candidates and coverage are seeded empty by migration 1 and still have no writers
# (verified), so there is no data to preserve: we DROP and recreate bug_candidates instead
# of performing the 12-step ALTER rebuild SQLite would otherwise demand. The runner wraps
# every migration in one BEGIN/COMMIT, so the DROP + CREATE + index land atomically — a
# failure rolls the whole thing back and leaves the old scaffold intact.
#
# The old two-field status model (status DEFAULT 'candidate' + a separate `reproduced`
# boolean) collapses into a single permissive `status` TEXT column over the 7-state machine
# the controller drives (validation lives in the Pydantic/enum layer, like tasks.state):
#   discovered → reproduced → fixing → fixed
#                          ↘ dismissed_false_positive | declined_needs_human | deferred
# The new columns carry the reproduction artifact (region, repro_test_path/body/hash), the
# fix-retry count (attempts), the human-handoff reason (decline_reason), and the serialized
# consensus payload (consensus_json). coverage gains priority + examined_count so the region
# scheduler can rank and age-out what it has already swept.
_SQL_008 = """
DROP TABLE bug_candidates;

CREATE TABLE bug_candidates (
  id                TEXT PRIMARY KEY,
  project_id        TEXT NOT NULL,
  fingerprint       TEXT NOT NULL,
  file              TEXT,
  symbol            TEXT,
  region            TEXT,
  claim             TEXT NOT NULL,
  severity          TEXT,
  status            TEXT NOT NULL DEFAULT 'discovered',
  repro_test_path   TEXT,
  repro_test_body   TEXT,
  repro_test_hash   TEXT,
  attempts          INTEGER NOT NULL DEFAULT 0,
  decline_reason    TEXT,
  consensus_json    TEXT NOT NULL DEFAULT '{}',
  task_id           TEXT,
  notes             TEXT,
  discovered_at     TEXT NOT NULL,
  last_examined_at  TEXT NOT NULL,
  fixed_at          TEXT,
  FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX idx_bug_fingerprint ON bug_candidates(project_id, fingerprint);

ALTER TABLE coverage ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
ALTER TABLE coverage ADD COLUMN examined_count INTEGER NOT NULL DEFAULT 0;
"""

_SQL_009 = """
CREATE TABLE task_checkpoints (
  task_id       TEXT NOT NULL,
  node_id       TEXT NOT NULL,
  attempt       INTEGER NOT NULL DEFAULT 1,
  status        TEXT NOT NULL DEFAULT 'completed',
  git_tree_sha  TEXT,
  artifact_json TEXT NOT NULL DEFAULT '{}',
  updated_at    TEXT NOT NULL,
  PRIMARY KEY(task_id, node_id, attempt),
  FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
);
CREATE INDEX idx_task_checkpoints_task ON task_checkpoints(task_id, attempt);

CREATE TABLE idempotency_keys (
  key           TEXT PRIMARY KEY,
  method        TEXT NOT NULL,
  path          TEXT NOT NULL,
  status_code   INTEGER NOT NULL,
  response_json TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL
);
CREATE INDEX idx_idempotency_created ON idempotency_keys(created_at);
"""

_SQL_010 = """
CREATE TABLE store_items (
  id           TEXT PRIMARY KEY,
  kind         TEXT NOT NULL,
  name         TEXT NOT NULL,
  description  TEXT NOT NULL DEFAULT '',
  tags_json    TEXT NOT NULL DEFAULT '[]',
  payload_json TEXT NOT NULL DEFAULT '{}',
  built_in     INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_store_items_kind_name ON store_items(kind, name);

ALTER TABLE tasks ADD COLUMN swarm_id TEXT;
CREATE INDEX idx_tasks_swarm ON tasks(project_id, swarm_id);
"""

_SQL_011 = """
CREATE TABLE interaction_logs (
  session_id         TEXT PRIMARY KEY,
  title              TEXT NOT NULL DEFAULT '',
  directory          TEXT NOT NULL DEFAULT '',
  path               TEXT NOT NULL DEFAULT '',
  slug               TEXT NOT NULL DEFAULT '',
  model              TEXT NOT NULL DEFAULT '',
  agent              TEXT NOT NULL DEFAULT '',
  cost               REAL NOT NULL DEFAULT 0.0,
  tokens_input       INTEGER NOT NULL DEFAULT 0,
  tokens_output      INTEGER NOT NULL DEFAULT 0,
  tokens_reasoning   INTEGER NOT NULL DEFAULT 0,
  tokens_cache_read  INTEGER NOT NULL DEFAULT 0,
  tokens_cache_write INTEGER NOT NULL DEFAULT 0,
  time_created       INTEGER NOT NULL DEFAULT 0,
  time_updated       INTEGER NOT NULL DEFAULT 0,
  created_at_str     TEXT NOT NULL DEFAULT '',
  updated_at_str     TEXT NOT NULL DEFAULT '',
  duration           REAL NOT NULL DEFAULT 0.0,
  tool_calls_count   INTEGER NOT NULL DEFAULT 0,
  message_count      INTEGER NOT NULL DEFAULT 0,
  user_prompt        TEXT NOT NULL DEFAULT '',
  tool_breakdown     TEXT NOT NULL DEFAULT '{}',
  messages           TEXT NOT NULL DEFAULT '[]',
  project_id         TEXT,
  task_id            TEXT,
  node_id            TEXT,
  source             TEXT NOT NULL DEFAULT 'conclave'
);
CREATE INDEX idx_interaction_logs_time ON interaction_logs(time_updated DESC);
CREATE INDEX idx_interaction_logs_project ON interaction_logs(project_id, time_updated DESC);
CREATE INDEX idx_interaction_logs_agent ON interaction_logs(agent, time_updated DESC);
"""

_SQL_012 = """
ALTER TABLE tasks ADD COLUMN source_branch TEXT;
"""

_SQL_013 = """
CREATE TABLE IF NOT EXISTS pending_data_migrations (
  version  INTEGER PRIMARY KEY
);
INSERT OR IGNORE INTO pending_data_migrations(version) VALUES (13);
"""


def _json_object(raw: Any) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


async def _normalise_project_config_deltas(conn: Any) -> None:
    """v13: rewrite each project's stored config to its minimal delta (built-in + global).

    Before layering, attach copied ``global-defaults`` into the project and a Settings save
    froze the whole resolved document into it, so later global and built-in changes never
    reached it. The delta is taken against what the project inherits, its ``default_branch``
    included, so it keeps the layered effective config exactly (the step is idempotent) and a
    stored ``execution.target_branch`` naming another branch, so tasks keep merging into the
    same branch. A stored config that is corrupt or does not validate is left as it is.
    """
    cur = await conn.execute(
        "SELECT payload_json FROM store_items WHERE kind = 'config_preset' AND name = ?",
        (GLOBAL_DEFAULTS_PRESET,),
    )
    row = await cur.fetchone()
    global_overrides = (_json_object(row["payload_json"]) if row is not None else None) or {}
    cur = await conn.execute("SELECT id, default_branch, config_json FROM projects")
    projects = list(await cur.fetchall())
    rewritten = 0
    for proj in projects:
        stored = _json_object(proj["config_json"])
        if stored is None:
            logger.warning("project %s: stored config is not a JSON object; left as is", proj["id"])
            continue
        try:
            _, delta = apply_project_overrides(
                global_overrides, stored, default_branch=proj["default_branch"]
            )
        except ValueError as exc:
            logger.warning("project %s: stored config is invalid; left as is: %s", proj["id"], exc)
            continue
        if delta != stored:
            await conn.execute(
                "UPDATE projects SET config_json = ? WHERE id = ?", (json.dumps(delta), proj["id"])
            )
            rewritten += 1
    logger.info(
        "normalised project configs to minimal deltas: %d of %d rewritten",
        rewritten,
        len(projects),
    )


MIGRATIONS: list[Migration] = [
    Migration(version=1, name="initial_schema", sql=_SQL_001),
    Migration(version=2, name="add_ai_enriched_to_repo_knowledge", sql=_SQL_002),
    Migration(version=3, name="add_planning_sessions", sql=_SQL_003),
    Migration(version=4, name="add_parent_task_id_to_tasks", sql=_SQL_004),
    Migration(version=5, name="add_tokens_to_usage", sql=_SQL_005),
    Migration(version=6, name="add_task_usage_indexes", sql=_SQL_006),
    Migration(version=7, name="add_stabilization_reason_to_planning_sessions", sql=_SQL_007),
    Migration(version=8, name="bug_candidate_ledger_7_state_machine", sql=_SQL_008),
    Migration(version=9, name="workflow_checkpoints_and_idempotency", sql=_SQL_009),
    Migration(version=10, name="store_and_swarms", sql=_SQL_010),
    Migration(version=11, name="interaction_logs_ledger", sql=_SQL_011),
    Migration(version=12, name="add_source_branch_to_tasks", sql=_SQL_012),
    Migration(
        version=13,
        name="normalise_project_config_deltas",
        sql=_SQL_013,
        run=_normalise_project_config_deltas,
    ),
]
