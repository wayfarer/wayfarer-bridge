"""OAuth onboarding and login helpers for the `wfb` CLI (stdlib-only)."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

OAUTH_GUIDE_URL = "https://ai.google.dev/gemini-api/docs/oauth"
DEFAULT_OAUTH_SCOPE = "https://www.googleapis.com/auth/generative-language.retriever"


class OAuthFlowError(Exception):
    """Raised when the local OAuth installed-app flow cannot complete."""


def client_secret_path(wfb_home: Path) -> Path:
    """Local OAuth desktop client secret location for OSS/PyPI onboarding."""
    return wfb_home / "client_secret.json"


def token_path(wfb_home: Path) -> Path:
    """Local OAuth token storage location."""
    return wfb_home / "token.json"


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


def _urlsafe_b64_nopad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _pkce_verifier() -> str:
    return _urlsafe_b64_nopad(secrets.token_bytes(32))


def _pkce_challenge(verifier: str) -> str:
    return _urlsafe_b64_nopad(hashlib.sha256(verifier.encode("ascii")).digest())


def _oauth_state() -> str:
    return _urlsafe_b64_nopad(secrets.token_bytes(24))


def load_client_config(wfb_home: Path) -> dict[str, str | list[str]]:
    """Load required desktop OAuth client fields from client_secret.json."""
    p = client_secret_path(wfb_home)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise OAuthFlowError(f"missing OAuth client secret: {p}") from e
    except json.JSONDecodeError as e:
        raise OAuthFlowError(f"invalid JSON in OAuth client secret: {e}") from e

    installed = raw.get("installed")
    if not isinstance(installed, dict):
        raise OAuthFlowError("client_secret.json must contain top-level 'installed' object")

    required = ("client_id", "client_secret", "auth_uri", "token_uri", "redirect_uris")
    for k in required:
        if k not in installed:
            raise OAuthFlowError(f"client_secret.json missing installed.{k}")

    if not isinstance(installed["redirect_uris"], list) or not installed["redirect_uris"]:
        raise OAuthFlowError("installed.redirect_uris must be a non-empty array")

    return installed


def save_token(wfb_home: Path, token: dict[str, object]) -> None:
    """Persist token JSON under ~/.wfb/token.json with restrictive permissions."""
    path = token_path(wfb_home)
    now = int(time.time())
    token = dict(token)
    token["created_at"] = now
    expires_in = token.get("expires_in")
    if isinstance(expires_in, int):
        token["expires_at"] = now + expires_in

    serialized = json.dumps(token, indent=2, sort_keys=True)
    path.write_text(serialized + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Best effort on non-POSIX or restricted filesystems.
        pass


def load_token(wfb_home: Path) -> dict[str, object] | None:
    """Load persisted token JSON, if present and parseable."""
    path = token_path(wfb_home)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise OAuthFlowError(f"invalid token file at {path}: {e}") from e
    if not isinstance(raw, dict):
        raise OAuthFlowError(f"invalid token file at {path}: expected JSON object")
    return raw


def token_is_valid(token: dict[str, object], skew_seconds: int = 60) -> bool:
    access_token = token.get("access_token")
    expires_at = token.get("expires_at")
    if not isinstance(access_token, str) or not access_token:
        return False
    if isinstance(expires_at, int):
        return expires_at > int(time.time()) + skew_seconds
    return False


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        return int(s.getsockname()[1])


def _run_callback_server(expected_state: str, timeout_seconds: int) -> str:
    """Wait for one callback and return authorization code."""
    port = _free_loopback_port()
    redirect_uri = f"http://127.0.0.1:{port}/oauth/callback"
    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            state = params.get("state", [None])[0]
            code = params.get("code", [None])[0]
            error = params.get("error", [None])[0]
            if error:
                captured["error"] = str(error)
            if state is not None:
                captured["state"] = str(state)
            if code is not None:
                captured["code"] = str(code)

            if parsed.path == "/oauth/callback" and "code" in captured:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Authentication complete. You can close this tab.")
            else:
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(b"Authentication failed. Return to your terminal.")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = timeout_seconds
    try:
        server.handle_request()
    finally:
        server.server_close()

    if "error" in captured:
        raise OAuthFlowError(f"OAuth authorization failed: {captured['error']}")
    if captured.get("state") != expected_state:
        raise OAuthFlowError("OAuth state mismatch; aborting login")
    code = captured.get("code")
    if not code:
        raise OAuthFlowError("OAuth callback timed out or returned no code")
    return code


def _exchange_code_for_token(
    *,
    token_uri: str,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict[str, object]:
    form = urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }
    ).encode("utf-8")
    req = Request(
        token_uri,
        data=form,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise OAuthFlowError(f"token exchange failed: {e}") from e
    if not isinstance(payload, dict) or "access_token" not in payload:
        raise OAuthFlowError("token exchange failed: missing access_token in response")
    return payload


def ensure_logged_in(
    *,
    wfb_home: Path,
    no_browser: bool,
    force_login: bool,
    timeout_seconds: int = 120,
) -> dict[str, object]:
    """
    Ensure a usable access token exists.

    Returns token payload from cache or fresh login.
    """
    existing = load_token(wfb_home)
    if existing and not force_login and token_is_valid(existing):
        return existing

    conf = load_client_config(wfb_home)
    client_id = str(conf["client_id"])
    client_secret = str(conf["client_secret"])
    auth_uri = str(conf["auth_uri"])
    token_uri = str(conf["token_uri"])

    state = _oauth_state()
    verifier = _pkce_verifier()
    challenge = _pkce_challenge(verifier)

    port = _free_loopback_port()
    redirect_uri = f"http://127.0.0.1:{port}/oauth/callback"
    auth_params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": DEFAULT_OAUTH_SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    }
    auth_url = f"{auth_uri}?{urlencode(auth_params)}"
    print("Open this URL to authenticate:", file=sys.stderr)
    print(auth_url, file=sys.stderr)
    if not no_browser:
        try:
            webbrowser.open(auth_url)
        except Exception:
            pass

    # Re-bind callback server to chosen port now (avoid races between URL and listener startup)
    captured: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            if parsed.path != "/oauth/callback":
                self.send_response(404)
                self.end_headers()
                return
            if "error" in params:
                captured["error"] = str(params["error"][0])
            if "state" in params:
                captured["state"] = str(params["state"][0])
            if "code" in params:
                captured["code"] = str(params["code"][0])
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Authentication complete. You can close this tab.")

        def log_message(self, format: str, *args: object) -> None:  # noqa: A003
            return

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = timeout_seconds
    try:
        server.handle_request()
    finally:
        server.server_close()

    if "error" in captured:
        raise OAuthFlowError(f"OAuth authorization failed: {captured['error']}")
    if captured.get("state") != state:
        raise OAuthFlowError("OAuth state mismatch; aborting login")
    code = captured.get("code")
    if not code:
        raise OAuthFlowError("OAuth callback timed out or returned no code")

    token = _exchange_code_for_token(
        token_uri=token_uri,
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        redirect_uri=redirect_uri,
        code_verifier=verifier,
    )
    save_token(wfb_home, token)
    return token
