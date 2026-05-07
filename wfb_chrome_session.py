"""Persisted Chrome bridge attachment state helpers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from wfb_paths import chrome_bridge_attachment_path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def save_attachment(
    home: Path,
    *,
    target_id: str,
    ws_url: str,
    url: str,
    title: str,
    debug_port: int,
) -> dict[str, Any]:
    payload = {
        "target_id": target_id,
        "webSocketDebuggerUrl": ws_url,
        "url": url,
        "title": title,
        "debug_port": int(debug_port),
        "attached_at": _now_iso(),
        "mode": "chrome_debug",
    }
    path = chrome_bridge_attachment_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def load_attachment(home: Path) -> dict[str, Any] | None:
    path = chrome_bridge_attachment_path(home)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        return payload
    return None


def clear_attachment(home: Path) -> bool:
    path = chrome_bridge_attachment_path(home)
    if not path.exists():
        return False
    path.unlink()
    return True
