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
from wfb_chrome_bridge import (
    DEFAULT_DEBUG_PORT,
    ChromeBridgeError,
    choose_target,
    detect_debug_ports,
    fetch_version,
    inspect_target,
    launch_chrome_debug,
    list_targets,
    parse_target_types,
    select_capture_target,
)
from wfb_chrome_session import clear_attachment, load_attachment as load_chrome_attachment, save_attachment
from wfb_db import UPDATED_AT_SQL, connect_db, init_db, require_v1_schema
from wfb_gemini_api import (
    DEFAULT_MODEL,
    GeminiApiError,
    api_managed_state_supported,
    ask_with_messages,
    extract_world_state_envelope,
    list_models,
    summarization_policy_for_model,
    summarize_messages,
)
from wfb_gemini_sessions import (
    append_turn,
    compacted_session_copy,
    create_session,
    get_active_session_id,
    list_sessions,
    load_session,
    reset_session,
    save_session,
    session_message_stats,
    set_active_session,
    update_world_state_sync,
    world_state_sync_enabled,
)
from wfb_oauth import (
    ensure_client_secret_present,
    ensure_logged_in,
    maybe_open_oauth_guide,
    print_oauth_setup_instructions,
    OAuthFlowError,
)
from wfb_paths import chrome_bridge_profile_dir, default_db_path, wfb_home

# --- Exit codes (README) ---
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VALIDATION = 3
EXIT_DB = 4
EXIT_IO = 5

DEFAULT_STATUS_LIMIT = 5
AGENT_WORKFLOW_GUIDANCE = """Agent workflow guidance:
  1) Initialize once: `wfb init`
  2) For browser-context capture:
       - `wfb chrome targets --include-types page,webview --gemini-only`
       - `wfb chrome attach --target-id <id> --include-types page,webview`
       - `wfb chrome inspect --include-types page,webview --format json`
  3) For durable local memory and model control:
       - create/select session with `wfb gemini session new|use`
       - run asks with `wfb gemini ask --session <id> ...`
  4) State ownership:
       - browser panel text = live context source
       - local gemini session = durable agent execution history
  5) Optional persistence:
       - enable `--sync-world-state on` when chat context should update SQLite world state.
"""

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


def _warn_world_state_sync(msg: str) -> None:
    print(f"world-state sync skipped: {msg}", file=sys.stderr)


def _chrome_recovery_hint(port: int) -> str:
    return (
        f"next steps: try `wfb chrome launch --port {port}` or "
        f"`wfb chrome targets --port {port} --include-types page,webview --gemini-only`"
    )


def _chrome_current_payload(home: Path, fallback_port: int = DEFAULT_DEBUG_PORT) -> dict[str, Any]:
    attachment = load_chrome_attachment(home)
    payload: dict[str, Any] = {
        "attached": attachment is not None,
        "attachment": attachment,
        "endpoint": {"reachable": False, "port": None, "browser": None},
        "target_present": None,
    }
    if attachment is None:
        return payload
    saved_port = attachment.get("debug_port")
    port = int(saved_port) if isinstance(saved_port, int) else fallback_port
    payload["endpoint"]["port"] = port
    try:
        version = fetch_version(port=port)
        payload["endpoint"]["reachable"] = True
        payload["endpoint"]["browser"] = str(version.get("Browser", ""))
    except ChromeBridgeError:
        payload["endpoint"]["reachable"] = False
        return payload
    target_id = str(attachment.get("target_id", ""))
    targets = list_targets(port=port, include_types=("page", "webview"))
    payload["target_present"] = any(str(t.get("id", "")) == target_id for t in targets)
    return payload


