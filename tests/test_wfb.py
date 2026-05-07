"""Smoke tests for wfb CLI (stdlib unittest)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import wfb


ROOT = Path(__file__).resolve().parents[1]
WFB = ROOT / "wfb.py"


def _run(
    args: list[str],
    cwd: Path | None = None,
    env_extra: dict[str, str] | None = None,
):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(WFB), *args],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        env=env,
    )


MIN_ENVELOPE = json.dumps({"version": 1})


class TestWfb(unittest.TestCase):
    @staticmethod
    def _oauth_ready_env(fake_home: Path) -> dict[str, str]:
        wfb_dir = fake_home / ".wfb"
        wfb_dir.mkdir(parents=True, exist_ok=True)
        client_secret = wfb_dir / "client_secret.json"
        client_secret.write_text(
            json.dumps(
                {
                    "installed": {
                        "client_id": "dummy-client-id.apps.googleusercontent.com",
                        "client_secret": "dummy-secret",
                        "redirect_uris": ["http://localhost"],
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                    }
                }
            ),
            encoding="utf-8",
        )
        token = wfb_dir / "token.json"
        token.write_text(
            json.dumps(
                {
                    "access_token": "cached-token",
                    "refresh_token": "cached-refresh",
                    "token_type": "Bearer",
                    "expires_at": int(time.time()) + 3600,
                }
            ),
            encoding="utf-8",
        )
        return {"HOME": str(fake_home)}

    def test_init_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            fake_home = d / "home"
            fake_home.mkdir()
            env = self._oauth_ready_env(fake_home)
            db = d / "t.db"
            r1 = _run(["--db", str(db), "init"], cwd=d, env_extra=env)
            self.assertEqual(r1.returncode, 0, r1.stderr)
            r2 = _run(["--db", str(db), "init"], cwd=d, env_extra=env)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertTrue(db.is_file())

    def test_seed_upsert_and_status(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            fake_home = d / "home"
            fake_home.mkdir()
            env = self._oauth_ready_env(fake_home)
            db = d / "t.db"
            self.assertEqual(_run(["--db", str(db), "init"], cwd=d, env_extra=env).returncode, 0)

            env1 = {
                "version": 1,
                "source": "gemini",
                "active_tasks": [
                    {
                        "id": "t1",
                        "title": "First",
                        "status": "in_progress",
                        "priority": 2,
                    }
                ],
                "environmental_constraints": [
                    {
                        "id": "c1",
                        "kind": "tool_version_warning",
                        "name": "python",
                        "value": "need 3.11+",
                        "severity": "warn",
                    }
                ],
                "style_specifications": [
                    {
                        "id": "s1",
                        "category": "coding_style",
                        "rule": "Use type hints.",
                        "priority": 1,
                    }
                ],
            }
            r = _run(
                ["--db", str(db), "seed", "--json", json.dumps(env1)],
                cwd=d,
                env_extra=env,
            )
            self.assertEqual(r.returncode, 0, r.stderr)

            env2 = {
                "version": 1,
                "active_tasks": [
                    {
                        "id": "t1",
                        "title": "First updated",
                        "status": "blocked",
                        "priority": 5,
                    }
                ],
            }
            r2 = _run(
                ["--db", str(db), "seed", "--json", json.dumps(env2)],
                cwd=d,
                env_extra=env,
            )
            self.assertEqual(r2.returncode, 0, r2.stderr)

            st = _run(["--db", str(db), "status", "--format", "text"], cwd=d, env_extra=env)
            self.assertEqual(st.returncode, 0, st.stderr)
            self.assertIn("First updated", st.stdout)
            self.assertIn("blocked", st.stdout)

            js = _run(["--db", str(db), "status", "--format", "json"], cwd=d, env_extra=env)
            self.assertEqual(js.returncode, 0, js.stderr)
            data = json.loads(js.stdout)
            self.assertEqual(data["version"], 1)
            self.assertEqual(data["summary"]["tasks"]["blocked"], 1)
            self.assertEqual(len(data["highlights"]["constraints"]), 1)

    def test_seed_replace_clears_stale(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            fake_home = d / "home"
            fake_home.mkdir()
            env = self._oauth_ready_env(fake_home)
            db = d / "t.db"
            self.assertEqual(_run(["--db", str(db), "init"], cwd=d, env_extra=env).returncode, 0)
            payload = {
                "version": 1,
                "active_tasks": [{"id": "a", "title": "A", "status": "done"}],
            }
            self.assertEqual(
                _run(
                    ["--db", str(db), "seed", "--json", json.dumps(payload)],
                    cwd=d,
                    env_extra=env,
                ).returncode,
                0,
            )
            rep = {"version": 1, "active_tasks": []}
            r = _run(
                ["--db", str(db), "seed", "--replace", "--json", json.dumps(rep)],
                cwd=d,
                env_extra=env,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            js = _run(["--db", str(db), "status", "--format", "json"], cwd=d, env_extra=env)
            data = json.loads(js.stdout)
            self.assertEqual(data["summary"]["tasks"]["done"], 0)
            self.assertEqual(data["highlights"]["tasks"], [])

    def test_validation_unknown_envelope_key(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            fake_home = d / "home"
            fake_home.mkdir()
            env = self._oauth_ready_env(fake_home)
            db = d / "t.db"
            self.assertEqual(_run(["--db", str(db), "init"], cwd=d, env_extra=env).returncode, 0)
            bad = {"version": 1, "extra": 1}
            r = _run(["--db", str(db), "seed", "--json", json.dumps(bad)], cwd=d, env_extra=env)
            self.assertEqual(r.returncode, 3)
            self.assertIn("unknown envelope", r.stderr)

    def test_seed_missing_db_tables(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            db = d / "empty.db"
            r = _run(["--db", str(db), "seed", "--json", MIN_ENVELOPE], cwd=d)
            self.assertEqual(r.returncode, 4)

    def test_init_default_db_under_fake_home(self):
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td) / "h"
            fake_home.mkdir()
            env = self._oauth_ready_env(fake_home)
            r1 = _run(["init"], env_extra=env)
            self.assertEqual(r1.returncode, 0, r1.stderr)
            dbpath = fake_home / ".wfb" / "wayfarer.db"
            self.assertTrue((fake_home / ".wfb").is_dir())
            self.assertTrue(dbpath.is_file())
            r2 = _run(["init"], env_extra=env)
            self.assertEqual(r2.returncode, 0, r2.stderr)

    def test_init_requires_client_secret_with_instructions(self):
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td) / "h"
            fake_home.mkdir()
            env = {"HOME": str(fake_home)}
            r = _run(["init", "--no-open-oauth-guide"], env_extra=env)
            self.assertEqual(r.returncode, 5)
            self.assertIn("~/.wfb/client_secret.json", r.stderr)
            self.assertIn("https://ai.google.dev/gemini-api/docs/oauth", r.stderr)

    def test_init_passes_no_browser_and_force_login_to_oauth(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            fake_home = d / "home"
            fake_home.mkdir()
            db = d / "t.db"

            with (
                mock.patch("wfb.wfb_home", return_value=fake_home),
                mock.patch("wfb.ensure_client_secret_present", return_value=True),
                mock.patch("wfb.connect_db") as mock_connect,
                mock.patch("wfb.init_db"),
                mock.patch("wfb.ensure_logged_in") as mock_login,
            ):
                mock_conn = mock.Mock()
                mock_connect.return_value = mock_conn
                rc = wfb.main(
                    [
                        "--db",
                        str(db),
                        "init",
                        "--no-browser",
                        "--force-login",
                    ]
                )
                self.assertEqual(rc, 0)
                mock_login.assert_called_once_with(
                    wfb_home=fake_home,
                    no_browser=True,
                    force_login=True,
                )
                mock_conn.close.assert_called()

    def test_init_oauth_timeout_returns_exit_io(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            fake_home = d / "home"
            fake_home.mkdir()
            db = d / "t.db"

            with (
                mock.patch("wfb.wfb_home", return_value=fake_home),
                mock.patch("wfb.ensure_client_secret_present", return_value=True),
                mock.patch("wfb.connect_db") as mock_connect,
                mock.patch("wfb.init_db"),
                mock.patch(
                    "wfb.ensure_logged_in",
                    side_effect=wfb.OAuthFlowError("OAuth callback timed out or returned no code"),
                ),
            ):
                mock_conn = mock.Mock()
                mock_connect.return_value = mock_conn
                rc = wfb.main(["--db", str(db), "init", "--no-browser"])
                self.assertEqual(rc, 5)
                mock_conn.close.assert_called()

    def test_gemini_ping_prints_models(self):
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.list_models", return_value=["models/a", "models/b", "models/c"]),
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(["gemini", "ping", "--limit", "2"])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            self.assertIn("Models: 3", written)
            self.assertIn("- models/a", written)
            self.assertIn("- models/b", written)

    def test_gemini_ask_prints_response(self):
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch(
                "wfb.load_session",
                return_value={
                    "id": "sess_1",
                    "name": "sess_1",
                    "model": "gemini-2.5-flash",
                    "messages": [],
                },
            ),
            mock.patch("wfb.get_active_session_id", return_value="sess_1"),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.append_turn"),
            mock.patch("wfb.ask_with_messages", return_value="hello from gemini"),
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(["gemini", "ask", "--prompt", "hello"])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            self.assertIn("hello from gemini", written)

    def test_gemini_session_new_use_inspect_json(self):
        with tempfile.TemporaryDirectory() as td:
            fake_home = Path(td)
            sess = {
                "id": "sess_abc",
                "name": "demo",
                "model": "gemini-2.5-flash",
                "messages": [],
            }
            with (
                mock.patch("wfb.wfb_home", return_value=fake_home),
                mock.patch("wfb.create_session", return_value=sess),
                mock.patch("wfb.load_session", return_value=sess),
                mock.patch("wfb.set_active_session"),
            ):
                rc_new = wfb.main(["gemini", "session", "new", "--name", "demo"])
                self.assertEqual(rc_new, 0)
                rc_use = wfb.main(["gemini", "session", "use", "--id", "sess_abc"])
                self.assertEqual(rc_use, 0)
                rc_inspect = wfb.main(["gemini", "session", "inspect", "--id", "sess_abc", "--format", "json"])
                self.assertEqual(rc_inspect, 0)

    def test_gemini_ask_triggers_summarization(self):
        long_messages = [{"role": "user", "text": f"m{i}"} for i in range(50)]
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch(
                "wfb.load_session",
                return_value={
                    "id": "sess_1",
                    "name": "sess_1",
                    "model": "gemini-2.5-flash",
                    "messages": long_messages,
                },
            ),
            mock.patch("wfb.get_active_session_id", return_value="sess_1"),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.session_message_stats", return_value={"turns": 50, "chars": 5000}),
            mock.patch(
                "wfb.summarization_policy_for_model",
                return_value={"max_turns": 40, "max_chars": 4000, "keep_recent_turns": 10},
            ),
            mock.patch("wfb.summarize_messages", return_value="summary") as summarize,
            mock.patch(
                "wfb.compacted_session_copy",
                return_value={
                    "id": "sess_1",
                    "name": "sess_1",
                    "model": "gemini-2.5-flash",
                    "messages": [{"role": "model", "kind": "history_summary", "text": "summary"}],
                },
            ),
            mock.patch("wfb.save_session"),
            mock.patch("wfb.ask_with_messages", return_value="ok"),
            mock.patch("wfb.append_turn"),
        ):
            rc = wfb.main(["gemini", "ask", "--prompt", "hello", "--auto-summarize", "on"])
            self.assertEqual(rc, 0)
            self.assertTrue(summarize.called)

    def test_gemini_ask_summarization_failure_hard_fails(self):
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch(
                "wfb.load_session",
                return_value={
                    "id": "sess_1",
                    "name": "sess_1",
                    "model": "gemini-2.5-flash",
                    "messages": [{"role": "user", "text": "m"}] * 50,
                },
            ),
            mock.patch("wfb.get_active_session_id", return_value="sess_1"),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.session_message_stats", return_value={"turns": 50, "chars": 5000}),
            mock.patch(
                "wfb.summarization_policy_for_model",
                return_value={"max_turns": 40, "max_chars": 4000, "keep_recent_turns": 10},
            ),
            mock.patch("wfb.summarize_messages", side_effect=wfb.GeminiApiError("summary failed")),
        ):
            rc = wfb.main(["gemini", "ask", "--prompt", "hello", "--auto-summarize", "on"])
            self.assertEqual(rc, 5)

    def test_gemini_ask_default_auto_summarize_is_off(self):
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch(
                "wfb.load_session",
                return_value={
                    "id": "sess_1",
                    "name": "sess_1",
                    "model": "gemini-2.5-flash",
                    "messages": [{"role": "user", "text": "m"}] * 500,
                },
            ),
            mock.patch("wfb.get_active_session_id", return_value="sess_1"),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.ask_with_messages", return_value="ok"),
            mock.patch("wfb.append_turn"),
            mock.patch("wfb.summarize_messages") as summarize,
        ):
            rc = wfb.main(["gemini", "ask", "--prompt", "hello"])
            self.assertEqual(rc, 0)
            self.assertFalse(summarize.called)

    def test_gemini_ask_failure_after_summary_does_not_save_compacted(self):
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch(
                "wfb.load_session",
                return_value={
                    "id": "sess_1",
                    "name": "sess_1",
                    "model": "gemini-2.5-flash",
                    "messages": [{"role": "user", "text": "m"}] * 200,
                },
            ),
            mock.patch("wfb.get_active_session_id", return_value="sess_1"),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.session_message_stats", return_value={"turns": 200, "chars": 250000}),
            mock.patch(
                "wfb.summarization_policy_for_model",
                return_value={"max_turns": 160, "max_chars": 200000, "keep_recent_turns": 50},
            ),
            mock.patch("wfb.summarize_messages", return_value="summary"),
            mock.patch(
                "wfb.compacted_session_copy",
                return_value={"id": "sess_1", "messages": [{"role": "model", "text": "summary", "kind": "history_summary"}]},
            ),
            mock.patch("wfb.ask_with_messages", side_effect=wfb.GeminiApiError("unavailable")),
            mock.patch("wfb.save_session") as save_session,
            mock.patch("wfb.append_turn"),
        ):
            rc = wfb.main(["gemini", "ask", "--prompt", "hello", "--auto-summarize", "on"])
            self.assertEqual(rc, 5)
            self.assertFalse(save_session.called)

    def test_gemini_ask_always_includes_summary_when_trimming(self):
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch(
                "wfb.load_session",
                return_value={
                    "id": "sess_1",
                    "name": "sess_1",
                    "model": "gemini-2.5-flash",
                    "messages": [
                        {"role": "model", "kind": "history_summary", "text": "summary"},
                        *[{"role": "user", "text": f"m{i}"} for i in range(60)],
                    ],
                },
            ),
            mock.patch("wfb.get_active_session_id", return_value="sess_1"),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.ask_with_messages", return_value="ok") as ask_call,
            mock.patch("wfb.append_turn"),
        ):
            rc = wfb.main(["gemini", "ask", "--prompt", "hello", "--max-history-turns", "5"])
            self.assertEqual(rc, 0)
            sent_messages = ask_call.call_args.kwargs["messages"]
            self.assertEqual(sent_messages[0]["text"], "summary")

    def test_gemini_ask_normalizes_legacy_system_role(self):
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch(
                "wfb.load_session",
                return_value={
                    "id": "sess_1",
                    "name": "sess_1",
                    "model": "gemini-2.5-flash",
                    "messages": [{"role": "system", "text": "legacy summary"}],
                },
            ),
            mock.patch("wfb.get_active_session_id", return_value="sess_1"),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.ask_with_messages", return_value="ok") as ask_call,
            mock.patch("wfb.append_turn"),
        ):
            rc = wfb.main(["gemini", "ask", "--prompt", "hello", "--max-history-turns", "5"])
            self.assertEqual(rc, 0)
            sent_messages = ask_call.call_args.kwargs["messages"]
            self.assertEqual(sent_messages[0]["role"], "model")

    def test_gemini_ask_auto_summarize_off(self):
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch(
                "wfb.load_session",
                return_value={
                    "id": "sess_1",
                    "name": "sess_1",
                    "model": "gemini-2.5-flash",
                    "messages": [{"role": "user", "text": "m"}] * 50,
                },
            ),
            mock.patch("wfb.get_active_session_id", return_value="sess_1"),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.ask_with_messages", return_value="ok"),
            mock.patch("wfb.append_turn"),
            mock.patch("wfb.summarize_messages") as summarize,
        ):
            rc = wfb.main(["gemini", "ask", "--prompt", "hello", "--auto-summarize", "off"])
            self.assertEqual(rc, 0)
            self.assertFalse(summarize.called)

    def test_gemini_session_new_sets_world_state_sync_defaults(self):
        sess = {"id": "sess_cfg", "name": "cfg", "model": "gemini-2.5-flash", "messages": []}
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.create_session", return_value=sess),
            mock.patch("wfb.update_world_state_sync", return_value=sess) as update_sync,
        ):
            rc = wfb.main(
                [
                    "gemini",
                    "session",
                    "new",
                    "--name",
                    "cfg",
                    "--sync-world-state",
                    "on",
                    "--world-state-db",
                    "/tmp/world.db",
                    "--world-state-scope",
                    "dev",
                ]
            )
            self.assertEqual(rc, 0)
            update_sync.assert_called_once()

    def test_gemini_ask_sync_uses_session_default(self):
        sess = {
            "id": "sess_1",
            "name": "sess_1",
            "model": "gemini-2.5-flash",
            "messages": [],
            "world_state_sync": "on",
            "world_state_db_path": "/tmp/world.db",
            "world_state_scope": "sync-scope",
        }
        envelope = {"version": 1, "active_tasks": [], "environmental_constraints": [], "style_specifications": []}
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.load_session", return_value=sess),
            mock.patch("wfb.get_active_session_id", return_value="sess_1"),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.ask_with_messages", return_value="ok"),
            mock.patch("wfb.append_turn"),
            mock.patch("wfb.extract_world_state_envelope", return_value=envelope),
            mock.patch("wfb.validate_envelope", return_value=envelope),
            mock.patch("wfb.connect_db") as connect_db,
            mock.patch("wfb.require_v1_schema"),
            mock.patch("wfb.seed_db") as seed_db,
        ):
            mock_conn = mock.Mock()
            connect_db.return_value = mock_conn
            rc = wfb.main(["gemini", "ask", "--prompt", "hello"])
            self.assertEqual(rc, 0)
            self.assertTrue(seed_db.called)
            mock_conn.close.assert_called_once()

    def test_gemini_ask_sync_override_off_skips_sync(self):
        sess = {
            "id": "sess_1",
            "name": "sess_1",
            "model": "gemini-2.5-flash",
            "messages": [],
            "world_state_sync": "on",
        }
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.load_session", return_value=sess),
            mock.patch("wfb.get_active_session_id", return_value="sess_1"),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.ask_with_messages", return_value="ok"),
            mock.patch("wfb.append_turn"),
            mock.patch("wfb.extract_world_state_envelope") as extract_sync,
        ):
            rc = wfb.main(["gemini", "ask", "--prompt", "hello", "--sync-world-state", "off"])
            self.assertEqual(rc, 0)
            self.assertFalse(extract_sync.called)

    def test_gemini_ask_sync_failure_is_non_fatal(self):
        sess = {
            "id": "sess_1",
            "name": "sess_1",
            "model": "gemini-2.5-flash",
            "messages": [],
            "world_state_sync": "on",
        }
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.load_session", return_value=sess),
            mock.patch("wfb.get_active_session_id", return_value="sess_1"),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.ask_with_messages", return_value="ok"),
            mock.patch("wfb.append_turn"),
            mock.patch(
                "wfb.extract_world_state_envelope",
                side_effect=wfb.GeminiApiError("extract failed"),
            ),
        ):
            rc = wfb.main(["gemini", "ask", "--prompt", "hello"])
            self.assertEqual(rc, 0)

    def test_chrome_targets_json(self):
        targets = [
            {
                "id": "t1",
                "title": "One",
                "url": "https://example.test/1",
                "type": "page",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
            }
        ]
        with mock.patch("wfb.list_page_targets", return_value=targets):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(["chrome", "targets", "--format", "json"])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            payload = json.loads(written)
            self.assertEqual(payload[0]["id"], "t1")

    def test_chrome_attach_persists_target(self):
        targets = [
            {
                "id": "t1",
                "title": "One",
                "url": "https://example.test/1",
                "type": "page",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
            }
        ]
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.list_page_targets", return_value=targets),
            mock.patch("wfb.save_attachment", return_value={"target_id": "t1"}) as save_attach,
        ):
            rc = wfb.main(["chrome", "attach", "--target-id", "t1"])
            self.assertEqual(rc, 0)
            save_attach.assert_called_once()

    def test_chrome_inspect_uses_saved_attachment(self):
        targets = [
            {
                "id": "t1",
                "title": "One",
                "url": "https://example.test/1",
                "type": "page",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
            }
        ]
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch(
                "wfb.load_chrome_attachment",
                return_value={
                    "target_id": "t1",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
                    "debug_port": 9222,
                },
            ),
            mock.patch("wfb.list_page_targets", return_value=targets),
            mock.patch(
                "wfb.inspect_target",
                return_value={"title": "One", "url": "https://example.test/1", "text_snapshot": "abc"},
            ) as inspect_target,
        ):
            rc = wfb.main(["chrome", "inspect", "--format", "json"])
            self.assertEqual(rc, 0)
            inspect_target.assert_called_once()

    def test_chrome_launch_reports_already_running(self):
        payload = {"Browser": "Chrome/136", "already_running": True}
        with mock.patch("wfb.launch_chrome_debug", return_value=payload):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(["chrome", "launch", "--format", "text"])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            self.assertIn("already_running: True", written)

    def test_chrome_launch_rejects_invalid_timeout(self):
        rc = wfb.main(["chrome", "launch", "--timeout-seconds", "0"])
        self.assertEqual(rc, 2)

    def test_chrome_detach_text(self):
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.clear_attachment", return_value=True),
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(["chrome", "detach"])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            self.assertIn("detached", written)


if __name__ == "__main__":
    unittest.main()
