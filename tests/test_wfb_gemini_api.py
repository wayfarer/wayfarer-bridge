"""Unit tests for stdlib Gemini REST module."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import wfb_gemini_api as api


class _Resp:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class TestWfbGeminiApi(unittest.TestCase):
    def test_summarization_policy_for_flash(self):
        p = api.summarization_policy_for_model("gemini-2.5-flash")
        self.assertEqual(p["max_turns"], 160)
        self.assertEqual(p["keep_recent_turns"], 64)

    def test_summarize_messages_empty_raises(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            with self.assertRaises(api.GeminiApiError):
                api.summarize_messages(wfb_home=home, model="gemini-2.5-flash", messages=[])

    def test_list_models_with_valid_token(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            with mock.patch(
                "wfb_gemini_api.load_token",
                return_value={"access_token": "abc", "expires_at": int(time.time()) + 3600},
            ), mock.patch(
                "wfb_gemini_api.urlopen",
                return_value=_Resp({"models": [{"name": "models/gemini-2.5-flash"}]}),
            ):
                names = api.list_models(wfb_home=home)
            self.assertEqual(names, ["models/gemini-2.5-flash"])

    def test_expired_token_refresh_success(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            expired = {
                "access_token": "expired",
                "refresh_token": "refresh-me",
                "expires_at": int(time.time()) - 1,
            }
            refresh_payload = {"access_token": "new-token", "expires_in": 3600, "token_type": "Bearer"}
            model_payload = {"models": [{"name": "models/gemini-2.5-flash"}]}
            with mock.patch("wfb_gemini_api.load_token", return_value=expired), mock.patch(
                "wfb_gemini_api.load_client_config",
                return_value={
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "client_id": "id",
                    "client_secret": "secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "redirect_uris": ["http://localhost"],
                },
            ), mock.patch(
                "wfb_gemini_api.urlopen",
                side_effect=[_Resp(refresh_payload), _Resp(model_payload)],
            ), mock.patch("wfb_gemini_api.save_token") as save_token:
                names = api.list_models(wfb_home=home)
            self.assertEqual(names, ["models/gemini-2.5-flash"])
            self.assertTrue(save_token.called)

    def test_refresh_failure_suggests_force_login(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            expired = {
                "access_token": "expired",
                "refresh_token": "refresh-me",
                "expires_at": int(time.time()) - 1,
            }
            with mock.patch("wfb_gemini_api.load_token", return_value=expired), mock.patch(
                "wfb_gemini_api.load_client_config",
                return_value={
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "client_id": "id",
                    "client_secret": "secret",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "redirect_uris": ["http://localhost"],
                },
            ), mock.patch(
                "wfb_gemini_api.urlopen",
                side_effect=api.URLError("down"),
            ):
                with self.assertRaises(api.GeminiApiError) as ctx:
                    api.list_models(wfb_home=home)
            self.assertIn("network error during token refresh", str(ctx.exception))

    def test_ask_text_malformed_response(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            with mock.patch(
                "wfb_gemini_api.load_token",
                return_value={"access_token": "abc", "expires_at": int(time.time()) + 3600},
            ), mock.patch(
                "wfb_gemini_api.urlopen",
                return_value=_Resp({"candidates": []}),
            ):
                with self.assertRaises(api.GeminiApiError) as ctx:
                    api.ask_text(wfb_home=home, prompt="hello")
            self.assertIn("missing candidates", str(ctx.exception))

    def test_ask_with_messages_sends_history_and_system(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            payload = {
                "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            }
            with mock.patch(
                "wfb_gemini_api.load_token",
                return_value={"access_token": "abc", "expires_at": int(time.time()) + 3600},
            ), mock.patch("wfb_gemini_api.urlopen", return_value=_Resp(payload)):
                out = api.ask_with_messages(
                    wfb_home=home,
                    model="gemini-2.5-flash",
                    messages=[
                        {"role": "user", "text": "hi"},
                        {"role": "model", "text": "hello"},
                        {"role": "user", "text": "again"},
                    ],
                    system="be concise",
                )
            self.assertEqual(out, "ok")

    def test_api_managed_state_supported_false(self):
        state = api.api_managed_state_supported()
        self.assertFalse(state["supported"])
        self.assertIn("generateContent", state["reason"])

    def test_summarize_messages_strict_empty_output(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            with mock.patch("wfb_gemini_api.ask_with_messages", return_value="   "):
                with self.assertRaises(api.GeminiApiError) as ctx:
                    api.summarize_messages(
                        wfb_home=home,
                        model="gemini-2.5-flash",
                        messages=[{"role": "user", "text": "x"}],
                    )
            self.assertIn("empty", str(ctx.exception))

    def test_extract_world_state_envelope_parses_json(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            with mock.patch(
                "wfb_gemini_api.ask_with_messages",
                return_value='{"version":1,"active_tasks":[],"environmental_constraints":[],"style_specifications":[]}',
            ):
                out = api.extract_world_state_envelope(
                    wfb_home=home,
                    model="gemini-2.5-flash",
                    session_id="sess_1",
                    messages=[{"role": "user", "text": "x"}],
                )
            self.assertEqual(out["version"], 1)

    def test_extract_world_state_envelope_invalid_json_raises(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            with mock.patch("wfb_gemini_api.ask_with_messages", return_value="not json"):
                with self.assertRaises(api.GeminiApiError):
                    api.extract_world_state_envelope(
                        wfb_home=home,
                        model="gemini-2.5-flash",
                        session_id="sess_1",
                        messages=[{"role": "user", "text": "x"}],
                    )


if __name__ == "__main__":
    unittest.main()
