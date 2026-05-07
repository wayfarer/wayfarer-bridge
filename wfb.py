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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from wfb_db import UPDATED_AT_SQL, connect_db, init_db, require_v1_schema
from wfb_gemini_api import (
    DEFAULT_MODEL,
    GeminiApiError,
    api_managed_state_supported,
    ask_with_messages,
    list_models,
)
from wfb_gemini_sessions import (
    append_turn,
    create_session,
    get_active_session_id,
    list_sessions,
    load_session,
    reset_session,
    save_session,
    set_active_session,
)
from wfb_oauth import (
    ensure_client_secret_present,
    ensure_logged_in,
    maybe_open_oauth_guide,
    print_oauth_setup_instructions,
    OAuthFlowError,
)
from wfb_paths import default_db_path, wfb_home

# --- Exit codes (README) ---
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VALIDATION = 3
EXIT_DB = 4
EXIT_IO = 5

DEFAULT_STATUS_LIMIT = 5

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
    initp.add_argument(
        "--no-browser",
        action="store_true",
        help="print OAuth URL instead of attempting to open a browser",
    )
    initp.add_argument(
        "--force-login",
        action="store_true",
        help="ignore cached token and run OAuth login again",
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

    gem = sub.add_parser("gemini", help="call Gemini APIs using local OAuth token")
    gem_sub = gem.add_subparsers(dest="gemini_command", required=True)

    ping = gem_sub.add_parser("ping", help="list available Gemini models")
    ping.add_argument(
        "--limit",
        type=int,
        default=10,
        metavar="N",
        help="max model names to print (default: 10)",
    )

    ask = gem_sub.add_parser("ask", help="run a single text prompt")
    ask.add_argument(
        "--prompt",
        required=True,
        help="prompt text to send to Gemini",
    )
    ask.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"model id (default: {DEFAULT_MODEL})",
    )
    ask.add_argument("--session", help="optional session id; defaults to active session")
    ask.add_argument(
        "--max-history-turns",
        type=int,
        default=30,
        metavar="N",
        help="max historical turns to include (default: 30)",
    )
    ask.add_argument(
        "--system",
        help="optional session-level system instruction override for this call",
    )

    sess = gem_sub.add_parser("session", help="manage local Gemini chat sessions")
    sess_sub = sess.add_subparsers(dest="gemini_session_command", required=True)
    sess_sub.add_parser("current", help="show active session id")
    sess_sub.add_parser("list", help="list local sessions")
    newp = sess_sub.add_parser("new", help="create and select a new session")
    newp.add_argument("--name", help="optional human-readable session name")
    newp.add_argument("--model", default=DEFAULT_MODEL, help=f"default model (default: {DEFAULT_MODEL})")
    newp.add_argument("--system", help="optional default system instruction")
    usep = sess_sub.add_parser("use", help="select an existing session as active")
    usep.add_argument("--id", required=True, help="session id")
    resetp = sess_sub.add_parser("reset", help="clear session message history")
    resetp.add_argument("--id", help="session id (defaults to active session)")
    insp = sess_sub.add_parser("inspect", help="print session record")
    insp.add_argument("--id", help="session id (defaults to active session)")
    insp.add_argument("--format", choices=("text", "json"), default="text")
    p.set_defaults(
        file=None,
        json_data=None,
        no_open_oauth_guide=False,
        no_browser=False,
        force_login=False,
    )
    return p


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    parser = build_parser()
    args = parser.parse_args(argv)

    db_path = Path(args.db)

    if args.command == "init":
        try:
            wfb_home().mkdir(parents=True, exist_ok=True)
            if not ensure_client_secret_present(wfb_home()):
                print_oauth_setup_instructions(wfb_home())
                maybe_open_oauth_guide(args.no_open_oauth_guide)
                return EXIT_IO
            conn = connect_db(db_path)
            try:
                init_db(conn)
            finally:
                conn.close()
            try:
                ensure_logged_in(
                    wfb_home=wfb_home(),
                    no_browser=args.no_browser,
                    force_login=args.force_login,
                )
            except OAuthFlowError as e:
                _err(str(e))
                return EXIT_IO
        except sqlite3.Error as e:
            _err(str(e))
            return EXIT_DB
        return EXIT_OK

    if args.command == "gemini":
        try:
            if args.gemini_command == "ping":
                if args.limit < 0:
                    _err("--limit must be non-negative")
                    return EXIT_USAGE
                names = list_models(wfb_home=wfb_home())
                print(f"Models: {len(names)}")
                for name in names[: args.limit]:
                    print(f"- {name}")
                return EXIT_OK

            if args.gemini_command == "ask":
                if args.max_history_turns < 0:
                    _err("--max-history-turns must be non-negative")
                    return EXIT_USAGE

                sid = args.session or get_active_session_id(wfb_home())
                if sid:
                    sess = load_session(wfb_home(), sid)
                else:
                    sess = None
                if sess is None:
                    sess = create_session(
                        wfb_home(),
                        name=None,
                        model=args.model,
                        system=args.system,
                    )
                    sid = str(sess["id"])
                assert sid is not None
                set_active_session(wfb_home(), sid)

                model = args.model or str(sess.get("model", DEFAULT_MODEL))
                if model != sess.get("model"):
                    sess["model"] = model
                    save_session(wfb_home(), sess)
                system = args.system if args.system is not None else sess.get("system")

                history = sess.get("messages", [])
                if not isinstance(history, list):
                    history = []
                trimmed = history[-args.max_history_turns :] if args.max_history_turns else []
                normalized: list[dict[str, str]] = []
                for t in trimmed:
                    if isinstance(t, dict) and isinstance(t.get("role"), str) and isinstance(
                        t.get("text"), str
                    ):
                        normalized.append({"role": t["role"], "text": t["text"]})
                normalized.append({"role": "user", "text": args.prompt})

                answer = ask_with_messages(
                    wfb_home=wfb_home(),
                    model=model,
                    messages=normalized,
                    system=system if isinstance(system, str) else None,
                )
                append_turn(wfb_home(), sid, role="user", text=args.prompt)
                append_turn(wfb_home(), sid, role="model", text=answer)
                print(answer)
                return EXIT_OK

            if args.gemini_command == "session":
                if args.gemini_session_command == "current":
                    current = get_active_session_id(wfb_home())
                    if current is None:
                        print("No active session.")
                    else:
                        print(current)
                    st = api_managed_state_supported()
                    if not st.get("supported"):
                        print(f"api_managed_state_supported: no ({st.get('reason')})")
                    return EXIT_OK

                if args.gemini_session_command == "list":
                    sessions = list_sessions(wfb_home())
                    active = get_active_session_id(wfb_home())
                    if not sessions:
                        print("No local sessions.")
                        return EXIT_OK
                    for s in sessions:
                        sid = str(s.get("id", ""))
                        marker = "*" if sid == active else " "
                        name = str(s.get("name", sid))
                        model = str(s.get("model", DEFAULT_MODEL))
                        print(f"{marker} {sid}\t{name}\tmodel={model}")
                    return EXIT_OK

                if args.gemini_session_command == "new":
                    sess = create_session(
                        wfb_home(),
                        name=args.name,
                        model=args.model,
                        system=args.system,
                    )
                    print(str(sess["id"]))
                    return EXIT_OK

                if args.gemini_session_command == "use":
                    if load_session(wfb_home(), args.id) is None:
                        _err(f"session not found: {args.id}")
                        return EXIT_IO
                    set_active_session(wfb_home(), args.id)
                    print(args.id)
                    return EXIT_OK

                if args.gemini_session_command == "reset":
                    sid = args.id or get_active_session_id(wfb_home())
                    if not sid:
                        _err("no active session")
                        return EXIT_IO
                    if reset_session(wfb_home(), sid) is None:
                        _err(f"session not found: {sid}")
                        return EXIT_IO
                    print(f"reset {sid}")
                    return EXIT_OK

                if args.gemini_session_command == "inspect":
                    sid = args.id or get_active_session_id(wfb_home())
                    if not sid:
                        _err("no active session")
                        return EXIT_IO
                    sess = load_session(wfb_home(), sid)
                    if sess is None:
                        _err(f"session not found: {sid}")
                        return EXIT_IO
                    if args.format == "json":
                        print(json.dumps(sess, indent=2, sort_keys=True))
                    else:
                        print(f"id: {sess.get('id')}")
                        print(f"name: {sess.get('name')}")
                        print(f"model: {sess.get('model')}")
                        msgs = sess.get("messages", [])
                        print(f"messages: {len(msgs) if isinstance(msgs, list) else 0}")
                    return EXIT_OK
        except (GeminiApiError, OAuthFlowError) as e:
            _err(str(e))
            return EXIT_IO

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
