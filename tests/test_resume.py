from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from undobench.core import PARTS, Store, ValidationError

ROOT = Path(__file__).resolve().parents[1]


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "records.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(self.store.db.close)
        self.session = self.store.create_session()

    def record(self, part, cell, revision, session=None):
        return self.store.confirm(session or self.session, part_id=part, tray_cell=cell,
                                  actor="synthetic-human", request_id=f"report-{revision}",
                                  expected_revision=revision, confirmed=True)

    def test_empty_session_has_no_invented_locations_or_last_step(self):
        summary = self.store.resume_summary(self.session)
        self.assertIsNone(summary["last_step"])
        self.assertEqual(summary["revision"], 0)
        self.assertEqual([p["part_id"] for p in summary["parts"]], list(PARTS))
        self.assertTrue(all(p["latest_report"] is None for p in summary["parts"]))
        self.assertTrue(all(p["current_physical_location"] == "unknown"
                            for p in summary["parts"]))

    def test_latest_reports_keep_references_without_claiming_current_locations(self):
        self.record("P01", "A1", 0)
        self.record("P02", "A1", 1)  # Shared cell does not establish occupancy.
        last = self.record("P01", "B2", 2)
        summary = self.store.resume_summary(self.session)
        self.assertEqual(summary["last_step"], last)
        parts = {p["part_id"]: p for p in summary["parts"]}
        self.assertEqual(parts["P01"]["latest_report"]["sequence"], 3)
        self.assertEqual(parts["P01"]["latest_report"]["tray_cell"], "B2")
        self.assertEqual(parts["P02"]["latest_report"]["sequence"], 2)
        self.assertEqual(parts["P02"]["latest_report"]["tray_cell"], "A1")
        self.assertIsNone(parts["P03"]["latest_report"])
        self.assertTrue(all(p["current_physical_location"] == "unknown"
                            for p in parts.values()))

    def test_multiple_sessions_and_unknown_session(self):
        other = self.store.create_session()
        self.record("P01", "A1", 0)
        self.assertIsNone(self.store.resume_summary(other)["last_step"])
        self.record("P10", "D4", 0, session=other)
        self.assertEqual(self.store.resume_summary(other)["last_step"]["part_id"], "P10")
        self.assertEqual(self.store.resume_summary(self.session)["last_step"]["part_id"], "P01")
        with self.assertRaises(ValidationError):
            self.store.resume_summary("missing")

    def test_read_only_connection_cannot_write_and_summary_does_not_change_file(self):
        self.record("P01", "A1", 0)
        before = self.path.read_bytes()
        with Store(self.path, read_only=True) as reader:
            reader.resume_summary(self.session)
            with self.assertRaises(sqlite3.OperationalError):
                reader.create_session()
        self.assertEqual(self.path.read_bytes(), before)

    def test_new_process_from_relocated_repository_reads_identical_summary(self):
        self.record("P01", "C3", 0)
        args = [sys.executable, "-m", "undobench", "--db", str(self.path),
                "resume", self.session]
        first = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(first.returncode, 0, first.stderr)
        checkout = self.root / "relocated-checkout"
        checkout.mkdir()
        shutil.copytree(ROOT / "undobench", checkout / "undobench",
                        ignore=shutil.ignore_patterns("__pycache__"))
        restored = subprocess.run(args, cwd=checkout, capture_output=True, text=True, timeout=15)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual(restored.stdout, first.stdout)
        self.assertIn(f"{self.session}/1", restored.stdout)
        self.assertIn("last reported tray C3", restored.stdout)
        self.assertIn("P02: reported tray UNKNOWN", restored.stdout)
        self.assertEqual(restored.stdout.count("current physical location UNKNOWN"), 10)
        self.assertIn("No next assembly action", restored.stdout)

    def test_resume_missing_database_does_not_create_it(self):
        missing = self.root / "missing" / "db.sqlite3"
        result = subprocess.run(
            [sys.executable, "-m", "undobench", "--db", str(missing), "resume", self.session],
            cwd=ROOT, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Error:", result.stderr)
        self.assertFalse(missing.parent.exists())


if __name__ == "__main__":
    unittest.main()
