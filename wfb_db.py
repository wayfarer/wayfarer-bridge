"""Database schema and lifecycle helpers for wfb."""

from __future__ import annotations

import sqlite3
from pathlib import Path

UPDATED_AT_SQL = "strftime('%Y-%m-%dT%H:%M:%fZ','now')"

INIT_SQL = f"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER NOT NULL,
  applied_at TEXT NOT NULL DEFAULT ({UPDATED_AT_SQL})
);

CREATE TABLE IF NOT EXISTS active_tasks (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','in_progress','blocked','done')),
  priority INTEGER NOT NULL DEFAULT 0,
  owner TEXT,
  due_at TEXT,
  notes TEXT,
  source TEXT,
  updated_at TEXT NOT NULL DEFAULT ({UPDATED_AT_SQL}),
  metadata_json TEXT NOT NULL DEFAULT '{{}}'
);

CREATE TABLE IF NOT EXISTS environmental_constraints (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('tool_version_warning','policy','runtime_limit','dependency','other')),
  name TEXT NOT NULL,
  value TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('info','warn','error')),
  scope TEXT,
  source TEXT,
  updated_at TEXT NOT NULL DEFAULT ({UPDATED_AT_SQL}),
  metadata_json TEXT NOT NULL DEFAULT '{{}}'
);

CREATE TABLE IF NOT EXISTS style_specifications (
  id TEXT PRIMARY KEY,
  category TEXT NOT NULL CHECK (category IN ('tone','formatting','coding_style','workflow','other')),
  rule TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  applies_to TEXT,
  source TEXT,
  updated_at TEXT NOT NULL DEFAULT ({UPDATED_AT_SQL}),
  metadata_json TEXT NOT NULL DEFAULT '{{}}'
);

CREATE INDEX IF NOT EXISTS idx_active_tasks_status ON active_tasks(status);
CREATE INDEX IF NOT EXISTS idx_constraints_severity ON environmental_constraints(severity);
CREATE INDEX IF NOT EXISTS idx_style_priority ON style_specifications(priority DESC);
"""


def connect_db(db_path: str | Path) -> sqlite3.Connection:
    p = Path(db_path)
    conn = sqlite3.connect(str(p))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def require_v1_schema(conn: sqlite3.Connection) -> None:
    """Raise sqlite3.OperationalError if `wfb init` has not been run on this DB."""
    try:
        n = conn.execute("SELECT COUNT(*) AS c FROM schema_version").fetchone()["c"]
    except sqlite3.OperationalError as e:
        raise sqlite3.OperationalError(
            "database not initialized (missing schema); run `wfb init` first"
        ) from e
    if n == 0:
        raise sqlite3.OperationalError("database not initialized; run `wfb init` first")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(INIT_SQL)
    n = conn.execute("SELECT COUNT(*) AS c FROM schema_version").fetchone()["c"]
    if n == 0:
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    conn.commit()
