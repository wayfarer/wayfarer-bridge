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


if __name__ == "__main__":
    unittest.main()
