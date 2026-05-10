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
        with (
            mock.patch("wfb.parse_target_types", return_value=("page",)),
            mock.patch("wfb.list_targets", return_value=targets),
        ):
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
            mock.patch("wfb.parse_target_types", return_value=("page",)),
            mock.patch("wfb.list_targets", return_value=targets),
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
            mock.patch("wfb.parse_target_types", return_value=("page",)),
            mock.patch("wfb.detect_debug_ports", return_value=[]),
            mock.patch(
                "wfb.load_chrome_attachment",
                return_value={
                    "target_id": "t1",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
                    "debug_port": 9222,
                },
            ),
            mock.patch("wfb.list_targets", return_value=targets),
            mock.patch(
                "wfb.inspect_target",
                return_value={"title": "One", "url": "https://example.test/1", "text_snapshot": "abc"},
            ) as inspect_target,
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(["chrome", "inspect", "--format", "json"])
            self.assertEqual(rc, 0)
            inspect_target.assert_called_once()
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            payload = json.loads(written)
            self.assertFalse(payload["debug"]["fallback_used"])

    def test_chrome_inspect_saved_webview_expands_default_include_types(self):
        targets = [
            {
                "id": "t1",
                "title": "Gemini Panel",
                "url": "https://gemini.google.com/glic",
                "type": "webview",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
            }
        ]
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page",)),
            mock.patch("wfb.detect_debug_ports", return_value=[]),
            mock.patch(
                "wfb.load_chrome_attachment",
                return_value={
                    "target_id": "t1",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
                    "debug_port": 9222,
                    "type": "webview",
                },
            ),
            mock.patch("wfb.list_targets", return_value=targets) as list_targets,
            mock.patch(
                "wfb.inspect_target",
                return_value={"title": "Gemini Panel", "url": "https://gemini.google.com/glic", "text_snapshot": "abc"},
            ),
        ):
            rc = wfb.main(["chrome", "inspect", "--format", "json"])
            self.assertEqual(rc, 0)
            self.assertEqual(list_targets.call_args.kwargs["include_types"], ("page", "webview"))

    def test_chrome_inspect_explicit_include_types_takes_precedence(self):
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page",)),
            mock.patch("wfb.detect_debug_ports", return_value=[]),
            mock.patch(
                "wfb.load_chrome_attachment",
                return_value={
                    "target_id": "t1",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
                    "debug_port": 9222,
                    "type": "webview",
                },
            ),
            mock.patch("wfb.list_targets", return_value=[]) as list_targets,
        ):
            rc = wfb.main(["chrome", "inspect", "--format", "json", "--include-types", "page"])
            self.assertEqual(rc, 5)
            self.assertEqual(list_targets.call_args.kwargs["include_types"], ("page",))

    def test_chrome_inspect_port_fallback_follows_detected_debug_ports(self):
        tgt = {
            "id": "t1",
            "title": "Gemini Panel",
            "url": "https://gemini.google.com/glic",
            "type": "webview",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/page/t1",
        }

        def _lt(**kwargs: object) -> list[dict[str, object]]:
            port = kwargs["port"]
            if port == 9222:
                raise wfb.ChromeBridgeError("refused")
            return [tgt]

        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb.detect_debug_ports", return_value=[{"port": 9333, "version": {}}]),
            mock.patch("wfb.list_targets", side_effect=_lt),
            mock.patch(
                "wfb.load_chrome_attachment",
                return_value={
                    "target_id": "t1",
                    "type": "webview",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/page/t1",
                    "debug_port": 9222,
                },
            ),
            mock.patch(
                "wfb.inspect_target",
                return_value={"title": "Gemini Panel", "url": "https://gemini.google.com/glic", "text_snapshot": "z"},
            ),
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(["chrome", "inspect", "--format", "json"])
            self.assertEqual(rc, 0)
            payload = json.loads(
                "".join(call.args[0] for call in out.write.call_args_list if call.args)
            )
            self.assertTrue(payload["debug"]["fallback_used"])
            self.assertEqual(payload["debug"]["resolved_port"], 9333)

    def test_chrome_inspect_attachment_uses_resolved_target_websocket(self):
        resolved_target = {
            "id": "t1",
            "title": "Gemini Panel",
            "url": "https://gemini.google.com/glic",
            "type": "webview",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/page/t1",
        }

        def _lt(**kwargs: object) -> list[dict[str, object]]:
            port = int(kwargs["port"])
            if port == 9222:
                raise wfb.ChromeBridgeError("refused")
            return [resolved_target]

        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb.detect_debug_ports", return_value=[{"port": 9333, "version": {}}]),
            mock.patch("wfb.list_targets", side_effect=_lt),
            mock.patch(
                "wfb.load_chrome_attachment",
                return_value={
                    "target_id": "t1",
                    "type": "webview",
                    # Intentionally stale; fallback should replace this with resolved target websocket.
                    "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
                    "debug_port": 9222,
                },
            ),
            mock.patch("wfb.inspect_target", return_value={"title": "Gemini Panel", "url": "https://gemini.google.com/glic", "text_snapshot": "z"}) as inspect_target,
        ):
            rc = wfb.main(["chrome", "inspect", "--format", "json"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            inspect_target.call_args.kwargs["ws_url"],
            "ws://127.0.0.1:9333/devtools/page/t1",
        )

    @staticmethod
    def _ax_node(node_id, role, name=None, child_ids=None, ignored=False, parent_id=None):
        node = {
            "nodeId": node_id,
            "ignored": ignored,
            "role": {"type": "internalRole", "value": role},
            "childIds": child_ids or [],
        }
        if name is not None:
            node["name"] = {"type": "computedString", "value": name}
        if parent_id is not None:
            node["parentId"] = parent_id
        return node

    @staticmethod
    def _ax_attachment_target():
        return {
            "id": "t1",
            "title": "Conversation",
            "url": "https://example.test",
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
        }

    def test_chrome_ax_outline_renders_indented_tree(self):
        target = self._ax_attachment_target()
        ax_nodes = [
            self._ax_node("1", "WebArea", child_ids=["2"]),
            self._ax_node("2", "main", name="Conversation", child_ids=["3"], parent_id="1"),
            self._ax_node("3", "button", name="Send", parent_id="2"),
        ]
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb.detect_debug_ports", return_value=[]),
            mock.patch(
                "wfb.load_chrome_attachment",
                return_value={
                    "target_id": "t1",
                    "webSocketDebuggerUrl": target["webSocketDebuggerUrl"],
                    "debug_port": 9222,
                },
            ),
            mock.patch("wfb.list_targets", return_value=[target]),
            mock.patch("wfb.get_accessibility_tree", return_value=ax_nodes) as get_ax,
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(["chrome", "ax", "--format", "outline"])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            self.assertIn("WebArea", written)
            self.assertIn('main "Conversation"', written)
            self.assertIn('button "Send"', written)
            self.assertIn("# total_nodes=3", written)
            get_ax.assert_called_once()

    def test_chrome_ax_json_includes_filters_quality_and_nodes(self):
        target = self._ax_attachment_target()
        ax_nodes = [
            self._ax_node("1", "main", child_ids=["2"]),
            self._ax_node("2", "textbox", name="Compose", parent_id="1"),
            self._ax_node("3", "generic"),
        ]
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb.detect_debug_ports", return_value=[]),
            mock.patch(
                "wfb.load_chrome_attachment",
                return_value={
                    "target_id": "t1",
                    "webSocketDebuggerUrl": target["webSocketDebuggerUrl"],
                    "debug_port": 9222,
                },
            ),
            mock.patch("wfb.list_targets", return_value=[target]),
            mock.patch("wfb.get_accessibility_tree", return_value=ax_nodes),
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(
                    ["chrome", "ax", "--format", "json", "--role", "textbox"]
                )
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            payload = json.loads(written)
            self.assertEqual(payload["target"]["id"], "t1")
            self.assertEqual(payload["filters"]["role"], "textbox")
            self.assertEqual(payload["outline_meta"]["selected_count"], 1)
            self.assertGreaterEqual(payload["ax_quality"]["meaningful_roles"], 1)
            self.assertEqual([n["role"] for n in payload["nodes"]], ["textbox"])

    def test_chrome_ax_propagates_depth_to_cdp(self):
        target = self._ax_attachment_target()
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb.detect_debug_ports", return_value=[]),
            mock.patch(
                "wfb.load_chrome_attachment",
                return_value={
                    "target_id": "t1",
                    "webSocketDebuggerUrl": target["webSocketDebuggerUrl"],
                    "debug_port": 9222,
                },
            ),
            mock.patch("wfb.list_targets", return_value=[target]),
            mock.patch("wfb.get_accessibility_tree", return_value=[]) as get_ax,
        ):
            rc = wfb.main(["chrome", "ax", "--format", "json", "--depth", "3"])
            self.assertEqual(rc, 0)
            self.assertEqual(get_ax.call_args.kwargs["depth"], 3)

    def test_chrome_ax_validates_args(self):
        with mock.patch("sys.stderr"):
            rc = wfb.main(["chrome", "ax", "--max-nodes", "0"])
            self.assertEqual(rc, 2)
            rc = wfb.main(["chrome", "ax", "--depth", "-1"])
            self.assertEqual(rc, 2)

    def test_chrome_ax_no_attachment_returns_io_error(self):
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb.load_chrome_attachment", return_value=None),
            mock.patch("sys.stderr") as err,
        ):
            rc = wfb.main(["chrome", "ax"])
            self.assertEqual(rc, 5)
            written = "".join(call.args[0] for call in err.write.call_args_list if call.args)
            self.assertIn("no attached Chrome target", written)

    def test_chrome_find_returns_text_and_aom_matches(self):
        target = self._ax_attachment_target()
        ax_nodes = [
            self._ax_node("1", "main", name="Conversation", child_ids=["2"]),
            self._ax_node("2", "log", name="Find this needle here", parent_id="1"),
        ]
        inspect_payload = {
            "title": "Conversation",
            "url": "https://example.test",
            "text_snapshot": "before needle here and another needle there",
            "text_snapshot_chars": 43,
            "text_snapshot_truncated": False,
            "selector_matched": None,
        }
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb.detect_debug_ports", return_value=[]),
            mock.patch(
                "wfb.load_chrome_attachment",
                return_value={
                    "target_id": "t1",
                    "webSocketDebuggerUrl": target["webSocketDebuggerUrl"],
                    "debug_port": 9222,
                },
            ),
            mock.patch("wfb.list_targets", return_value=[target]),
            mock.patch("wfb.inspect_target", return_value=inspect_payload),
            mock.patch("wfb.get_accessibility_tree", return_value=ax_nodes),
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(
                    ["chrome", "find", "--query", "needle", "--format", "json"]
                )
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            payload = json.loads(written)
            self.assertEqual(payload["query"], "needle")
            self.assertEqual(payload["mode"], "both")
            self.assertEqual(len(payload["text_matches"]), 2)
            self.assertEqual(len(payload["ax_matches"]), 1)
            self.assertEqual(payload["ax_matches"][0]["role"], "log")
            self.assertEqual(
                [p["role"] for p in payload["ax_matches"][0]["path"]],
                ["main", "log"],
            )

    def test_chrome_find_text_only_skips_aom(self):
        target = self._ax_attachment_target()
        inspect_payload = {
            "title": "T",
            "url": "https://example.test",
            "text_snapshot": "needle text",
            "text_snapshot_chars": 11,
            "text_snapshot_truncated": False,
        }
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb.detect_debug_ports", return_value=[]),
            mock.patch(
                "wfb.load_chrome_attachment",
                return_value={
                    "target_id": "t1",
                    "webSocketDebuggerUrl": target["webSocketDebuggerUrl"],
                    "debug_port": 9222,
                },
            ),
            mock.patch("wfb.list_targets", return_value=[target]),
            mock.patch("wfb.inspect_target", return_value=inspect_payload),
            mock.patch("wfb.get_accessibility_tree") as get_ax,
        ):
            rc = wfb.main(
                [
                    "chrome",
                    "find",
                    "--query",
                    "needle",
                    "--mode",
                    "text",
                    "--format",
                    "json",
                ]
            )
            self.assertEqual(rc, 0)
            get_ax.assert_not_called()

    def test_chrome_find_aom_only_skips_text(self):
        target = self._ax_attachment_target()
        ax_nodes = [self._ax_node("1", "log", name="needle here")]
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb.detect_debug_ports", return_value=[]),
            mock.patch(
                "wfb.load_chrome_attachment",
                return_value={
                    "target_id": "t1",
                    "webSocketDebuggerUrl": target["webSocketDebuggerUrl"],
                    "debug_port": 9222,
                },
            ),
            mock.patch("wfb.list_targets", return_value=[target]),
            mock.patch("wfb.inspect_target") as inspect_mock,
            mock.patch("wfb.get_accessibility_tree", return_value=ax_nodes),
        ):
            rc = wfb.main(
                [
                    "chrome",
                    "find",
                    "--query",
                    "needle",
                    "--mode",
                    "aom",
                    "--format",
                    "json",
                ]
            )
            self.assertEqual(rc, 0)
            inspect_mock.assert_not_called()

    def test_chrome_find_validates_args(self):
        with mock.patch("sys.stderr"):
            rc = wfb.main(["chrome", "find", "--query", "x", "--max-results", "0"])
            self.assertEqual(rc, 2)
            rc = wfb.main(["chrome", "find", "--query", " "])
            self.assertEqual(rc, 2)

    def test_chrome_inspect_passes_selector_to_inspect_target(self):
        target = self._ax_attachment_target()
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page",)),
            mock.patch("wfb.detect_debug_ports", return_value=[]),
            mock.patch(
                "wfb.load_chrome_attachment",
                return_value={
                    "target_id": "t1",
                    "webSocketDebuggerUrl": target["webSocketDebuggerUrl"],
                    "debug_port": 9222,
                },
            ),
            mock.patch("wfb.list_targets", return_value=[target]),
            mock.patch(
                "wfb.inspect_target",
                return_value={
                    "title": "T",
                    "url": "https://example.test",
                    "text_snapshot": "subtree",
                    "text_snapshot_chars": 7,
                    "text_snapshot_truncated": False,
                    "selector": "main",
                    "selector_matched": True,
                },
            ) as inspect_mock,
        ):
            rc = wfb.main(
                ["chrome", "inspect", "--format", "json", "--selector", "main"]
            )
            self.assertEqual(rc, 0)
            self.assertEqual(inspect_mock.call_args.kwargs["selector"], "main")

    def test_chrome_inspect_warns_when_selector_unmatched(self):
        target = self._ax_attachment_target()
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page",)),
            mock.patch("wfb.detect_debug_ports", return_value=[]),
            mock.patch(
                "wfb.load_chrome_attachment",
                return_value={
                    "target_id": "t1",
                    "webSocketDebuggerUrl": target["webSocketDebuggerUrl"],
                    "debug_port": 9222,
                },
            ),
            mock.patch("wfb.list_targets", return_value=[target]),
            mock.patch(
                "wfb.inspect_target",
                return_value={
                    "title": "T",
                    "url": "https://example.test",
                    "text_snapshot": "",
                    "text_snapshot_chars": 0,
                    "text_snapshot_truncated": False,
                    "selector": ".missing",
                    "selector_matched": False,
                },
            ),
            mock.patch("sys.stderr") as err,
        ):
            rc = wfb.main(
                ["chrome", "inspect", "--format", "json", "--selector", ".missing"]
            )
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in err.write.call_args_list if call.args)
            self.assertIn("selector did not match", written)

    def test_bridge_ask_auto_capture_picks_aom_with_meaningful_roles(self):
        target = {
            "id": "t1",
            "title": "Page",
            "url": "https://example.test",
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
        }
        ax_nodes = [
            self._ax_node("1", "main", name="Page", child_ids=["2", "3", "4", "5", "6"]),
            self._ax_node("2", "heading", name="Welcome", parent_id="1"),
            self._ax_node("3", "paragraph", name="Body text", parent_id="1"),
            self._ax_node("4", "button", name="Send", parent_id="1"),
            self._ax_node("5", "textbox", name="Compose", parent_id="1"),
            self._ax_node("6", "link", name="Home", parent_id="1"),
        ]
        inspect_payload = {
            "title": "Page",
            "url": "https://example.test",
            "text_snapshot": "fallback text",
            "text_snapshot_chars": 13,
            "text_snapshot_truncated": False,
        }
        fake_session = {
            "id": "sess_auto",
            "name": "sess_auto",
            "model": "gemini-2.5-flash",
            "system": None,
            "messages": [],
        }
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb._list_targets_with_port_fallback", return_value=([target], 9222)),
            mock.patch("wfb.select_capture_target", return_value=(target, "heuristic", "ranked")),
            mock.patch("wfb.save_attachment", return_value={"target_id": "t1"}),
            mock.patch("wfb.inspect_target", return_value=inspect_payload),
            mock.patch("wfb.get_accessibility_tree", return_value=ax_nodes),
            mock.patch("wfb.get_active_session_id", return_value="sess_auto"),
            mock.patch("wfb.load_session", return_value=fake_session),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.ask_with_messages", return_value="ok") as ask_mock,
            mock.patch("wfb.append_turn"),
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(["bridge", "ask", "--prompt", "summarize"])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            payload = json.loads(written)
            self.assertEqual(payload["capture"]["mode_chosen"], "aom")
            self.assertIn("auto:", payload["capture"]["mode_reason"])
            self.assertEqual(
                payload["prompt_envelope"]["budget"]["capture_mode"],
                "aom",
            )
            self.assertGreater(
                payload["prompt_envelope"]["budget"]["ax_total_nodes"],
                0,
            )
            sent_message = ask_mock.call_args.kwargs["messages"][0]["text"]
            self.assertIn("--- ACCESSIBILITY OUTLINE ---", sent_message)
            self.assertNotIn("--- PAGE CONTENT ---", sent_message)

    def test_bridge_ask_capture_mode_text_keeps_legacy_envelope(self):
        target = {
            "id": "t1",
            "title": "Page",
            "url": "https://example.test",
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
        }
        inspect_payload = {
            "title": "Page",
            "url": "https://example.test",
            "text_snapshot": "body text",
            "text_snapshot_chars": 9,
            "text_snapshot_truncated": False,
        }
        fake_session = {
            "id": "sess_text",
            "name": "sess_text",
            "model": "gemini-2.5-flash",
            "system": None,
            "messages": [],
        }
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb._list_targets_with_port_fallback", return_value=([target], 9222)),
            mock.patch("wfb.select_capture_target", return_value=(target, "heuristic", "ranked")),
            mock.patch("wfb.save_attachment", return_value={"target_id": "t1"}),
            mock.patch("wfb.inspect_target", return_value=inspect_payload),
            mock.patch("wfb.get_accessibility_tree") as get_ax,
            mock.patch("wfb.get_active_session_id", return_value="sess_text"),
            mock.patch("wfb.load_session", return_value=fake_session),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.ask_with_messages", return_value="ok") as ask_mock,
            mock.patch("wfb.append_turn"),
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(
                    [
                        "bridge",
                        "ask",
                        "--prompt",
                        "summarize",
                        "--capture-mode",
                        "text",
                    ]
                )
            self.assertEqual(rc, 0)
            get_ax.assert_not_called()
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            payload = json.loads(written)
            self.assertEqual(payload["capture"]["mode_chosen"], "text")
            sent_message = ask_mock.call_args.kwargs["messages"][0]["text"]
            self.assertIn("--- PAGE CONTENT ---", sent_message)
            self.assertNotIn("--- ACCESSIBILITY OUTLINE ---", sent_message)

    def test_bridge_ask_auto_falls_back_to_text_when_ax_capture_fails(self):
        target = {
            "id": "t1",
            "title": "Page",
            "url": "https://example.test",
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
        }
        inspect_payload = {
            "title": "Page",
            "url": "https://example.test",
            "text_snapshot": "fallback body text",
            "text_snapshot_chars": 18,
            "text_snapshot_truncated": False,
        }
        fake_session = {
            "id": "sess_axfail",
            "name": "sess_axfail",
            "model": "gemini-2.5-flash",
            "system": None,
            "messages": [],
        }
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb._list_targets_with_port_fallback", return_value=([target], 9222)),
            mock.patch("wfb.select_capture_target", return_value=(target, "heuristic", "ranked")),
            mock.patch("wfb.save_attachment", return_value={"target_id": "t1"}),
            mock.patch("wfb.inspect_target", return_value=inspect_payload),
            mock.patch(
                "wfb.get_accessibility_tree",
                side_effect=wfb.ChromeBridgeError("Accessibility domain unsupported"),
            ),
            mock.patch("wfb.get_active_session_id", return_value="sess_axfail"),
            mock.patch("wfb.load_session", return_value=fake_session),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.ask_with_messages", return_value="ok") as ask_mock,
            mock.patch("wfb.append_turn"),
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(["bridge", "ask", "--prompt", "summarize"])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            payload = json.loads(written)
            self.assertEqual(payload["capture"]["mode_chosen"], "text")
            self.assertIn("AX capture failed", payload["capture"]["mode_reason"])
            self.assertEqual(
                payload["prompt_envelope"]["budget"]["capture_mode"],
                "text",
            )
            sent_message = ask_mock.call_args.kwargs["messages"][0]["text"]
            self.assertIn("--- PAGE CONTENT ---", sent_message)
            self.assertNotIn("--- ACCESSIBILITY OUTLINE ---", sent_message)

    def test_bridge_ask_capture_mode_aom_propagates_ax_error(self):
        target = {
            "id": "t1",
            "title": "Page",
            "url": "https://example.test",
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
        }
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb._list_targets_with_port_fallback", return_value=([target], 9222)),
            mock.patch("wfb.select_capture_target", return_value=(target, "heuristic", "ranked")),
            mock.patch("wfb.save_attachment", return_value={"target_id": "t1"}),
            mock.patch(
                "wfb.get_accessibility_tree",
                side_effect=wfb.ChromeBridgeError("Accessibility domain unsupported"),
            ),
            mock.patch("sys.stderr") as err,
        ):
            rc = wfb.main(
                [
                    "bridge",
                    "ask",
                    "--prompt",
                    "summarize",
                    "--capture-mode",
                    "aom",
                ]
            )
            self.assertEqual(rc, 5)
            written = "".join(call.args[0] for call in err.write.call_args_list if call.args)
            self.assertIn("capture stage failed", written)
            self.assertIn("Accessibility domain unsupported", written)

    def test_inspect_target_selector_none_forces_match_to_none(self):
        # Even if Chrome reports false (e.g. because document.body was null),
        # we should not surface a selector_matched=False when the user didn't
        # supply a selector.
        from wfb_chrome_bridge import inspect_target as _inspect

        with mock.patch("wfb_chrome_bridge.CDPConnection") as conn_cls:
            cm = conn_cls.return_value.__enter__.return_value
            cm.call.return_value = {
                "result": {
                    "value": {
                        "url": "https://example.test",
                        "title": "X",
                        "selected_text": "",
                        "text_snapshot": "",
                        "selector_matched": False,
                    }
                }
            }
            out = _inspect(ws_url="ws://127.0.0.1:9222/devtools/page/x")
        self.assertIsNone(out["selector_matched"])
        self.assertIsNone(out["selector"])

    def test_bridge_ask_capture_mode_both_includes_text_and_aom(self):
        target = {
            "id": "t1",
            "title": "Page",
            "url": "https://example.test",
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
        }
        ax_nodes = [self._ax_node("1", "main", name="Page", child_ids=["2"]),
                    self._ax_node("2", "button", name="Go", parent_id="1")]
        inspect_payload = {
            "title": "Page",
            "url": "https://example.test",
            "text_snapshot": "page body",
            "text_snapshot_chars": 9,
            "text_snapshot_truncated": False,
        }
        fake_session = {
            "id": "sess_both",
            "name": "sess_both",
            "model": "gemini-2.5-flash",
            "system": None,
            "messages": [],
        }
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb._list_targets_with_port_fallback", return_value=([target], 9222)),
            mock.patch("wfb.select_capture_target", return_value=(target, "heuristic", "ranked")),
            mock.patch("wfb.save_attachment", return_value={"target_id": "t1"}),
            mock.patch("wfb.inspect_target", return_value=inspect_payload),
            mock.patch("wfb.get_accessibility_tree", return_value=ax_nodes),
            mock.patch("wfb.get_active_session_id", return_value="sess_both"),
            mock.patch("wfb.load_session", return_value=fake_session),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.ask_with_messages", return_value="ok") as ask_mock,
            mock.patch("wfb.append_turn"),
        ):
            rc = wfb.main(
                [
                    "bridge",
                    "ask",
                    "--prompt",
                    "summarize",
                    "--capture-mode",
                    "both",
                ]
            )
            self.assertEqual(rc, 0)
            sent_message = ask_mock.call_args.kwargs["messages"][0]["text"]
            self.assertIn("--- ACCESSIBILITY OUTLINE ---", sent_message)
            self.assertIn("--- PAGE CONTENT ---", sent_message)

    def test_list_targets_with_port_fallback_retries_detected_ports(self):
        calls: list[int] = []

        def _lt(**kwargs: object) -> list[dict[str, str]]:
            calls.append(int(kwargs["port"]))
            port = int(kwargs["port"])
            if port == 9222:
                raise wfb.ChromeBridgeError("bad")
            return [{"id": "ok"}]

        with (
            mock.patch("wfb.detect_debug_ports", return_value=[{"port": 9223, "version": {}}]),
            mock.patch("wfb.list_targets", side_effect=_lt),
        ):
            rows, p = wfb._list_targets_with_port_fallback(port=9222, include_types=("page",))
        self.assertEqual(p, 9223)
        self.assertEqual(rows[0]["id"], "ok")

    def test_bridge_doctor_defaults_to_json(self):
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.detect_debug_ports", return_value=[]),
            mock.patch("wfb.fetch_version", return_value={"Browser": "Chrome/Test"}),
            mock.patch("wfb.list_targets", return_value=[]),
            mock.patch("wfb.load_chrome_attachment", return_value=None),
            mock.patch("wfb.get_active_session_id", return_value=None),
            mock.patch("sys.stdout") as out,
        ):
            rc = wfb.main(["bridge", "doctor"])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            payload = json.loads(written)
            self.assertTrue(payload["endpoint"]["reachable"])
            self.assertGreaterEqual(len(payload["recommendations"]), 1)

    def test_bridge_doctor_invalid_port(self):
        rc = wfb.main(["bridge", "doctor", "--port", "0"])
        self.assertEqual(rc, 2)

    def test_bridge_doctor_text_format_has_recommendation_lines(self):
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.detect_debug_ports", return_value=[]),
            mock.patch("wfb.fetch_version", return_value={"Browser": "Chrome/Test"}),
            mock.patch("wfb.list_targets", return_value=[]),
            mock.patch("wfb.load_chrome_attachment", return_value=None),
            mock.patch("wfb.get_active_session_id", return_value=None),
            mock.patch("sys.stdout") as out,
        ):
            rc = wfb.main(["bridge", "doctor", "--format", "text"])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            self.assertIn("endpoint_reachable=", written)
            self.assertIn("- ", written)

    def test_chrome_targets_gemini_only_passed(self):
        with (
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb.list_targets", return_value=[] ) as list_targets,
        ):
            rc = wfb.main(["chrome", "targets", "--gemini-only", "--include-types", "page,webview"])
            self.assertEqual(rc, 0)
            self.assertTrue(list_targets.call_args.kwargs["gemini_only"])

    def test_chrome_attach_target_not_found_hints_webview(self):
        with (
            mock.patch("wfb.parse_target_types", return_value=("page",)),
            mock.patch("wfb.list_targets", return_value=[]),
        ):
            rc = wfb.main(["chrome", "attach", "--target-id", "missing"])
            self.assertEqual(rc, 5)

    def test_chrome_targets_invalid_include_types_returns_exit_io(self):
        with mock.patch(
            "wfb.parse_target_types",
            side_effect=wfb.ChromeBridgeError("unsupported target type"),
        ):
            rc = wfb.main(["chrome", "targets", "--include-types", "page,iframe"])
            self.assertEqual(rc, 5)

    def test_chrome_targets_unreachable_includes_recovery_hint(self):
        with (
            mock.patch("wfb.parse_target_types", return_value=("page",)),
            mock.patch("wfb.list_targets", side_effect=wfb.ChromeBridgeError("connection refused")),
            mock.patch("sys.stderr") as err,
        ):
            rc = wfb.main(["chrome", "targets", "--port", "9333"])
            self.assertEqual(rc, 5)
            written = "".join(call.args[0] for call in err.write.call_args_list if call.args)
            self.assertIn("next steps:", written)
            self.assertIn("wfb bridge doctor", written)

    def test_chrome_capture_warns_on_empty_text_snapshot(self):
        target = {
            "id": "t1",
            "title": "Internal",
            "url": "chrome://version/",
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
        }
        empty_inspect = {
            "title": "Internal",
            "url": "chrome://version/",
            "text_snapshot": "",
            "text_snapshot_chars": 0,
            "text_snapshot_truncated": False,
        }
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb._list_targets_with_port_fallback", return_value=([target], 9222)),
            mock.patch(
                "wfb.select_capture_target",
                return_value=(target, "heuristic", "selected by heuristic ranking"),
            ),
            mock.patch("wfb.save_attachment", return_value={"target_id": "t1"}),
            mock.patch("wfb.inspect_target", return_value=empty_inspect),
            mock.patch("sys.stdout"),
            mock.patch("sys.stderr") as err,
        ):
            rc = wfb.main(["chrome", "capture", "--format", "json"])
        self.assertEqual(rc, 0)
        written = "".join(call.args[0] for call in err.write.call_args_list if call.args)
        self.assertIn("empty text snapshot", written)

    def test_chrome_launch_reports_already_running(self):
        payload = {
            "Browser": "Chrome/136",
            "already_running": True,
            "fallback_used": False,
            "requested_port": 9222,
            "resolved_port": 9222,
        }
        with mock.patch("wfb.launch_chrome_debug", return_value=payload):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(["chrome", "launch", "--format", "text"])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            self.assertIn("already_running: True", written)

    def test_chrome_launch_reports_fallback_port(self):
        payload = {
            "Browser": "Chrome/136",
            "already_running": True,
            "fallback_used": True,
            "requested_port": 9222,
            "resolved_port": 9333,
        }
        with mock.patch("wfb.launch_chrome_debug", return_value=payload):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(["chrome", "launch", "--format", "text", "--port", "9222"])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            self.assertIn("Chrome debug ready on port 9333", written)
            self.assertIn("fallback_used: True", written)

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

    def test_chrome_current_defaults_to_json(self):
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb._chrome_current_payload", return_value={"attached": False}),
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(["chrome", "current"])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            self.assertIn('"attached": false', written)

    def test_chrome_current_text_when_unattached(self):
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb._chrome_current_payload", return_value={"attached": False}),
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(["chrome", "current", "--format", "text"])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            self.assertIn("no attachment", written)

    def test_chrome_capture_defaults_to_json(self):
        target = {
            "id": "t1",
            "title": "Gemini Chat",
            "url": "https://gemini.google.com/glic",
            "type": "webview",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/page/t1",
        }
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb._list_targets_with_port_fallback", return_value=([target], 9333)),
            mock.patch("wfb.select_capture_target", return_value=(target, "heuristic", "selected by heuristic ranking")),
            mock.patch("wfb.save_attachment", return_value={"target_id": "t1"}),
            mock.patch("wfb.inspect_target", return_value={"text_snapshot": "abc", "text_snapshot_chars": 3}),
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(["chrome", "capture"])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            payload = json.loads(written)
            self.assertEqual(payload["selection"]["method"], "heuristic")
            self.assertEqual(payload["target"]["id"], "t1")
            self.assertEqual(payload["debug"]["resolved_port"], 9333)

    def test_chrome_capture_selection_error_includes_recovery_hint(self):
        with (
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb._list_targets_with_port_fallback", return_value=([], 9222)),
            mock.patch(
                "wfb.select_capture_target",
                side_effect=wfb.ChromeBridgeError("no capture candidates found"),
            ),
            mock.patch("sys.stderr") as err,
        ):
            rc = wfb.main(["chrome", "capture"])
            self.assertEqual(rc, 5)
            written = "".join(call.args[0] for call in err.write.call_args_list if call.args)
            self.assertIn("next steps:", written)

    def test_bridge_ask_returns_json_with_provenance(self):
        target = {
            "id": "t1",
            "title": "Gemini Chat",
            "url": "https://gemini.google.com/glic",
            "type": "webview",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/page/t1",
        }
        inspect_result = {
            "title": "Gemini Chat",
            "url": "https://gemini.google.com/glic",
            "text_snapshot": "Hello from Gemini panel",
            "text_snapshot_chars": 23,
            "text_snapshot_truncated": False,
        }
        fake_session = {
            "id": "sess_abc",
            "name": "sess_abc",
            "model": "gemini-2.5-flash",
            "system": None,
            "messages": [],
        }
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb._list_targets_with_port_fallback", return_value=([target], 9333)),
            mock.patch("wfb.select_capture_target", return_value=(target, "heuristic", "selected by heuristic ranking")),
            mock.patch("wfb.save_attachment", return_value={"target_id": "t1"}),
            mock.patch("wfb.inspect_target", return_value=inspect_result),
            mock.patch("wfb.get_accessibility_tree", return_value=[]),
            mock.patch("wfb.get_active_session_id", return_value="sess_abc"),
            mock.patch("wfb.load_session", return_value=fake_session),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.ask_with_messages", return_value="The page discusses Gemini features."),
            mock.patch("wfb.append_turn"),
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(["bridge", "ask", "--prompt", "summarize this page"])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            payload = json.loads(written)
            self.assertEqual(payload["capture"]["selection"]["method"], "heuristic")
            self.assertEqual(payload["capture"]["target"]["id"], "t1")
            self.assertEqual(payload["prompt_envelope"]["template_version"], "2")
            self.assertEqual(payload["prompt_envelope"]["user_prompt"], "summarize this page")
            self.assertEqual(payload["capture"]["mode_chosen"], "text")
            self.assertEqual(payload["prompt_envelope"]["budget"]["capture_mode"], "text")
            self.assertEqual(payload["gemini_response"]["answer"], "The page discusses Gemini features.")
            self.assertEqual(payload["gemini_response"]["session_id"], "sess_abc")

    def test_bridge_ask_text_format(self):
        target = {
            "id": "t2",
            "title": "Example",
            "url": "https://example.test",
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t2",
        }
        inspect_result = {
            "title": "Example",
            "url": "https://example.test",
            "text_snapshot": "body text",
            "text_snapshot_chars": 9,
            "text_snapshot_truncated": False,
        }
        fake_session = {
            "id": "sess_xyz",
            "name": "sess_xyz",
            "model": "gemini-2.5-flash",
            "system": None,
            "messages": [],
        }
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb._list_targets_with_port_fallback", return_value=([target], 9222)),
            mock.patch("wfb.select_capture_target", return_value=(target, "fallback_first", "first candidate")),
            mock.patch("wfb.save_attachment", return_value={"target_id": "t2"}),
            mock.patch("wfb.inspect_target", return_value=inspect_result),
            mock.patch("wfb.get_accessibility_tree", return_value=[]),
            mock.patch("wfb.get_active_session_id", return_value="sess_xyz"),
            mock.patch("wfb.load_session", return_value=fake_session),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.ask_with_messages", return_value="Short answer."),
            mock.patch("wfb.append_turn"),
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(["bridge", "ask", "--prompt", "what is this?", "--format", "text"])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            self.assertIn("Short answer.", written)
            self.assertIn("fallback_first", written)

    def test_bridge_ask_capture_failure_reports_stage(self):
        with (
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch(
                "wfb._list_targets_with_port_fallback",
                side_effect=wfb.ChromeBridgeError("connection refused"),
            ),
            mock.patch("sys.stderr") as err,
        ):
            rc = wfb.main(["bridge", "ask", "--prompt", "test"])
            self.assertEqual(rc, 5)
            written = "".join(call.args[0] for call in err.write.call_args_list if call.args)
            self.assertIn("capture stage failed", written)
            self.assertIn("next steps:", written)
            self.assertIn("wfb bridge doctor", written)

    def test_bridge_ask_gemini_failure_reports_stage(self):
        target = {
            "id": "t1",
            "title": "Page",
            "url": "https://example.test",
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
        }
        inspect_result = {
            "title": "Page",
            "url": "https://example.test",
            "text_snapshot": "content",
            "text_snapshot_chars": 7,
            "text_snapshot_truncated": False,
        }
        fake_session = {
            "id": "sess_err",
            "name": "sess_err",
            "model": "gemini-2.5-flash",
            "system": None,
            "messages": [],
        }
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb._list_targets_with_port_fallback", return_value=([target], 9222)),
            mock.patch("wfb.select_capture_target", return_value=(target, "heuristic", "ranked")),
            mock.patch("wfb.save_attachment", return_value={"target_id": "t1"}),
            mock.patch("wfb.inspect_target", return_value=inspect_result),
            mock.patch("wfb.get_accessibility_tree", return_value=[]),
            mock.patch("wfb.get_active_session_id", return_value="sess_err"),
            mock.patch("wfb.load_session", return_value=fake_session),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.ask_with_messages", side_effect=wfb.GeminiApiError("503 unavailable")),
            mock.patch("sys.stderr") as err,
        ):
            rc = wfb.main(["bridge", "ask", "--prompt", "summarize"])
            self.assertEqual(rc, 5)
            written = "".join(call.args[0] for call in err.write.call_args_list if call.args)
            self.assertIn("ask stage failed", written)

    def test_bridge_ask_creates_session_when_none_active(self):
        target = {
            "id": "t1",
            "title": "Page",
            "url": "https://example.test",
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
        }
        inspect_result = {
            "title": "Page",
            "url": "https://example.test",
            "text_snapshot": "content",
            "text_snapshot_chars": 7,
            "text_snapshot_truncated": False,
        }
        new_session = {
            "id": "sess_new",
            "name": "sess_new",
            "model": "gemini-2.5-flash",
            "system": None,
            "messages": [],
        }
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb._list_targets_with_port_fallback", return_value=([target], 9222)),
            mock.patch("wfb.select_capture_target", return_value=(target, "heuristic", "ranked")),
            mock.patch("wfb.save_attachment", return_value={"target_id": "t1"}),
            mock.patch("wfb.inspect_target", return_value=inspect_result),
            mock.patch("wfb.get_accessibility_tree", return_value=[]),
            mock.patch("wfb.get_active_session_id", return_value=None),
            mock.patch("wfb.load_session", return_value=None),
            mock.patch("wfb.create_session", return_value=new_session) as create_sess,
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.ask_with_messages", return_value="ok"),
            mock.patch("wfb.append_turn"),
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(["bridge", "ask", "--prompt", "test"])
            self.assertEqual(rc, 0)
            create_sess.assert_called_once()
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            payload = json.loads(written)
            self.assertEqual(payload["gemini_response"]["session_id"], "sess_new")

    def test_bridge_loop_runs_max_iterations_and_stops(self):
        target = {
            "id": "t1",
            "title": "Page",
            "url": "https://example.test",
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
        }
        call_count = 0
        def _changing_inspect(**kwargs: object) -> dict[str, object]:
            nonlocal call_count
            call_count += 1
            return {
                "title": "Page",
                "url": "https://example.test",
                "text_snapshot": f"content v{call_count}",
                "text_snapshot_chars": 10,
                "text_snapshot_truncated": False,
            }
        fake_session = {
            "id": "sess_loop",
            "name": "sess_loop",
            "model": "gemini-2.5-flash",
            "system": None,
            "messages": [],
        }
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb._list_targets_with_port_fallback", return_value=([target], 9222)),
            mock.patch("wfb.select_capture_target", return_value=(target, "heuristic", "ranked")),
            mock.patch("wfb.save_attachment", return_value={"target_id": "t1"}),
            mock.patch("wfb.inspect_target", side_effect=_changing_inspect),
            mock.patch("wfb.get_accessibility_tree", return_value=[]),
            mock.patch("wfb.get_active_session_id", return_value="sess_loop"),
            mock.patch("wfb.load_session", return_value=fake_session),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.ask_with_messages", return_value="answer"),
            mock.patch("wfb.append_turn"),
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(["bridge", "loop", "--prompt", "check", "--max-iterations", "2"])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            payload = json.loads(written)
            self.assertEqual(payload["run"]["stop_reason"], "max_iterations")
            self.assertEqual(payload["run"]["iterations_completed"], 2)
            self.assertEqual(len(payload["iterations"]), 2)
            self.assertEqual(payload["summary"]["last_answer"], "answer")

    def test_bridge_loop_stability_check_stops_on_no_change(self):
        target = {
            "id": "t1",
            "title": "Page",
            "url": "https://example.test",
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
        }
        static_inspect = {
            "title": "Page",
            "url": "https://example.test",
            "text_snapshot": "same content",
            "text_snapshot_chars": 12,
            "text_snapshot_truncated": False,
        }
        fake_session = {
            "id": "sess_stable",
            "name": "sess_stable",
            "model": "gemini-2.5-flash",
            "system": None,
            "messages": [],
        }
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb._list_targets_with_port_fallback", return_value=([target], 9222)),
            mock.patch("wfb.select_capture_target", return_value=(target, "heuristic", "ranked")),
            mock.patch("wfb.save_attachment", return_value={"target_id": "t1"}),
            mock.patch("wfb.inspect_target", return_value=static_inspect),
            mock.patch("wfb.get_accessibility_tree", return_value=[]),
            mock.patch("wfb.get_active_session_id", return_value="sess_stable"),
            mock.patch("wfb.load_session", return_value=fake_session),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.ask_with_messages", return_value="first answer"),
            mock.patch("wfb.append_turn"),
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main([
                    "bridge", "loop", "--prompt", "check",
                    "--max-iterations", "5", "--stability-check", "on",
                ])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            payload = json.loads(written)
            self.assertEqual(payload["run"]["stop_reason"], "no_change")
            self.assertEqual(payload["run"]["iterations_completed"], 2)
            self.assertEqual(payload["iterations"][0]["status"], "continued")
            self.assertEqual(payload["iterations"][1]["status"], "no_change")

    def test_bridge_loop_capture_error_stops_with_error_reason(self):
        fake_session = {
            "id": "sess_err",
            "name": "sess_err",
            "model": "gemini-2.5-flash",
            "system": None,
            "messages": [],
        }
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch(
                "wfb._list_targets_with_port_fallback",
                side_effect=wfb.ChromeBridgeError("connection refused"),
            ),
            mock.patch("wfb.get_active_session_id", return_value="sess_err"),
            mock.patch("wfb.load_session", return_value=fake_session),
            mock.patch("wfb.set_active_session"),
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(["bridge", "loop", "--prompt", "check"])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            payload = json.loads(written)
            self.assertEqual(payload["run"]["stop_reason"], "error")
            self.assertEqual(payload["iterations"][0]["status"], "error")
            self.assertIn("capture stage failed", payload["iterations"][0]["error"])
            self.assertIn("wfb bridge doctor", payload["iterations"][0]["error"])

    def test_bridge_loop_ask_error_stops_with_error_reason(self):
        target = {
            "id": "t1",
            "title": "Page",
            "url": "https://example.test",
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
        }
        inspect_result = {
            "title": "Page",
            "url": "https://example.test",
            "text_snapshot": "content",
            "text_snapshot_chars": 7,
            "text_snapshot_truncated": False,
        }
        fake_session = {
            "id": "sess_ask_err",
            "name": "sess_ask_err",
            "model": "gemini-2.5-flash",
            "system": None,
            "messages": [],
        }
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb._list_targets_with_port_fallback", return_value=([target], 9222)),
            mock.patch("wfb.select_capture_target", return_value=(target, "heuristic", "ranked")),
            mock.patch("wfb.save_attachment", return_value={"target_id": "t1"}),
            mock.patch("wfb.inspect_target", return_value=inspect_result),
            mock.patch("wfb.get_accessibility_tree", return_value=[]),
            mock.patch("wfb.get_active_session_id", return_value="sess_ask_err"),
            mock.patch("wfb.load_session", return_value=fake_session),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.ask_with_messages", side_effect=wfb.GeminiApiError("503 unavailable")),
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main(["bridge", "loop", "--prompt", "check"])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            payload = json.loads(written)
            self.assertEqual(payload["run"]["stop_reason"], "error")
            self.assertIn("ask stage failed", payload["iterations"][0]["error"])

    def test_bridge_loop_rejects_invalid_max_iterations(self):
        rc = wfb.main(["bridge", "loop", "--prompt", "test", "--max-iterations", "0"])
        self.assertEqual(rc, 2)

    def test_bridge_loop_text_format(self):
        target = {
            "id": "t1",
            "title": "Page",
            "url": "https://example.test",
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/t1",
        }
        inspect_result = {
            "title": "Page",
            "url": "https://example.test",
            "text_snapshot": "body",
            "text_snapshot_chars": 4,
            "text_snapshot_truncated": False,
        }
        fake_session = {
            "id": "sess_txt",
            "name": "sess_txt",
            "model": "gemini-2.5-flash",
            "system": None,
            "messages": [],
        }
        with (
            mock.patch("wfb.wfb_home", return_value=Path("/tmp/fake")),
            mock.patch("wfb.parse_target_types", return_value=("page", "webview")),
            mock.patch("wfb._list_targets_with_port_fallback", return_value=([target], 9222)),
            mock.patch("wfb.select_capture_target", return_value=(target, "heuristic", "ranked")),
            mock.patch("wfb.save_attachment", return_value={"target_id": "t1"}),
            mock.patch("wfb.inspect_target", return_value=inspect_result),
            mock.patch("wfb.get_accessibility_tree", return_value=[]),
            mock.patch("wfb.get_active_session_id", return_value="sess_txt"),
            mock.patch("wfb.load_session", return_value=fake_session),
            mock.patch("wfb.set_active_session"),
            mock.patch("wfb.ask_with_messages", return_value="text answer"),
            mock.patch("wfb.append_turn"),
        ):
            with mock.patch("sys.stdout") as out:
                rc = wfb.main([
                    "bridge", "loop", "--prompt", "go",
                    "--max-iterations", "1", "--format", "text",
                ])
            self.assertEqual(rc, 0)
            written = "".join(call.args[0] for call in out.write.call_args_list if call.args)
            self.assertIn("iterations: 1/1", written)
            self.assertIn("stop_reason: max_iterations", written)
            self.assertIn("text answer", written)


if __name__ == "__main__":
    unittest.main()
