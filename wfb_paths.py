"""Filesystem path helpers for the wfb CLI."""

from __future__ import annotations

from pathlib import Path


def wfb_home() -> Path:
    """Default directory for CLI-managed assets."""
    return Path.home() / ".wfb"


def default_db_path() -> Path:
    """Default SQLite store path under the CLI asset directory."""
    return wfb_home() / "wayfarer.db"


def gemini_sessions_dir(home: Path | None = None) -> Path:
    """Directory for local Gemini chat session records."""
    base = home if home is not None else wfb_home()
    return base / "gemini_sessions"


def gemini_active_session_path(home: Path | None = None) -> Path:
    """Pointer file for currently active Gemini local session."""
    base = home if home is not None else wfb_home()
    return base / "gemini_active_session.json"


def chrome_bridge_profile_dir(home: Path | None = None) -> Path:
    """Default isolated Chrome profile directory for remote debugging."""
    base = home if home is not None else wfb_home()
    return base / "chrome_debug_profile"


def chrome_bridge_attachment_path(home: Path | None = None) -> Path:
    """Persisted selected Chrome target for bridge inspect calls."""
    base = home if home is not None else wfb_home()
    return base / "chrome_attachment.json"
