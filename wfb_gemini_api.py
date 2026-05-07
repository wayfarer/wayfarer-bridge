"""Stdlib-only Gemini REST client for wfb."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from wfb_oauth import OAuthFlowError, load_client_config, load_token, save_token, token_is_valid

GEMINI_API_BASE = "https://generativelanguage.googleapis.com"
DEFAULT_MODEL = "gemini-2.5-flash"


class GeminiApiError(Exception):
    """Raised when a Gemini API request or token refresh fails."""


def summarization_policy_for_model(model: str) -> dict[str, object]:
    """
    Heuristic model-aware compaction policy for drift prevention.
    This is not a hard context-window limit; it intentionally compacts only
    at very long histories to reduce quality drift across many turns.
    """
    m = model.lower()
    if "flash-lite" in m:
        return {"max_turns": 120, "max_chars": 180000, "keep_recent_turns": 48}
    if "flash" in m:
        return {"max_turns": 160, "max_chars": 260000, "keep_recent_turns": 64}
    if "pro" in m:
        return {"max_turns": 220, "max_chars": 360000, "keep_recent_turns": 88}
    return {"max_turns": 140, "max_chars": 220000, "keep_recent_turns": 56}


def _token_error_hint(msg: str) -> GeminiApiError:
    return GeminiApiError(f"{msg}. Run `wfb init --force-login` to re-authenticate.")


def _ensure_token(wfb_home: Path) -> dict[str, object]:
    token = load_token(wfb_home)
    if token is None:
        raise _token_error_hint("missing token file at ~/.wfb/token.json")
    return token


def _refresh_access_token(
    *,
    wfb_home: Path,
    token: dict[str, object],
) -> dict[str, object]:
    refresh_token = token.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise _token_error_hint("missing refresh token in token.json")

    conf = load_client_config(wfb_home)
    token_uri = str(conf["token_uri"])
    client_id = str(conf["client_id"])
    client_secret = str(conf["client_secret"])

    form = urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
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
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise _token_error_hint(f"token refresh failed ({e.code}): {body[:400]}") from e
    except URLError as e:
        raise GeminiApiError(f"network error during token refresh: {e}") from e
    except Exception as e:
        raise GeminiApiError(f"unexpected token refresh failure: {e}") from e

    if not isinstance(payload, dict) or "access_token" not in payload:
        raise _token_error_hint("token refresh response missing access_token")

    merged = dict(token)
    merged.update(payload)
    if "refresh_token" not in payload:
        # Google often omits refresh_token in refresh responses.
        merged["refresh_token"] = refresh_token
    save_token(wfb_home, merged)
    return merged


def _get_access_token(wfb_home: Path) -> str:
    token = _ensure_token(wfb_home)
    if not token_is_valid(token):
        token = _refresh_access_token(wfb_home=wfb_home, token=token)
    access_token = token.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise _token_error_hint("token.json does not contain a valid access_token")
    return access_token


def _request_json(
    *,
    method: str,
    url: str,
    access_token: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = None
    headers = {"Authorization": f"Bearer {access_token}"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
    except HTTPError as e:
        text = ""
        try:
            text = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise GeminiApiError(f"Gemini API error {e.code}: {text[:500]}") from e
    except URLError as e:
        raise GeminiApiError(f"network error calling Gemini API: {e}") from e
    except json.JSONDecodeError as e:
        raise GeminiApiError(f"invalid JSON response from Gemini API: {e}") from e
    if not isinstance(data, dict):
        raise GeminiApiError("Gemini API returned non-object JSON response")
    return data


def list_models(*, wfb_home: Path) -> list[str]:
    access_token = _get_access_token(wfb_home)
    data = _request_json(
        method="GET",
        url=f"{GEMINI_API_BASE}/v1/models",
        access_token=access_token,
    )
    models = data.get("models")
    if not isinstance(models, list):
        raise GeminiApiError("models response missing 'models' array")
    names: list[str] = []
    for m in models:
        if isinstance(m, dict) and isinstance(m.get("name"), str):
            names.append(m["name"])
    return names


def ask_text(*, wfb_home: Path, prompt: str, model: str = DEFAULT_MODEL) -> str:
    return ask_with_messages(
        wfb_home=wfb_home,
        model=model,
        messages=[{"role": "user", "text": prompt}],
        system=None,
    )


def ask_with_messages(
    *,
    wfb_home: Path,
    model: str,
    messages: list[dict[str, str]],
    system: str | None,
) -> str:
    access_token = _get_access_token(wfb_home)
    contents = []
    for msg in messages:
        role = msg.get("role", "user")
        text = msg.get("text", "")
        if not isinstance(text, str) or not text.strip():
            continue
        contents.append({"role": role, "parts": [{"text": text}]})
    payload = {
        "contents": contents
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    data = _request_json(
        method="POST",
        url=f"{GEMINI_API_BASE}/v1beta/models/{model}:generateContent",
        access_token=access_token,
        payload=payload,
    )
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise GeminiApiError("generation response missing candidates")
    first = candidates[0]
    if not isinstance(first, dict):
        raise GeminiApiError("generation response has invalid candidate format")
    content = first.get("content")
    if not isinstance(content, dict):
        raise GeminiApiError("generation response missing candidate content")
    parts = content.get("parts")
    if not isinstance(parts, list) or not parts:
        raise GeminiApiError("generation response missing content parts")
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            return part["text"]
    raise GeminiApiError("generation response did not contain text output")


def summarize_messages(
    *,
    wfb_home: Path,
    model: str,
    messages: list[dict[str, str]],
) -> str:
    """
    Generate a compact summary for historical turns.
    Raises GeminiApiError on empty/malformed summarization output.
    """
    if not messages:
        raise GeminiApiError("cannot summarize empty message list")
    prompt_lines = [
        "Summarize the following prior conversation turns for memory compaction.",
        "Return plain text only.",
        "Preserve exact details when present: file paths, commands, errors, decisions, constraints, TODOs, IDs, and quoted strings.",
        "Use compact sections with prefixes: GOALS:, CONSTRAINTS:, DECISIONS:, OPEN_QUESTIONS:, KEY_DETAILS:.",
        "Avoid markdown and avoid inventing details.",
        "",
    ]
    for i, msg in enumerate(messages, start=1):
        role = msg.get("role", "user")
        text = msg.get("text", "")
        if isinstance(text, str) and text.strip():
            prompt_lines.append(f"{i}. {role}: {text}")
    summary_prompt = "\n".join(prompt_lines)
    out = ask_with_messages(
        wfb_home=wfb_home,
        model=model,
        messages=[{"role": "user", "text": summary_prompt}],
        system="You are a precise conversation summarizer for AI memory compaction.",
    )
    normalized = out.strip()
    if not normalized:
        raise GeminiApiError("summarization response was empty")
    return normalized


def api_managed_state_supported() -> dict[str, object]:
    """
    Current public REST usage provides no stable conversation/session handle fields.

    This function exposes the decision-gate result for CLI/docs.
    """
    return {
        "supported": False,
        "reason": "Public Gemini REST generateContent/list models paths do not expose reusable chat session handles in this client surface.",
        "evidence": ["v1/models", "v1beta/models/{model}:generateContent"],
    }
