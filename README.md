# wayfarer-bridge

Wayfarer Bridge is a **Relational Context Store**: a lightweight **Python standard library–only** (`sqlite3`, `json`, `argparse`) shim that holds structured world state for terminal-based agents (Cursor, Claude Code, etc.). The canonical CLI binary name is **`wfb`**.

This document freezes **v1** so implementation can proceed without reinterpretation.

---

## v1 CLI surface

Default database path is `./wayfarer.db` unless `--db PATH` overrides.

### `wfb init [--db PATH]`

- Creates `wayfarer.db` (or `PATH`) and applies v1 schema if missing.
- **Idempotent:** safe to run multiple times (`CREATE TABLE IF NOT EXISTS`, etc.).
- Ensures `schema_version` reflects v1 after successful init.

### `wfb seed (--json STRING | --file PATH) [--db PATH] [--replace]`

- Ingests a **seed envelope** (JSON).
- **`--replace`:** before inserting, deletes all rows from `active_tasks`, `environmental_constraints`, and `style_specifications`; then inserts the envelope contents. Omit for **upsert** behavior.
- **Upsert (default):** for each row, insert or replace by **`id`** (see [Upsert semantics](#upsert-semantics)).
- Entire ingest runs in **one transaction** commit or rollback on error.

### `wfb status [--db PATH] [--format text|json] [--limit N]`

- Summarizes current world state from the DB.
- **`--format text`** (default): concise, agent-readable summary.
- **`--format json`:** deterministic machine-readable snapshot (shape below).
- **`--limit N`:** caps list previews in text mode (default **5**); applies to highlighted rows where lists are truncated.

Optional future commands (`export`, `validate`) are **out of scope** for v1; v1 ships only **`init`**, **`seed`**, **`status`**.

---

## Canonical seed envelope (JSON)

Top-level object:

| Field                         | Required | Notes |
|------------------------------|----------|--------|
| `version`                     | Yes      | Integer; must equal **1** for v1 envelopes. |
| `generated_at`                | No       | ISO-8601 UTC string (recommended). |
| `source`                      | No       | Short string naming origin (e.g. `gemini`, `cursor`). |
| `active_tasks`               | No       | Array; omit or `[]` for none. |
| `environmental_constraints`   | No       | Array; omit or `[]` for none. |
| `style_specifications`       | No       | Array; omit or `[]` for none. |

Example:

```json
{
  "version": 1,
  "generated_at": "2026-05-05T23:00:00Z",
  "source": "gemini",
  "active_tasks": [],
  "environmental_constraints": [],
  "style_specifications": []
}
```

---

## Record shapes and validation

Validation runs before any DB writes. **`metadata`** (when present) must be a JSON object at parse time; it is stored as a compact JSON string in `metadata_json` columns (`json.dumps(..., separators=(",", ":"))`).

### `active_tasks[]`

| Field       | Required | Type   | Constraints |
|------------|----------|--------|--------------|
| `id`       | Yes      | string | Primary upsert key. |
| `title`    | Yes      | string | |
| `status`   | Yes      | string | One of: `pending`, `in_progress`, `blocked`, `done`. |
| `priority`| No       | int    | Default **0**. |
| `owner`    | No       | string | |
| `due_at`   | No       | string | ISO-8601 recommended. |
| `notes`    | No       | string | |
| `source`   | No       | string | Overrides envelope `source` for this row when present. |
| `metadata` | No       | object | |

### `environmental_constraints[]`

| Field       | Required | Type   | Constraints |
|------------|----------|--------|--------------|
| `id`       | Yes      | string | Primary upsert key. |
| `kind`     | Yes      | string | One of: `tool_version_warning`, `policy`, `runtime_limit`, `dependency`, `other`. |
| `name`     | Yes      | string | |
| `value`    | Yes      | string | |
| `severity` | Yes      | string | One of: `info`, `warn`, `error`. |
| `scope`    | No       | string | e.g. `global`, `repo`, `task:<id>`. |
| `source`   | No       | string | Overrides envelope `source` for this row when present. |
| `metadata` | No       | object | |

### `style_specifications[]`

| Field         | Required | Type   | Constraints |
|---------------|----------|--------|--------------|
| `id`          | Yes      | string | Primary upsert key. |
| `category`    | Yes      | string | One of: `tone`, `formatting`, `coding_style`, `workflow`, `other`. |
| `rule`        | Yes      | string | |
| `priority`    | No       | int    | Default **0**. |
| `applies_to`  | No       | string | e.g. `all`, `python`, `docs`. |
| `source`      | No       | string | Overrides envelope `source` for this row when present. |
| `metadata`    | No       | object | |

### Upsert semantics

- Default mode: **`INSERT … ON CONFLICT(id) DO UPDATE`** for each table row.
- **`updated_at`** is set on every successful write for that row (`strftime('%Y-%m-%dT%H:%M:%fZ','now')` UTC).
- Row **`source`:** use the item’s `source` if present; otherwise use the envelope’s `source` if present; otherwise `NULL`.
- Unknown extra keys **at the envelope top level**: **reject** (only keys listed in [Canonical seed envelope](#canonical-seed-envelope-json) are allowed).
- Unknown keys **inside array items**: **reject**. Allowed keys per type:
  - `active_tasks`: `id`, `title`, `status`, `priority`, `owner`, `due_at`, `notes`, `source`, `metadata`.
  - `environmental_constraints`: `id`, `kind`, `name`, `value`, `severity`, `scope`, `source`, `metadata`.
  - `style_specifications`: `id`, `category`, `rule`, `priority`, `applies_to`, `source`, `metadata`.

---

## SQLite schema v1 (`schema_version` + DDL)

v1 does not define foreign keys between entity tables. Run `PRAGMA foreign_keys = ON;` once when opening the DB (recommended).

Exactly one semantics for **`schema_version`:** after `wfb init`, there is one row with `version = 1`. Init must insert that row if the table is empty (v1 is the initial schema; no migrations yet).

Full v1 DDL:

```sql
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER NOT NULL,
  applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
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
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS environmental_constraints (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('tool_version_warning','policy','runtime_limit','dependency','other')),
  name TEXT NOT NULL,
  value TEXT NOT NULL,
  severity TEXT NOT NULL CHECK (severity IN ('info','warn','error')),
  scope TEXT,
  source TEXT,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS style_specifications (
  id TEXT PRIMARY KEY,
  category TEXT NOT NULL CHECK (category IN ('tone','formatting','coding_style','workflow','other')),
  rule TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  applies_to TEXT,
  source TEXT,
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_active_tasks_status ON active_tasks(status);
CREATE INDEX IF NOT EXISTS idx_constraints_severity ON environmental_constraints(severity);
CREATE INDEX IF NOT EXISTS idx_style_priority ON style_specifications(priority DESC);
```

---

## `wfb status` output contract

### Text (default)

Include, in order:

1. **Header:** resolved `db` path and current generation timestamp (ISO-8601 UTC).
2. **Counts:** tasks by `status`; constraints by `severity`; count of style rules.
3. **In progress / blocked:** up to `--limit` tasks, ordered by `priority` desc then `updated_at` desc.
4. **Warnings / errors:** constraints with `severity` in `warn`, `error` (up to `--limit`), ordered by severity then `updated_at` desc.
5. **Style rules:** up to `--limit` rows by `priority` desc, then `updated_at` desc.
6. **Last updated:** max(`updated_at`) per table (`active_tasks`, `environmental_constraints`, `style_specifications`).

### JSON (`--format json`)

Top-level shape (field order not normative; keys must exist):

```json
{
  "version": 1,
  "db_path": "./wayfarer.db",
  "summary": {
    "tasks": {
      "pending": 0,
      "in_progress": 0,
      "blocked": 0,
      "done": 0
    },
    "constraints": {
      "info": 0,
      "warn": 0,
      "error": 0
    },
    "style_specifications": 0
  },
  "highlights": {
    "tasks": [],
    "constraints": [],
    "style_specifications": []
  },
  "updated_at": {
    "active_tasks": null,
    "environmental_constraints": null,
    "style_specifications": null
  }
}
```

- `highlights.*` arrays contain one object per row, with **keys matching SQL column names** (`id`, `title`, `status`, …, `metadata_json`, `updated_at`). Values are JSON types as returned from the DB (`metadata_json` remains a **string**).
- Empty DB: counts zero, `highlights` empty arrays, `updated_at` values `null`.

---

## Exit codes

| Code | Meaning |
|------|---------|
| `0`  | Success. |
| `2`  | CLI usage / argument error (e.g. missing `--json`/`--file` for `seed`). |
| `3`  | Validation error (bad envelope, wrong `version`, invalid enum, malformed JSON). |
| `4`  | Database error (connect, SQL, transaction). |
| `5`  | File I/O error (unreadable `--file` path). |

---

## Implementation baseline (accepted for v1)

- **Language:** Python 3, stdlib only: `argparse`, `sqlite3`, `json`, `pathlib` (optional), `sys`.
- **Commands:** `wfb init`, `wfb seed`, `wfb status` only.
- **Binary name:** `wfb` everywhere.
- **Contract:** This README is the single source of truth for v1 until v2 is documented.

No Python implementation is required to consider the **v1 contract frozen**; the next step is implementing `wfb` per this document.
