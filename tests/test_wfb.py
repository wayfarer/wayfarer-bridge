"""Smoke tests for wfb CLI (stdlib unittest)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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
    def test_init_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            db = d / "t.db"
            r1 = _run(["--db", str(db), "init"], cwd=d)
            self.assertEqual(r1.returncode, 0, r1.stderr)
            r2 = _run(["--db", str(db), "init"], cwd=d)
            self.assertEqual(r2.returncode, 0, r2.stderr)
            self.assertTrue(db.is_file())

    def test_seed_upsert_and_status(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            db = d / "t.db"
            self.assertEqual(_run(["--db", str(db), "init"], cwd=d).returncode, 0)

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
            )
            self.assertEqual(r2.returncode, 0, r2.stderr)

            st = _run(["--db", str(db), "status", "--format", "text"], cwd=d)
            self.assertEqual(st.returncode, 0, st.stderr)
            self.assertIn("First updated", st.stdout)
            self.assertIn("blocked", st.stdout)

            js = _run(["--db", str(db), "status", "--format", "json"], cwd=d)
            self.assertEqual(js.returncode, 0, js.stderr)
            data = json.loads(js.stdout)
            self.assertEqual(data["version"], 1)
            self.assertEqual(data["summary"]["tasks"]["blocked"], 1)
            self.assertEqual(len(data["highlights"]["constraints"]), 1)

    def test_seed_replace_clears_stale(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            db = d / "t.db"
            self.assertEqual(_run(["--db", str(db), "init"], cwd=d).returncode, 0)
            env = {
                "version": 1,
                "active_tasks": [{"id": "a", "title": "A", "status": "done"}],
            }
            self.assertEqual(
                _run(["--db", str(db), "seed", "--json", json.dumps(env)], cwd=d).returncode,
                0,
            )
            rep = {"version": 1, "active_tasks": []}
            r = _run(
                ["--db", str(db), "seed", "--replace", "--json", json.dumps(rep)],
                cwd=d,
            )
            self.assertEqual(r.returncode, 0, r.stderr)
            js = _run(["--db", str(db), "status", "--format", "json"], cwd=d)
            data = json.loads(js.stdout)
            self.assertEqual(data["summary"]["tasks"]["done"], 0)
            self.assertEqual(data["highlights"]["tasks"], [])

    def test_validation_unknown_envelope_key(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            db = d / "t.db"
            self.assertEqual(_run(["--db", str(db), "init"], cwd=d).returncode, 0)
            bad = {"version": 1, "extra": 1}
            r = _run(["--db", str(db), "seed", "--json", json.dumps(bad)], cwd=d)
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
            env = {"HOME": str(fake_home)}
            r1 = _run(["init"], env_extra=env)
            self.assertEqual(r1.returncode, 0, r1.stderr)
            dbpath = fake_home / ".wfb" / "wayfarer.db"
            self.assertTrue((fake_home / ".wfb").is_dir())
            self.assertTrue(dbpath.is_file())
            r2 = _run(["init"], env_extra=env)
            self.assertEqual(r2.returncode, 0, r2.stderr)


if __name__ == "__main__":
    unittest.main()
