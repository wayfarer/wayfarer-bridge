"""OAuth onboarding helpers for the `wfb` CLI (stdlib-only)."""

from __future__ import annotations

import sys
import webbrowser
from pathlib import Path

OAUTH_GUIDE_URL = "https://ai.google.dev/gemini-api/docs/oauth"


def client_secret_path(wfb_home: Path) -> Path:
    """Local OAuth desktop client secret location for OSS/PyPI onboarding."""
    return wfb_home / "client_secret.json"


def print_oauth_setup_instructions(wfb_home: Path) -> None:
    """Print deterministic onboarding instructions to stderr."""
    secret_path = client_secret_path(wfb_home)
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


def ensure_client_secret_present(wfb_home: Path) -> bool:
    """True when required local client secret file exists."""
    return client_secret_path(wfb_home).is_file()


def maybe_open_oauth_guide(disabled: bool) -> None:
    """Best-effort open; never raises or changes CLI control flow."""
    if disabled:
        return
    try:
        webbrowser.open(OAUTH_GUIDE_URL)
    except Exception:
        pass
