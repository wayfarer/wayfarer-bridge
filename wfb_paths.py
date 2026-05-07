"""Filesystem path helpers for the wfb CLI."""

from __future__ import annotations

from pathlib import Path


def wfb_home() -> Path:
    """Default directory for CLI-managed assets."""
    return Path.home() / ".wfb"


def default_db_path() -> Path:
    """Default SQLite store path under the CLI asset directory."""
    return wfb_home() / "wayfarer.db"