def _list_targets_with_port_fallback(
    *,
    port: int,
    include_types: tuple[str, ...],
    gemini_only: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    try:
        targets = list_targets(port=port, include_types=include_types, gemini_only=gemini_only)
        return targets, port
    except ChromeBridgeError:
        detected = detect_debug_ports()
        for entry in detected:
            resolved = int(entry["port"])
            if resolved == port:
                continue
            try:
                targets = list_targets(
                    port=resolved,
                    include_types=include_types,
                    gemini_only=gemini_only,
                )
                return targets, resolved
            except ChromeBridgeError:
                continue
        raise


def _annotate_sync_envelope(
    envelope: dict[str, Any], *, session_id: str, scope: str | None
) -> dict[str, Any]:
    out = dict(envelope)
    out["source"] = f"gemini_session:{session_id}"
    for key in ("active_tasks", "environmental_constraints", "style_specifications"):
        rows = out.get(key)
        if not isinstance(rows, list):
            continue
        new_rows: list[dict[str, Any]] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            row = dict(r)
            md = row.get("metadata")
            if not isinstance(md, dict):
                md = {}
            md = dict(md)
            md["origin_session_id"] = session_id
            if isinstance(scope, str) and scope:
                md["world_state_scope"] = scope
            row["metadata"] = md
            row["source"] = out["source"]
            new_rows.append(row)
        out[key] = new_rows
    return out


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
    p = argparse.ArgumentParser(
        prog="wfb",
        description="Wayfarer Bridge v1 CLI",
        epilog=AGENT_WORKFLOW_GUIDANCE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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

    gem = sub.add_parser(
        "gemini",
        help="call Gemini APIs using local OAuth token",
        description=(
            "Gemini API path for deterministic model execution and local state.\n"
            "Use sessions (`gemini session`) when agents need durable memory."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    gem_sub = gem.add_subparsers(dest="gemini_command", required=True)

    ping = gem_sub.add_parser("ping", help="list available Gemini models")
    ping.add_argument(
        "--limit",
        type=int,
        default=10,
        metavar="N",
        help="max model names to print (default: 10)",
    )

    ask = gem_sub.add_parser(
        "ask",
        help="run a single text prompt",
        description=(
            "Send a prompt with local session continuity.\n"
            "For agent workflows, prefer explicit `--session` routing.\n"
            "Browser-panel content can be captured first via `wfb chrome inspect`."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
    ask.add_argument(
        "--auto-summarize",
        choices=("on", "off"),
        default="off",
        help="auto-compact long session history before ask (default: off)",
    )
    ask.add_argument(
        "--summarize-model",
        help="optional model override used only for history summarization",
    )
    ask.add_argument(
        "--sync-world-state",
        choices=("on", "off"),
        default=None,
        help="override session world-state sync mode for this ask",
    )
    ask.add_argument(
        "--world-state-db",
        help="override target world-state DB path for this ask",
    )

    sess = gem_sub.add_parser(
        "session",
        help="manage local Gemini chat sessions",
        description=(
            "Manage durable local session state used by `wfb gemini ask`.\n"
            "Use `new`/`use` to avoid accidental cross-task history mixing."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sess_sub = sess.add_subparsers(dest="gemini_session_command", required=True)
    sess_sub.add_parser("current", help="show active session id")
    sess_sub.add_parser("list", help="list local sessions")
    newp = sess_sub.add_parser("new", help="create and select a new session")
    newp.add_argument("--name", help="optional human-readable session name")
    newp.add_argument("--model", default=DEFAULT_MODEL, help=f"default model (default: {DEFAULT_MODEL})")
    newp.add_argument("--system", help="optional default system instruction")
    newp.add_argument(
        "--sync-world-state",
        choices=("on", "off"),
        default=None,
        help="default world-state sync mode for this session",
    )
    newp.add_argument("--world-state-db", help="default world-state DB path for this session")
    newp.add_argument("--world-state-scope", help="optional scope tag for synced records")
    usep = sess_sub.add_parser("use", help="select an existing session as active")
    usep.add_argument("--id", required=True, help="session id")
    usep.add_argument(
        "--sync-world-state",
        choices=("on", "off"),
        default=None,
        help="update world-state sync mode while selecting the session",
    )
    usep.add_argument("--world-state-db", help="update default world-state DB path for this session")
    usep.add_argument("--world-state-scope", help="update default world-state scope for this session")
    resetp = sess_sub.add_parser("reset", help="clear session message history")
    resetp.add_argument("--id", help="session id (defaults to active session)")
    insp = sess_sub.add_parser("inspect", help="print session record")
    insp.add_argument("--id", help="session id (defaults to active session)")
    insp.add_argument("--format", choices=("text", "json"), default="text")

    chrome = sub.add_parser(
        "chrome",
        help="Chrome remote debugging bridge commands",
        description=(
            "Browser-context capture path.\n"
            "Use `--include-types page,webview` to include Gemini side-panel targets."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    chrome_sub = chrome.add_subparsers(dest="chrome_command", required=True)

    c_launch = chrome_sub.add_parser("launch", help="launch or verify debuggable Chrome")
    c_launch.add_argument(
        "--port",
        type=int,
        default=DEFAULT_DEBUG_PORT,
        help=f"remote debugging port (default: {DEFAULT_DEBUG_PORT})",
    )
    c_launch.add_argument(
        "--profile-mode",
        choices=("isolated", "user"),
        default="isolated",
        help="profile strategy (default: isolated)",
    )
    c_launch.add_argument("--chrome-path", help="explicit Chrome executable path")
    c_launch.add_argument(
        "--timeout-seconds",
        type=float,
        default=12.0,
        help="launch/verify timeout seconds; launch is skipped if endpoint already exists (default: 12.0)",
    )
    c_launch.add_argument("--format", choices=("text", "json"), default="text")

    c_targets = chrome_sub.add_parser("targets", help="list attachable Chrome page targets")
    c_targets.add_argument(
        "--port",
        type=int,
        default=DEFAULT_DEBUG_PORT,
        help=f"remote debugging port (default: {DEFAULT_DEBUG_PORT})",
    )
    c_targets.add_argument("--format", choices=("text", "json"), default="text")
    c_targets.add_argument(
        "--include-types",
        default="page",
        help="comma-separated target types to include (default: page)",
    )
    c_targets.add_argument(
        "--gemini-only",
        action="store_true",
        help="show only Gemini side-panel related targets",
    )

    c_attach = chrome_sub.add_parser("attach", help="persist selected Chrome target")
    c_attach.add_argument("--target-id", required=True, help="target id from chrome targets output")
    c_attach.add_argument(
        "--port",
        type=int,
        default=DEFAULT_DEBUG_PORT,
        help=f"remote debugging port (default: {DEFAULT_DEBUG_PORT})",
    )
    c_attach.add_argument("--format", choices=("text", "json"), default="text")
    c_attach.add_argument(
        "--include-types",
        default="page",
        help="comma-separated target types to search (default: page)",
    )

    c_inspect = chrome_sub.add_parser("inspect", help="inspect attached Chrome target content")
    c_inspect.add_argument("--target-id", help="override persisted target id for this call")
    c_inspect.add_argument("--port", type=int, help="override debug port")
    c_inspect.add_argument(
        "--timeout-seconds",
        type=float,
        default=5.0,
        help="CDP request timeout seconds (default: 5.0)",
    )
    c_inspect.add_argument(
        "--max-chars",
        type=int,
        default=4000,
        help="max snapshot text chars (default: 4000)",
    )
    c_inspect.add_argument("--format", choices=("text", "json"), default="json")
    c_inspect.add_argument(
        "--include-types",
        default="page",
        help="comma-separated target types to search when resolving ids (default: page)",
    )

    c_detach = chrome_sub.add_parser("detach", help="clear persisted Chrome target attachment")
    c_detach.add_argument("--format", choices=("text", "json"), default="text")
    c_current = chrome_sub.add_parser("current", help="show current attached target and endpoint health")
    c_current.add_argument("--format", choices=("json", "text"), default="json")
    c_capture = chrome_sub.add_parser("capture", help="discover, attach, and inspect in one command")
    c_capture.add_argument("--target-id", help="explicit target id override")
    c_capture.add_argument(
        "--port",
        type=int,
        default=DEFAULT_DEBUG_PORT,
        help=f"remote debugging port (default: {DEFAULT_DEBUG_PORT})",
    )
    c_capture.add_argument(
        "--include-types",
        default="page,webview",
        help="comma-separated target types to consider (default: page,webview)",
    )
    c_capture.add_argument("--gemini-only", action="store_true", help="restrict capture candidates to Gemini targets")
    c_capture.add_argument(
        "--max-chars",
        type=int,
        default=4000,
        help="max snapshot text chars (default: 4000)",
    )
    c_capture.add_argument("--format", choices=("json", "text"), default="json")
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
                pending_compacted: dict[str, Any] | None = None

                if args.auto_summarize == "on":
                    policy = summarization_policy_for_model(model)
                    stats = session_message_stats(sess)
                    max_turns = int(policy["max_turns"])
                    max_chars = int(policy["max_chars"])
                    keep_recent_turns = int(policy["keep_recent_turns"])
                    should_compact = stats["turns"] > max_turns or stats["chars"] > max_chars
                    if should_compact:
                        summarize_model = args.summarize_model or model
                        if keep_recent_turns >= len(history):
                            keep_recent_turns = max(1, len(history) // 2)
                        older: list[dict[str, str]] = []
                        for t in history[: max(0, len(history) - keep_recent_turns)]:
                            if isinstance(t, dict) and isinstance(t.get("role"), str) and isinstance(
                                t.get("text"), str
                            ):
                                older.append({"role": t["role"], "text": t["text"]})
                        if older:
                            summary = summarize_messages(
                                wfb_home=wfb_home(),
                                model=summarize_model,
                                messages=older,
                            )
                            updated = compacted_session_copy(
                                sess,
                                summary_text=summary,
                                source_model=summarize_model,
                                keep_recent_turns=keep_recent_turns,
                            )
                            pending_compacted = updated
                            sess = updated
                            history = sess.get("messages", [])
                            if not isinstance(history, list):
                                history = []

                summary_msgs: list[dict[str, str]] = []
                non_summary_msgs: list[dict[str, str]] = []
                for t in history:
                    if not (isinstance(t, dict) and isinstance(t.get("role"), str) and isinstance(t.get("text"), str)):
                        continue
                    role = t["role"]
                    if role == "system":
                        # Backward compatibility for earlier summary artifacts.
                        role = "model"
                    normalized_turn = {"role": role, "text": t["text"]}
                    if t.get("kind") == "history_summary":
                        summary_msgs.append(normalized_turn)
                    else:
                        non_summary_msgs.append(normalized_turn)

                trimmed_non_summary = (
                    non_summary_msgs[-args.max_history_turns :] if args.max_history_turns else []
                )
                normalized: list[dict[str, str]] = []
                normalized.extend(summary_msgs)
                normalized.extend(trimmed_non_summary)
                normalized.append({"role": "user", "text": args.prompt})

                answer = ask_with_messages(
                    wfb_home=wfb_home(),
                    model=model,
                    messages=normalized,
                    system=system if isinstance(system, str) else None,
                )
                if pending_compacted is not None:
                    save_session(wfb_home(), pending_compacted)
                append_turn(wfb_home(), sid, role="user", text=args.prompt)
                append_turn(wfb_home(), sid, role="model", text=answer)

                sync_on = (
                    args.sync_world_state
                    if args.sync_world_state is not None
                    else ("on" if world_state_sync_enabled(sess) else "off")
                )
                if sync_on == "on":
                    target_db_path = args.world_state_db or sess.get("world_state_db_path") or str(db_path)
                    scope = sess.get("world_state_scope")
                    try:
                        extraction = extract_world_state_envelope(
                            wfb_home=wfb_home(),
                            model=model,
                            session_id=sid,
                            messages=[*normalized, {"role": "model", "text": answer}],
                        )
                        annotated = _annotate_sync_envelope(
                            extraction,
                            session_id=sid,
                            scope=scope if isinstance(scope, str) else None,
                        )
                        normalized_env = validate_envelope(annotated)
                        sync_conn = connect_db(target_db_path)
                        try:
                            require_v1_schema(sync_conn)
                            seed_db(sync_conn, normalized_env, replace=False)
                        finally:
                            sync_conn.close()
                    except (GeminiApiError, ValidationError, sqlite3.Error, OSError) as e:
                        _warn_world_state_sync(str(e))

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
                    if (
                        args.sync_world_state is not None
                        or args.world_state_db is not None
                        or args.world_state_scope is not None
                    ):
                        updated = update_world_state_sync(
                            wfb_home(),
                            session_id=str(sess["id"]),
                            sync_mode=args.sync_world_state,
                            db_path=args.world_state_db,
                            scope=args.world_state_scope,
                        )
                        if updated is not None:
                            sess = updated
                    print(str(sess["id"]))
                    return EXIT_OK

                if args.gemini_session_command == "use":
                    if load_session(wfb_home(), args.id) is None:
                        _err(f"session not found: {args.id}")
                        return EXIT_IO
                    set_active_session(wfb_home(), args.id)
                    if (
                        args.sync_world_state is not None
                        or args.world_state_db is not None
                        or args.world_state_scope is not None
                    ):
                        updated = update_world_state_sync(
                            wfb_home(),
                            session_id=args.id,
                            sync_mode=args.sync_world_state,
                            db_path=args.world_state_db,
                            scope=args.world_state_scope,
                        )
                        if updated is None:
                            _err(f"session not found: {args.id}")
                            return EXIT_IO
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
                        if isinstance(msgs, list):
                            summary_count = 0
                            for m in msgs:
                                if isinstance(m, dict) and m.get("kind") == "history_summary":
                                    summary_count += 1
                            print(f"summaries: {summary_count}")
                    return EXIT_OK
        except (GeminiApiError, OAuthFlowError) as e:
            _err(str(e))
            return EXIT_IO

    if args.command == "chrome":
        try:
            if args.chrome_command == "launch":
                if args.port <= 0:
                    _err("--port must be positive")
                    return EXIT_USAGE
                if args.timeout_seconds <= 0:
                    _err("--timeout-seconds must be positive")
                    return EXIT_USAGE
                wfb_home().mkdir(parents=True, exist_ok=True)
                profile_dir = None
                if args.profile_mode == "isolated":
                    profile_dir = str(chrome_bridge_profile_dir(wfb_home()))
                payload = launch_chrome_debug(
                    port=args.port,
                    profile_mode=args.profile_mode,
                    profile_dir=profile_dir,
                    chrome_path=args.chrome_path,
                    timeout_seconds=args.timeout_seconds,
                )
                if args.format == "json":
                    print(json.dumps(payload, indent=2, sort_keys=True))
                else:
                    browser = payload.get("Browser", "unknown")
                    resolved = int(payload.get("resolved_port", args.port))
                    print(f"Chrome debug ready on port {resolved}")
                    print(f"Browser: {browser}")
                    print(f"profile_mode: {args.profile_mode}")
                    print(f"already_running: {bool(payload.get('already_running', False))}")
                    print(f"fallback_used: {bool(payload.get('fallback_used', False))}")
                    if int(payload.get("requested_port", args.port)) != resolved:
                        print(f"requested_port: {args.port}")
                return EXIT_OK

            if args.chrome_command == "targets":
                if args.port <= 0:
                    _err("--port must be positive")
                    return EXIT_USAGE
                selected_types = parse_target_types(args.include_types)
                try:
                    targets = list_targets(
                        port=args.port,
                        include_types=selected_types,
                        gemini_only=bool(args.gemini_only),
                    )
                except ChromeBridgeError as e:
                    _err(f"{e}; {_chrome_recovery_hint(args.port)}")
                    return EXIT_IO
                if args.format == "json":
                    out = []
                    for t in targets:
                        out.append(
                            {
                                "id": t.get("id"),
                                "title": t.get("title"),
                                "url": t.get("url"),
                                "type": t.get("type"),
                                "webSocketDebuggerUrl": t.get("webSocketDebuggerUrl"),
                            }
                        )
                    print(json.dumps(out, indent=2, sort_keys=True))
                else:
                    if not targets:
                        print("No matching targets.")
                    for t in targets:
                        print(
                            f"{t.get('id','')}\t{t.get('title','')}\t{t.get('url','')}\ttype={t.get('type','')}"
                        )
                return EXIT_OK

            if args.chrome_command == "attach":
                if args.port <= 0:
                    _err("--port must be positive")
                    return EXIT_USAGE
                selected_types = parse_target_types(args.include_types)
                try:
                    targets = list_targets(port=args.port, include_types=selected_types)
                except ChromeBridgeError as e:
                    _err(f"{e}; {_chrome_recovery_hint(args.port)}")
                    return EXIT_IO
                try:
                    target = choose_target(targets, args.target_id)
                except ChromeBridgeError as e:
                    _err(f"{e}; try --include-types page,webview; {_chrome_recovery_hint(args.port)}")
                    return EXIT_IO
                ws_url = str(target.get("webSocketDebuggerUrl", ""))
                if not ws_url:
                    _err(f"target missing websocket debugger url: {args.target_id}")
                    return EXIT_IO
                payload = save_attachment(
                    wfb_home(),
                    target_id=str(target.get("id", "")),
                    ws_url=ws_url,
                    url=str(target.get("url", "")),
                    title=str(target.get("title", "")),
                    debug_port=args.port,
                )
                if args.format == "json":
                    print(json.dumps(payload, indent=2, sort_keys=True))
                else:
                    print(f"attached {payload['target_id']}")
                return EXIT_OK

            if args.chrome_command == "inspect":
                if args.max_chars <= 0:
                    _err("--max-chars must be positive")
                    return EXIT_USAGE
                if args.timeout_seconds <= 0:
                    _err("--timeout-seconds must be positive")
                    return EXIT_USAGE
                target: dict[str, Any] | None = None
                ws_url = ""
                target_id = args.target_id
                port = args.port
                targets: list[dict[str, Any]] = []
                selected_types = parse_target_types(args.include_types)

                if target_id:
                    resolved_port = port if port is not None else DEFAULT_DEBUG_PORT
                    try:
                        targets = list_targets(port=resolved_port, include_types=selected_types)
                    except ChromeBridgeError as e:
                        _err(f"{e}; {_chrome_recovery_hint(resolved_port)}")
                        return EXIT_IO
                    try:
                        target = choose_target(targets, target_id)
                    except ChromeBridgeError as e:
                        _err(
                            f"{e}; try --include-types page,webview; "
                            f"{_chrome_recovery_hint(resolved_port)}"
                        )
                        return EXIT_IO
                    ws_url = str(target.get("webSocketDebuggerUrl", ""))
                    port = resolved_port
                else:
                    attachment = load_chrome_attachment(wfb_home())
                    if attachment is None:
                        _err("no attached Chrome target; run `wfb chrome attach --target-id ...`")
                        return EXIT_IO
                    ws_url = str(attachment.get("webSocketDebuggerUrl", ""))
                    target_id = str(attachment.get("target_id", ""))
                    if port is None:
                        saved_port = attachment.get("debug_port")
                        if isinstance(saved_port, int):
                            port = saved_port
                        else:
                            port = DEFAULT_DEBUG_PORT
                    try:
                        targets = list_targets(port=port, include_types=selected_types)
                    except ChromeBridgeError as e:
                        _err(f"{e}; {_chrome_recovery_hint(port)}")
                        return EXIT_IO
                    try:
                        target = choose_target(targets, target_id)
                    except ChromeBridgeError as e:
                        _err(f"{e}; try --include-types page,webview; {_chrome_recovery_hint(port)}")
                        return EXIT_IO

                if not ws_url:
                    _err("target has no websocket debugger url")
                    return EXIT_IO

                context = inspect_target(
                    ws_url=ws_url,
                    timeout_seconds=args.timeout_seconds,
                    max_chars=args.max_chars,
                )
                if target is not None:
                    context["target"] = {
                        "id": str(target.get("id", "")),
                        "title": str(target.get("title", "")),
                        "url": str(target.get("url", "")),
                        "type": str(target.get("type", "")),
                    }
                if port is not None:
                    context["debug_port"] = int(port)
                if args.format == "json":
                    print(json.dumps(context, indent=2, sort_keys=True))
                else:
                    print(f"title: {context.get('title','')}")
                    print(f"url: {context.get('url','')}")
                    print(f"text_snapshot_chars: {context.get('text_snapshot_chars', 0)}")
                    print(context.get("text_snapshot", ""))
                return EXIT_OK

            if args.chrome_command == "detach":
                removed = clear_attachment(wfb_home())
                payload = {"detached": removed}
                if args.format == "json":
                    print(json.dumps(payload, indent=2, sort_keys=True))
                else:
                    if removed:
                        print("detached")
                    else:
                        print("no attachment")
                return EXIT_OK

            if args.chrome_command == "current":
                payload = _chrome_current_payload(wfb_home())
                if args.format == "json":
                    print(json.dumps(payload, indent=2, sort_keys=True))
                else:
                    if not payload.get("attached"):
                        print("no attachment")
                        return EXIT_OK
                    attachment = payload.get("attachment", {}) if isinstance(payload.get("attachment"), dict) else {}
                    endpoint = payload.get("endpoint", {}) if isinstance(payload.get("endpoint"), dict) else {}
                    print(f"target_id: {attachment.get('target_id','')}")
                    print(f"title: {attachment.get('title','')}")
                    print(f"url: {attachment.get('url','')}")
                    print(f"debug_port: {endpoint.get('port')}")
                    print(f"endpoint_reachable: {bool(endpoint.get('reachable', False))}")
                    print(f"target_present: {payload.get('target_present')}")
                return EXIT_OK

            if args.chrome_command == "capture":
                if args.port <= 0:
                    _err("--port must be positive")
                    return EXIT_USAGE
                if args.max_chars <= 0:
                    _err("--max-chars must be positive")
                    return EXIT_USAGE
                selected_types = parse_target_types(args.include_types)
                try:
                    targets, resolved_port = _list_targets_with_port_fallback(
                        port=args.port,
                        include_types=selected_types,
                        gemini_only=bool(args.gemini_only),
                    )
                except ChromeBridgeError as e:
                    _err(f"{e}; {_chrome_recovery_hint(args.port)}")
                    return EXIT_IO
                try:
                    target, selection_method, selection_reason = select_capture_target(
                        targets,
                        target_id=args.target_id,
                    )
                except ChromeBridgeError as e:
                    _err(f"{e}; {_chrome_recovery_hint(resolved_port)}")
                    return EXIT_IO
                ws_url = str(target.get("webSocketDebuggerUrl", ""))
                if not ws_url:
                    _err("selected target missing websocket debugger url")
                    return EXIT_IO
                attachment = save_attachment(
                    wfb_home(),
                    target_id=str(target.get("id", "")),
                    ws_url=ws_url,
                    url=str(target.get("url", "")),
                    title=str(target.get("title", "")),
                    debug_port=resolved_port,
                )
                inspect_payload = inspect_target(
                    ws_url=ws_url,
                    max_chars=args.max_chars,
                )
                inspect_payload["target"] = {
                    "id": str(target.get("id", "")),
                    "title": str(target.get("title", "")),
                    "url": str(target.get("url", "")),
                    "type": str(target.get("type", "")),
                }
                inspect_payload["debug_port"] = int(resolved_port)
                payload = {
                    "selection": {"method": selection_method, "reason": selection_reason},
                    "target": inspect_payload["target"],
                    "attachment": attachment,
                    "inspect": inspect_payload,
                    "debug": {"requested_port": int(args.port), "resolved_port": int(resolved_port)},
                }
                if args.format == "json":
                    print(json.dumps(payload, indent=2, sort_keys=True))
                else:
                    print(f"selection_method: {selection_method}")
                    print(f"target_id: {payload['target']['id']}")
                    print(f"title: {payload['target']['title']}")
                    print(f"url: {payload['target']['url']}")
                    print(f"debug_port: {resolved_port}")
                    print(payload["inspect"].get("text_snapshot", ""))
                return EXIT_OK
        except ChromeBridgeError as e:
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
