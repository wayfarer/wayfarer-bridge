"""Unit tests for local Gemini session storage."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import wfb_gemini_sessions as sessions


class TestWfbGeminiSessions(unittest.TestCase):
    def test_create_and_load_session(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            created = sessions.create_session(home, name="demo", model="gemini-2.5-flash", system=None)
            loaded = sessions.load_session(home, str(created["id"]))
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded["name"], "demo")

    def test_active_session_pointer(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            sessions.set_active_session(home, "sess_1")
            self.assertEqual(sessions.get_active_session_id(home), "sess_1")

    def test_append_and_reset(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            created = sessions.create_session(home, name=None, model="gemini-2.5-flash", system=None)
            sid = str(created["id"])
            sessions.append_turn(home, sid, role="user", text="hello")
            sessions.append_turn(home, sid, role="model", text="hi")
            loaded = sessions.load_session(home, sid)
            assert loaded is not None
            self.assertEqual(len(loaded["messages"]), 2)
            sessions.reset_session(home, sid)
            reset = sessions.load_session(home, sid)
            assert reset is not None
            self.assertEqual(reset["messages"], [])

    def test_session_stats_and_compaction(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            created = sessions.create_session(home, name=None, model="gemini-2.5-flash", system=None)
            sid = str(created["id"])
            for i in range(6):
                sessions.append_turn(home, sid, role="user" if i % 2 == 0 else "model", text=f"msg{i}")
            sess = sessions.load_session(home, sid)
            assert sess is not None
            stats = sessions.session_message_stats(sess)
            self.assertEqual(stats["turns"], 6)
            compacted = sessions.compact_session_history(
                home,
                session_id=sid,
                summary_text="summary",
                source_model="gemini-2.5-flash",
                keep_recent_turns=2,
            )
            assert compacted is not None
            msgs = compacted["messages"]
            self.assertEqual(msgs[0]["kind"], "history_summary")
            self.assertEqual(msgs[0]["summary_meta"]["covered_turn_count"], 4)
            self.assertEqual(len(msgs), 3)

    def test_compacted_session_copy_does_not_mutate_original(self):
        original = {
            "id": "sess_x",
            "messages": [
                {"role": "user", "text": "a"},
                {"role": "model", "text": "b"},
                {"role": "user", "text": "c"},
            ],
        }
        compacted = sessions.compacted_session_copy(
            original,
            summary_text="summary",
            source_model="gemini-2.5-flash",
            keep_recent_turns=1,
        )
        self.assertEqual(len(original["messages"]), 3)
        self.assertEqual(compacted["messages"][0]["kind"], "history_summary")

    def test_update_world_state_sync(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            created = sessions.create_session(home, name="demo", model="gemini-2.5-flash", system=None)
            sid = str(created["id"])
            updated = sessions.update_world_state_sync(
                home,
                session_id=sid,
                sync_mode="on",
                db_path="/tmp/world.db",
                scope="dev",
            )
            assert updated is not None
            self.assertTrue(sessions.world_state_sync_enabled(updated))
            self.assertEqual(updated["world_state_db_path"], "/tmp/world.db")


if __name__ == "__main__":
    unittest.main()
