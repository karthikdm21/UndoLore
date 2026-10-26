from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from undobench.core import Store, ValidationError

ROOT = Path(__file__).resolve().parents[1]


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "records.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(self.store.db.close)
        self.session = self.store.create_session()
        self.original = self.store.confirm(
            self.session, part_id="P01", tray_cell="A1", actor="sample",
            request_id="original", expected_revision=0, confirmed=True)

    def correct(self, **changes):
        args = dict(target=f"{self.session}/1", reason="Synthetic label was transcribed incorrectly",
                    part_id="P02", tray_cell="B2", actor="reviewer", request_id="correction-1",
                    expected_revision=1, confirmed=True)
        args.update(changes)
        return self.store.correct(self.session, **args)

    def test_original_is_immutable_and_identity_replacement_is_not_extra_part(self):
        corrected = self.correct()
        history = self.store.history(self.session)
        self.assertEqual(history["steps"][0], self.original)
        self.assertEqual(history["steps"][1], corrected)
        self.assertEqual(history["revision"], 2)
        self.assertEqual(corrected["target_sequence"], 1)
        parts = {p["part_id"]: p for p in self.store.resume_summary(self.session)["parts"]}
        self.assertIsNone(parts["P01"]["latest_report"])
        self.assertEqual(parts["P02"]["latest_report"], corrected)
        self.assertTrue(all(p["current_physical_location"] == "unknown" for p in parts.values()))

    def test_correction_chain_retains_every_target_and_reason(self):
        first = self.correct()
        second = self.correct(target=f"{self.session}/2", request_id="correction-2",
                              part_id="P03", expected_revision=2, reason="Second manual review")
        self.assertEqual(self.store.history(self.session)["steps"], [self.original, first, second])
        parts = {p["part_id"]: p for p in self.store.resume_summary(self.session)["parts"]}
        self.assertIsNone(parts["P02"]["latest_report"])
        self.assertEqual(parts["P03"]["latest_report"], second)
        with self.assertRaisesRegex(ValidationError, "already corrected"):
            self.correct(request_id="branch", expected_revision=3)

    def test_correction_of_older_report_does_not_override_later_report(self):
        later = self.store.confirm(self.session, part_id="P01", tray_cell="D4", actor="sample",
                                   request_id="later", expected_revision=1, confirmed=True)
        corrected = self.correct(part_id="P01", expected_revision=2)
        summary = self.store.resume_summary(self.session)
        self.assertEqual(summary["last_step"], corrected)
        self.assertEqual(summary["parts"][0]["latest_report"], later)

    def test_retries_and_command_kind_conflicts_are_safe(self):
        first = self.correct()
        self.assertEqual(self.correct(), first)
        self.store.confirm(self.session, part_id="P10", tray_cell="D4", actor="sample",
                           request_id="later", expected_revision=2, confirmed=True)
        self.assertEqual(self.correct(), first)
        for changed in ({"reason": "Different reason"}, {"part_id": "P03"},
                        {"target": f"{self.session}/2"}, {"actor": "other"},
                        {"tray_cell": "C3"}, {"expected_revision": 3},
                        {"request_id": "original", "expected_revision": 0}):
            with self.subTest(changed=changed), self.assertRaises(ValidationError):
                self.correct(**changed)
        with self.assertRaises(ValidationError):
            self.store.confirm(self.session, part_id="P02", tray_cell="B2", actor="reviewer",
                               request_id="correction-1", expected_revision=1, confirmed=True)
        self.assertEqual(self.store.history(self.session)["revision"], 3)

    def test_invalid_cross_session_stale_and_unconfirmed_requests_leave_history_intact(self):
        other = self.store.create_session()
        self.store.confirm(other, part_id="P10", tray_cell="D4", actor="sample",
                           request_id="original", expected_revision=0, confirmed=True)
        invalid = [{"target": f"{other}/1"}, {"target": f"{self.session}/99"},
                   {"target": "1"}, {"target": None}, {"target": f"{self.session}/0"},
                   {"reason": " "}, {"reason": "a" * 301}, {"reason": "bad\nreason"},
                   {"reason": None}, {"expected_revision": 0}, {"expected_revision": True},
                   {"confirmed": False}, {"confirmed": 1}, {"part_id": "P99"},
                   {"tray_cell": "Z9"}, {"part_id": "P01", "tray_cell": "A1"}]
        for changed in invalid:
            with self.subTest(changed=changed), self.assertRaises(ValidationError):
                self.correct(**changed)
            self.assertEqual(self.store.history(self.session)["steps"], [self.original])
        self.assertEqual(self.store.history(other)["revision"], 1)

    def test_metadata_failure_rolls_back_appended_step_and_revision(self):
        self.store.db.execute("""CREATE TEMP TRIGGER fail_correction BEFORE INSERT ON corrections
                                 BEGIN SELECT RAISE(ABORT, 'test metadata failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test metadata failure"):
            self.correct()
        self.assertEqual(self.store.history(self.session)["steps"], [self.original])
        self.assertEqual(self.store.history(self.session)["revision"], 1)
        self.store.db.execute("DROP TRIGGER fail_correction")
        self.assertEqual(self.correct()["sequence"], 2)

    def test_legacy_database_read_only_then_additive_upgrade_preserves_report(self):
        self.store.db.execute("DROP TABLE corrections")
        before = self.path.read_bytes()
        with Store(self.path, read_only=True) as reader:
            self.assertEqual(reader.resume_summary(self.session)["last_step"], self.original)
        self.assertEqual(self.path.read_bytes(), before)
        with Store(self.path) as writer:
            self.assertEqual(writer.history(self.session)["steps"], [self.original])
        self.correct()
        self.assertEqual(self.store.history(self.session)["steps"][0], self.original)

    def test_cli_correction_and_provenance_survive_separate_processes(self):
        def cli(*args):
            return subprocess.run([sys.executable, "-m", "undobench", "--db", str(self.path), *args],
                                  cwd=ROOT, capture_output=True, text=True, timeout=15)
        args = ("correct", self.session, "--target", f"{self.session}/1", "--reason",
                "Synthetic transcription correction", "--part", "P02", "--cell", "B2",
                "--actor", "reviewer", "--request", "cli-correction", "--revision", "1")
        denied = cli(*args)
        self.assertEqual(denied.returncode, 2)
        accepted = cli(*args, "--confirm")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(cli(*args, "--confirm").returncode, 0)
        resume = cli("resume", self.session)
        self.assertEqual(resume.returncode, 0, resume.stderr)
        self.assertIn(f"correction of {self.session}/1", resume.stdout)
        self.assertIn("reason=Synthetic transcription correction", resume.stdout)
        self.assertIn(f"{self.session}/2", resume.stdout)
        self.assertIn("P01: reported tray UNKNOWN", resume.stdout)
        self.assertIn("P02: last reported tray B2", resume.stdout)
        self.assertIn("current physical location UNKNOWN", resume.stdout)
        self.assertIn("1. place_in_tray P01 -> A1", cli("history", self.session).stdout)
        self.assertEqual(self.store.history(self.session)["revision"], 2)


if __name__ == "__main__":
    unittest.main()
