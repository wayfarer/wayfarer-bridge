# wayfarer-bridge

Wayfarer Bridge is a **Relational Context Store**: a lightweight **Python standard library–only** (`sqlite3`, `json`, `argparse`) shim that holds structured world state for terminal-based agents (Cursor, Claude Code, etc.). The canonical CLI binary name is **`wfb`**.

This document freezes **v1** so implementation can proceed without reinterpretation.

---

## v1 CLI surface

### CLI asset directory

- Default **`~/.wfb/`** is the per-user **CLI asset directory** for all tools-managed files today and in the future (additional config/cache/log files could live beside the DB later).
- The default relational store is **`~/.wfb/wayfarer.db`**.
- **`--db PATH`** overrides only the SQLite file path (for projects, backups, tests, etc.). The asset-directory convention still applies when you omit `--db`.

### `wfb init [--db PATH] [--no-browser] [--force-login]`

- Requires a local OAuth desktop client secret at **`~/.wfb/client_secret.json`** for the OSS/PyPI build.
- If missing, `init` always prints setup instructions and the official OAuth guide URL, then attempts to open the guide in your browser.
- Ensures **`~/.wfb/` exists** (`mkdir -p`), then applies the v1 schema to the target database (default `~/.wfb/wayfarer.db`).
- Creates the DB file at `PATH` when missing.
- Runs OAuth login and stores credentials at **`~/.wfb/token.json`**.
- `--no-browser` prints the auth URL instead of attempting browser-open.
- `--force-login` ignores any cached token and reruns login.
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

### `wfb gemini ping [--limit N]`

- Uses cached OAuth credentials from `~/.wfb/token.json`.
- Calls Gemini REST models endpoint and prints model count + names.
- Useful as an authenticated connectivity smoke test.

### `wfb gemini ask --prompt STRING [--model ID] [--session ID] [--max-history-turns N] [--system TEXT] [--auto-summarize on|off] [--summarize-model ID]`

- Uses local hybrid session memory by default:
  - active session is used implicitly,
  - if none exists, one is auto-created.
- Appends user and model turns to local session history.
- Sends history (bounded by `--max-history-turns`) to Gemini `generateContent`.
- `--session` explicitly routes one ask to a specific local session.
- `--system` overrides system instruction for the current call.
- `--auto-summarize on|off` controls model-based pre-ask compaction (default: `off`).
- `--summarize-model` optionally uses a different Gemini model specifically for summary generation.
- Default model: `gemini-2.5-flash`.

### `wfb gemini session ...`

- `wfb gemini session current` shows the active session id.
- `wfb gemini session list` lists all local sessions (`*` marks active).
- `wfb gemini session new [--name ...] [--model ...] [--system ...]` creates + activates.
- `wfb gemini session use --id ...` activates an existing session.
- `wfb gemini session reset [--id ...]` clears only that session's turn history.
- `wfb gemini session inspect [--id ...] [--format text|json]` inspects a session.

Optional future commands (`export`, `validate`) remain out of scope for this stage.

---

## OAuth setup prerequisite (OSS/PyPI)

The open-source `wfb` CLI does **not** ship centralized OAuth client secrets. You must provide your own Desktop OAuth client JSON at:

- **`~/.wfb/client_secret.json`**

Setup reference:

- [Gemini OAuth quickstart](https://ai.google.dev/gemini-api/docs/oauth)

Minimum steps:

1. Create a Google Cloud project and enable the Generative Language API.
2. Configure OAuth consent screen and add yourself as a test user while developing.
3. Create an OAuth **Desktop app** client, download the JSON, and place it at `~/.wfb/client_secret.json`.
4. Run `wfb init` again.

After successful login, `wfb` stores OAuth tokens locally at:

- **`~/.wfb/token.json`**

Troubleshooting:

- If browser-open fails, copy/paste the printed auth URL manually.
- For headless/manual environments, run `wfb init --no-browser`.
- If API calls fail after some time due to token refresh issues, rerun `wfb init --force-login` to refresh local credentials.
- In testing-mode OAuth projects, `refresh_token_expires_in` may be short (e.g. about 7 days), which requires periodic re-login.

---

## Gemini Sessions For Agents

API-managed reusable conversation handles are currently treated as unsupported in `wfb`'s active Gemini REST surface; see `docs/gemini_session_discovery.md`.

Agent-first workflow:

1. `wfb gemini session new --name planning`
2. `wfb gemini ask --prompt "first prompt"`
3. `wfb gemini ask --prompt "follow-up prompt"` (same context by default)
4. `wfb gemini session inspect --format json` (deterministic state for orchestration)
5. `wfb gemini session reset` when you want a clean context window

### Session Summarization

- Long sessions can be compacted before `ask` using a Gemini-generated summary (`--auto-summarize on`).
- Trigger thresholds are model-aware drift heuristics (`flash-lite`, `flash`, `pro`, fallback policy), not hard context-window limits.
- Compaction preserves recent turns and replaces older turns with a `history_summary` message.
- No deterministic fallback is used.
- If compaction is required and summary generation fails, `wfb gemini ask` fails with an error (hard-fail).
- Safety behavior: compacted state is only persisted after the final ask succeeds, so transient failures do not overwrite raw history.

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
  "db_path": "/Users/you/.wfb/wayfarer.db",
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

- `db_path` is the **resolved absolute path** to the database in use (default under `~/.wfb/wayfarer.db` when `--db` is omitted).
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
- **Default asset layout:** **`~/.wfb/`** is the CLI asset directory; **`wayfarer.db`** defaults there (`wfb_home()` / `default_db_path()` in `wfb.py` keep this centralized for future paths).
- **Contract:** This README is the single source of truth for v1 until v2 is documented.

The reference implementation **`wfb.py`** should behave as specified above.
