#!/usr/bin/env python3
"""
wayfarer-bridge — Relational Context Store CLI (v1).
Stdlib only: argparse, json, sqlite3, sys; pathlib optional per contract.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

# --- Exit codes (README) ---
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VALIDATION = 3
EXIT_DB = 4
EXIT_IO = 5

DEFAULT_STATUS_LIMIT = 5
OAUTH_GUIDE_URL = "https://ai.google.dev/gemini-api/docs/oauth"


def wfb_home() -> Path:
    """Default directory for CLI-managed assets. Future artifacts live under here too."""
    return Path.home() / ".wfb"


def default_db_path() -> Path:
    """Default SQLite store path."""
    return wfb_home() / "wayfarer.db"


def client_secret_path() -> Path:
    """Local OAuth desktop client secret location for OSS/PyPI onboarding."""
    return wfb_home() / "client_secret.json"


def _print_oauth_setup_instructions() -> None:
    secret_path = client_secret_path()
    print("OAuth setup required for OSS/PyPI build.", file=sys.stderr)
    print(
        f"Expected OAuth desktop client secret file at: {secret_path}",
        file=sys.stderr,
    )
    print("Setup steps:", file=sys.stderr)
    print("  1) Open the Gemini OAuth guide.", file=sys.stderr)
    print("  2) Create a Desktop OAuth client in your Google Cloud project.", file=sys.stderr)
    print("  3) Download the JSON and place it at ~/.wfb/client_secret.json.", file=sys.stderr)
    print(f"Guide: {OAUTH_GUIDE_URL}", file=sys.stderr)
    print("After placing the file, run: wfb init", file=sys.stderr)

ENVELOPE_KEYS = frozenset(
    {
        "version",
        "generated_at",
        "source",
        "active_tasks",
        "environmental_constraints",
        "style_specifications",
    }
)

TASK_KEYS = frozenset(
    {"id", "title", "status", "priority", "owner", "due_at", "notes", "source", "metadata"}
)
CONSTRAINT_KEYS = frozenset(
    {"id", "kind", "name", "value", "severity", "scope", "source", "metadata"}
)
STYLE_KEYS = frozenset(
    {"id", "category", "rule", "priority", "applies_to", "source", "metadata"}
)

TASK_STATUSES = frozenset({"pending", "in_progress", "blocked", "done"})
CONSTRAINT_KINDS = frozenset(
    {"tool_version_warning", "policy", "runtime_limit", "dependency", "other"}
)
SEVERITIES = frozenset({"info", "warn", "error"})
STYLE_CATEGORIES = frozenset({"tone", "formatting", "coding_style", "workflow", "other"})

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


class ValidationError(Exception):
    """Invalid seed envelope or record (exit 3)."""


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _is_int(value: Any) -> bool:
    return type(value) is int  # noqa: E721 — reject bool/subclass ambiguity


def _require_non_empty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value


def _metadata_json(metadata: Any | None) -> str:
    if metadata is None:
        return "{}"
    if not isinstance(metadata, dict):
        raise ValidationError("metadata must be a JSON object when present")
    return json.dumps(metadata, separators=(",", ":"))


def validate_envelope(raw: Any) -> dict[str, Any]:
    """Validate top-level envelope; return normalized dict for persistence."""
    if not isinstance(raw, dict):
        raise ValidationError("seed payload must be a JSON object")

    unknown = set(raw) - ENVELOPE_KEYS
    if unknown:
        raise ValidationError(f"unknown envelope key(s): {sorted(unknown)!r}")

    if "version" not in raw:
        raise ValidationError("envelope missing required field: version")

    ver = raw["version"]
    if not _is_int(ver) or ver != 1:
        raise ValidationError("version must be integer 1")

    if "generated_at" in raw and raw["generated_at"] is not None:
        if not isinstance(raw["generated_at"], str):
            raise ValidationError("generated_at must be a string when present")

    env_source = raw.get("source")
    if env_source is not None and not isinstance(env_source, str):
        raise ValidationError("source must be a string when present")

    tasks = raw.get("active_tasks", [])
    constraints = raw.get("environmental_constraints", [])
    styles = raw.get("style_specifications", [])

    if tasks is None:
        tasks = []
    if constraints is None:
        constraints = []
    if styles is None:
        styles = []

    if not isinstance(tasks, list):
        raise ValidationError("active_tasks must be an array")
    if not isinstance(constraints, list):
        raise ValidationError("environmental_constraints must be an array")
    if not isinstance(styles, list):
        raise ValidationError("style_specifications must be an array")

    return {
        "version": 1,
        "generated_at": raw.get("generated_at"),
        "source": env_source,
        "active_tasks": _validate_tasks(tasks, env_source),
        "environmental_constraints": _validate_constraints(constraints, env_source),
        "style_specifications": _validate_styles(styles, env_source),
    }


def _strict_item_keys(item: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    extra = set(item) - allowed
    if extra:
        raise ValidationError(f"{label}: unknown key(s) {sorted(extra)!r}")


def _validate_tasks(rows: Iterable[Mapping[str, Any]], env_source: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        label = f"active_tasks[{i}]"
        if not isinstance(row, dict):
            raise ValidationError(f"{label} must be an object")
        _strict_item_keys(row, TASK_KEYS, label)

        pid = _require_non_empty_str(row["id"], f"{label}.id")
        title = _require_non_empty_str(row["title"], f"{label}.title")
        status = row["status"]
        if not isinstance(status, str) or status not in TASK_STATUSES:
            raise ValidationError(f"{label}.status must be one of {sorted(TASK_STATUSES)!r}")

        priority = row.get("priority", 0)
        if priority is None:
            priority = 0
        if not _is_int(priority):
            raise ValidationError(f"{label}.priority must be an integer")

        for opt in ("owner", "due_at", "notes"):
            val = row.get(opt)
            if val is None:
                continue
            if not isinstance(val, str):
                raise ValidationError(f"{label}.{opt} must be a string when present")

        src = row.get("source")
        if src is not None and not isinstance(src, str):
            raise ValidationError(f"{label}.source must be a string when present")

        md = row.get("metadata")
        if "metadata" in row and md is not None:
            _metadata_json(md)  # validate

        resolved_source: str | None
        if isinstance(src, str):
            resolved_source = src
        elif isinstance(env_source, str):
            resolved_source = env_source
        else:
            resolved_source = None

        out.append(
            {
                "id": pid,
                "title": title,
                "status": status,
                "priority": priority,
                "owner": row.get("owner"),
                "due_at": row.get("due_at"),
                "notes": row.get("notes"),
                "source": resolved_source,
                "metadata_json": _metadata_json(md if md is not None else None),
            }
        )
    return out


def _validate_constraints(
    rows: Iterable[Mapping[str, Any]], env_source: Any
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        label = f"environmental_constraints[{i}]"
        if not isinstance(row, dict):
            raise ValidationError(f"{label} must be an object")
        _strict_item_keys(row, CONSTRAINT_KEYS, label)

        cid = _require_non_empty_str(row["id"], f"{label}.id")
        kind = row["kind"]
        if not isinstance(kind, str) or kind not in CONSTRAINT_KINDS:
            raise ValidationError(f"{label}.kind must be one of {sorted(CONSTRAINT_KINDS)!r}")

        name = _require_non_empty_str(row["name"], f"{label}.name")
        value = _require_non_empty_str(row["value"], f"{label}.value")

        severity = row["severity"]
        if not isinstance(severity, str) or severity not in SEVERITIES:
            raise ValidationError(f"{label}.severity must be one of {sorted(SEVERITIES)!r}")

        scope = row.get("scope")
        if scope is not None and not isinstance(scope, str):
            raise ValidationError(f"{label}.scope must be a string when present")

        src = row.get("source")
        if src is not None and not isinstance(src, str):
            raise ValidationError(f"{label}.source must be a string when present")

        md = row.get("metadata")
        if "metadata" in row and md is not None:
            _metadata_json(md)

        resolved_source: str | None
        if isinstance(src, str):
            resolved_source = src
        elif isinstance(env_source, str):
            resolved_source = env_source
        else:
            resolved_source = None

        out.append(
            {
                "id": cid,
                "kind": kind,
                "name": name,
                "value": value,
                "severity": severity,
                "scope": scope,
                "source": resolved_source,
                "metadata_json": _metadata_json(md if md is not None else None),
            }
        )
    return out


def _validate_styles(rows: Iterable[Mapping[str, Any]], env_source: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        label = f"style_specifications[{i}]"
        if not isinstance(row, dict):
            raise ValidationError(f"{label} must be an object")
        _strict_item_keys(row, STYLE_KEYS, label)

        sid = _require_non_empty_str(row["id"], f"{label}.id")
        category = row["category"]
        if not isinstance(category, str) or category not in STYLE_CATEGORIES:
            raise ValidationError(
                f"{label}.category must be one of {sorted(STYLE_CATEGORIES)!r}"
            )

        rule = _require_non_empty_str(row["rule"], f"{label}.rule")

        priority = row.get("priority", 0)
        if priority is None:
            priority = 0
        if not _is_int(priority):
            raise ValidationError(f"{label}.priority must be an integer")

        applies = row.get("applies_to")
        if applies is not None and not isinstance(applies, str):
            raise ValidationError(f"{label}.applies_to must be a string when present")

        src = row.get("source")
        if src is not None and not isinstance(src, str):
            raise ValidationError(f"{label}.source must be a string when present")

        md = row.get("metadata")
        if "metadata" in row and md is not None:
            _metadata_json(md)

        resolved_source: str | None
        if isinstance(src, str):
            resolved_source = src
        elif isinstance(env_source, str):
            resolved_source = env_source
        else:
            resolved_source = None

        out.append(
            {
                "id": sid,
                "category": category,
                "rule": rule,
                "priority": priority,
                "applies_to": applies,
                "source": resolved_source,
                "metadata_json": _metadata_json(md if md is not None else None),
            }
        )
    return out


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


def _upsert_task(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        f"""
        INSERT INTO active_tasks (
          id, title, status, priority, owner, due_at, notes, source, updated_at, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, {UPDATED_AT_SQL}, ?)
        ON CONFLICT(id) DO UPDATE SET
          title=excluded.title,
          status=excluded.status,
          priority=excluded.priority,
          owner=excluded.owner,
          due_at=excluded.due_at,
          notes=excluded.notes,
          source=excluded.source,
          updated_at={UPDATED_AT_SQL},
          metadata_json=excluded.metadata_json
        """,
        (
            row["id"],
            row["title"],
            row["status"],
            row["priority"],
            row["owner"],
            row["due_at"],
            row["notes"],
            row["source"],
            row["metadata_json"],
        ),
    )


def _upsert_constraint(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        f"""
        INSERT INTO environmental_constraints (
          id, kind, name, value, severity, scope, source, updated_at, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, {UPDATED_AT_SQL}, ?)
        ON CONFLICT(id) DO UPDATE SET
          kind=excluded.kind,
          name=excluded.name,
          value=excluded.value,
          severity=excluded.severity,
          scope=excluded.scope,
          source=excluded.source,
          updated_at={UPDATED_AT_SQL},
          metadata_json=excluded.metadata_json
        """,
        (
            row["id"],
            row["kind"],
            row["name"],
            row["value"],
            row["severity"],
            row["scope"],
            row["source"],
            row["metadata_json"],
        ),
    )


def _upsert_style(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    conn.execute(
        f"""
        INSERT INTO style_specifications (
          id, category, rule, priority, applies_to, source, updated_at, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, {UPDATED_AT_SQL}, ?)
        ON CONFLICT(id) DO UPDATE SET
          category=excluded.category,
          rule=excluded.rule,
          priority=excluded.priority,
          applies_to=excluded.applies_to,
          source=excluded.source,
          updated_at={UPDATED_AT_SQL},
          metadata_json=excluded.metadata_json
        """,
        (
            row["id"],
            row["category"],
            row["rule"],
            row["priority"],
            row["applies_to"],
            row["source"],
            row["metadata_json"],
        ),
    )


def seed_db(conn: sqlite3.Connection, envelope: dict[str, Any], replace: bool) -> None:
    try:
        conn.execute("BEGIN IMMEDIATE")
        if replace:
            conn.execute("DELETE FROM active_tasks")
            conn.execute("DELETE FROM environmental_constraints")
            conn.execute("DELETE FROM style_specifications")

        for t in envelope["active_tasks"]:
            _upsert_task(conn, t)
        for c in envelope["environmental_constraints"]:
            _upsert_constraint(conn, c)
        for s in envelope["style_specifications"]:
            _upsert_style(conn, s)

        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise


def _row_to_obj(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def cmd_status(conn: sqlite3.Connection, db_path: Path, fmt: str, limit: int) -> None:
    if fmt == "json":
        payload = status_json(conn, db_path)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return

    gen = _utc_now_iso()
    print(f"Database: {db_path.resolve()}")
    print(f"Generated: {gen}")
    print()

    task_counts = {s: 0 for s in TASK_STATUSES}
    for r in conn.execute(
        "SELECT status, COUNT(*) AS c FROM active_tasks GROUP BY status"
    ):
        task_counts[r["status"]] = r["c"]

    sev_counts = {s: 0 for s in SEVERITIES}
    for r in conn.execute(
        "SELECT severity, COUNT(*) AS c FROM environmental_constraints GROUP BY severity"
    ):
        sev_counts[r["severity"]] = r["c"]

    style_n = conn.execute("SELECT COUNT(*) AS c FROM style_specifications").fetchone()["c"]

    parts = [
        f"pending={task_counts['pending']}",
        f"in_progress={task_counts['in_progress']}",
        f"blocked={task_counts['blocked']}",
        f"done={task_counts['done']}",
    ]
    print("Tasks:", " ".join(parts))
    print(
        "Constraints:",
        f"info={sev_counts['info']} warn={sev_counts['warn']} error={sev_counts['error']}",
    )
    print("Style rules:", style_n)
    print()

    print("## In progress / blocked")
    q_task = """
        SELECT * FROM active_tasks
        WHERE status IN ('in_progress', 'blocked')
        ORDER BY priority DESC, updated_at DESC, id ASC
        LIMIT ?
    """
    trows = list(conn.execute(q_task, (limit,)))
    if not trows:
        print("(none)")
    else:
        for r in trows:
            print(
                f"- [{r['status']}] (p={r['priority']}) {r['title']}  id={r['id']}"
            )
    print()

    print("## Warnings / errors")
    q_cons = """
        SELECT * FROM environmental_constraints
        WHERE severity IN ('warn', 'error')
        ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warn' THEN 1 END, updated_at DESC, id ASC
        LIMIT ?
    """
    crows = list(conn.execute(q_cons, (limit,)))
    if not crows:
        print("(none)")
    else:
        for r in crows:
            print(f"- [{r['severity']}] {r['name']}: {r['value']}  id={r['id']}")
    print()

    print("## Style rules")
    q_style = """
        SELECT * FROM style_specifications
        ORDER BY priority DESC, updated_at DESC, id ASC
        LIMIT ?
    """
    srows = list(conn.execute(q_style, (limit,)))
    if not srows:
        print("(none)")
    else:
        for r in srows:
            print(f"- (p={r['priority']}) [{r['category']}] {r['rule']}  id={r['id']}")
    print()

    print("## Last updated")
    for table in ("active_tasks", "environmental_constraints", "style_specifications"):
        m = conn.execute(f"SELECT MAX(updated_at) AS m FROM {table}").fetchone()["m"]
        print(f"{table}: {m if m else 'null'}")


def status_json(conn: sqlite3.Connection, db_path: Path) -> dict[str, Any]:
    task_counts = {s: 0 for s in TASK_STATUSES}
    for r in conn.execute(
        "SELECT status, COUNT(*) AS c FROM active_tasks GROUP BY status"
    ):
        task_counts[r["status"]] = r["c"]

    sev_counts = {s: 0 for s in SEVERITIES}
    for r in conn.execute(
        "SELECT severity, COUNT(*) AS c FROM environmental_constraints GROUP BY severity"
    ):
        sev_counts[r["severity"]] = r["c"]

    style_n = conn.execute("SELECT COUNT(*) AS c FROM style_specifications").fetchone()["c"]

    task_rows = [
        _row_to_obj(r)
        for r in conn.execute(
            "SELECT * FROM active_tasks ORDER BY priority DESC, updated_at DESC, id ASC"
        )
    ]
    cons_rows = [
        _row_to_obj(r)
        for r in conn.execute(
            """
            SELECT * FROM environmental_constraints
            ORDER BY CASE severity WHEN 'error' THEN 0 WHEN 'warn' THEN 1 WHEN 'info' THEN 2 END,
                     updated_at DESC, id ASC
            """
        )
    ]
    style_rows = [
        _row_to_obj(r)
        for r in conn.execute(
            "SELECT * FROM style_specifications ORDER BY priority DESC, updated_at DESC, id ASC"
        )
    ]

    def max_updated(table: str) -> str | None:
        m = conn.execute(f"SELECT MAX(updated_at) AS m FROM {table}").fetchone()["m"]
        return m

    return {
        "version": 1,
        "db_path": str(db_path.resolve()),
        "summary": {
            "tasks": {k: task_counts[k] for k in ("pending", "in_progress", "blocked", "done")},
            "constraints": {k: sev_counts[k] for k in ("info", "warn", "error")},
            "style_specifications": style_n,
        },
        "highlights": {
            "tasks": task_rows,
            "constraints": cons_rows,
            "style_specifications": style_rows,
        },
        "updated_at": {
            "active_tasks": max_updated("active_tasks"),
            "environmental_constraints": max_updated("environmental_constraints"),
            "style_specifications": max_updated("style_specifications"),
        },
    }


def _load_seed_json(path: Path | None, json_inline: str | None) -> Any:
    if path is not None:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise OSError(str(e)) from e
        return json.loads(text)
    assert json_inline is not None
    return json.loads(json_inline)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wfb", description="Wayfarer Bridge v1 CLI")
    p.add_argument(
        "--db",
        dest="db",
        default=str(default_db_path()),
        help=(
            "SQLite database path "
            "(default: ~/.wfb/wayfarer.db; wfb init ensures ~/.wfb/ exists)"
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    initp = sub.add_parser("init", help="create local database and schema")
    initp.add_argument(
        "--no-open-oauth-guide",
        action="store_true",
        help="do not attempt to open Gemini OAuth guide automatically",
    )

    sp = sub.add_parser("seed", help="ingest seed JSON envelope")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--json", dest="json_data", metavar="STRING", help="inline JSON envelope")
    g.add_argument("--file", dest="file", metavar="PATH", type=Path, help="path to JSON file")
    sp.add_argument(
        "--replace",
        action="store_true",
        help="delete all rows in entity tables before insert",
    )

    st = sub.add_parser("status", help="print world state summary")
    st.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        dest="fmt",
        help="output format",
    )
    st.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_STATUS_LIMIT,
        metavar="N",
        help=f"preview row cap for text output (default: {DEFAULT_STATUS_LIMIT})",
    )
    p.set_defaults(file=None, json_data=None, no_open_oauth_guide=False)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)

    db_path = Path(args.db)

    if args.command == "init":
        try:
            wfb_home().mkdir(parents=True, exist_ok=True)
            if not client_secret_path().is_file():
                _print_oauth_setup_instructions()
                if not args.no_open_oauth_guide:
                    try:
                        webbrowser.open(OAUTH_GUIDE_URL)
                    except Exception:
                        pass
                return EXIT_IO
            conn = connect_db(db_path)
            try:
                init_db(conn)
            finally:
                conn.close()
        except sqlite3.Error as e:
            _err(str(e))
            return EXIT_DB
        return EXIT_OK

    try:
        conn = connect_db(db_path)
    except sqlite3.Error as e:
        _err(str(e))
        return EXIT_DB

    try:
        if args.command == "seed":
            try:
                raw = _load_seed_json(args.file, args.json_data)
            except OSError as e:
                _err(str(e))
                return EXIT_IO
            except json.JSONDecodeError as e:
                _err(f"invalid JSON: {e}")
                return EXIT_VALIDATION

            try:
                envelope = validate_envelope(raw)
            except ValidationError as e:
                _err(str(e))
                return EXIT_VALIDATION

            try:
                require_v1_schema(conn)
                seed_db(conn, envelope, args.replace)
            except sqlite3.Error as e:
                _err(str(e))
                return EXIT_DB
            return EXIT_OK

        if args.command == "status":
            if args.limit < 0:
                _err("--limit must be non-negative")
                return EXIT_USAGE
            try:
                require_v1_schema(conn)
                cmd_status(conn, db_path, args.fmt, args.limit)
            except sqlite3.Error as e:
                _err(str(e))
                return EXIT_DB
            return EXIT_OK
    finally:
        conn.close()

    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
