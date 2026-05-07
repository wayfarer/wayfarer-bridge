"""Local Gemini session storage for agent-friendly chat continuity."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wfb_paths import gemini_active_session_path, gemini_sessions_dir


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sessions_dir(wfb_home: Path) -> Path:
    return gemini_sessions_dir(wfb_home)


def active_session_path(wfb_home: Path) -> Path:
    return gemini_active_session_path(wfb_home)


def _session_file(wfb_home: Path, session_id: str) -> Path:
    return sessions_dir(wfb_home) / f"{session_id}.json"


def create_session(wfb_home: Path, *, name: str | None, model: str, system: str | None) -> dict[str, Any]:
    sessions_dir(wfb_home).mkdir(parents=True, exist_ok=True)
    sid = f"sess_{secrets.token_hex(8)}"
    now = _now_iso()
    rec: dict[str, Any] = {
        "id": sid,
        "name": name or sid,
        "model": model,
        "system": system,
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }
    save_session(wfb_home, rec)
    set_active_session(wfb_home, sid)
    return rec


def save_session(wfb_home: Path, session: dict[str, Any]) -> Path:
    sessions_dir(wfb_home).mkdir(parents=True, exist_ok=True)
    session = dict(session)
    session["updated_at"] = _now_iso()
    p = _session_file(wfb_home, str(session["id"]))
    p.write_text(json.dumps(session, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return p


def load_session(wfb_home: Path, session_id: str) -> dict[str, Any] | None:
    p = _session_file(wfb_home, session_id)
    if not p.is_file():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return data
    return None


def list_sessions(wfb_home: Path) -> list[dict[str, Any]]:
    d = sessions_dir(wfb_home)
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out.append(data)
        except Exception:
            continue
    return out


def set_active_session(wfb_home: Path, session_id: str) -> Path:
    p = active_session_path(wfb_home)
    p.write_text(json.dumps({"session_id": session_id}, indent=2) + "\n", encoding="utf-8")
    return p


def get_active_session_id(wfb_home: Path) -> str | None:
    p = active_session_path(wfb_home)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict) and isinstance(data.get("session_id"), str):
        return data["session_id"]
    return None


def reset_session(wfb_home: Path, session_id: str) -> dict[str, Any] | None:
    sess = load_session(wfb_home, session_id)
    if sess is None:
        return None
    sess["messages"] = []
    save_session(wfb_home, sess)
    return sess


def append_turn(
    wfb_home: Path,
    session_id: str,
    *,
    role: str,
    text: str,
) -> dict[str, Any] | None:
    sess = load_session(wfb_home, session_id)
    if sess is None:
        return None
    msgs = sess.get("messages")
    if not isinstance(msgs, list):
        msgs = []
        sess["messages"] = msgs
    msgs.append({"role": role, "text": text, "ts": _now_iso()})
    save_session(wfb_home, sess)
    return sess


def session_message_stats(session: dict[str, Any]) -> dict[str, int]:
    messages = session.get("messages")
    if not isinstance(messages, list):
        return {"turns": 0, "chars": 0}
    turns = 0
    chars = 0
    for m in messages:
        if isinstance(m, dict) and isinstance(m.get("text"), str):
            turns += 1
            chars += len(m["text"])
    return {"turns": turns, "chars": chars}


def compact_session_history(
    wfb_home: Path,
    *,
    session_id: str,
    summary_text: str,
    source_model: str,
    keep_recent_turns: int,
) -> dict[str, Any] | None:
    sess = load_session(wfb_home, session_id)
    if sess is None:
        return None
    compacted = compacted_session_copy(
        sess,
        summary_text=summary_text,
        source_model=source_model,
        keep_recent_turns=keep_recent_turns,
    )
    save_session(wfb_home, compacted)
    return compacted


def compacted_session_copy(
    session: dict[str, Any],
    *,
    summary_text: str,
    source_model: str,
    keep_recent_turns: int,
) -> dict[str, Any]:
    """Return a compacted session object without persisting it."""
    sess = dict(session)
    messages = sess.get("messages")
    if not isinstance(messages, list):
        messages = []
    if keep_recent_turns < 0:
        keep_recent_turns = 0
    if keep_recent_turns >= len(messages):
        return sess

    old_count = len(messages) - keep_recent_turns
    older = messages[:old_count]
    recent = messages[old_count:]
    summary_msg: dict[str, Any] = {
        "role": "model",
        "text": summary_text,
        "ts": _now_iso(),
        "kind": "history_summary",
        "summary_meta": {
            "covered_turn_count": len(older),
            "source_model": source_model,
            "compacted_at": _now_iso(),
        },
    }
    sess["messages"] = [summary_msg, *recent]
    return sess
