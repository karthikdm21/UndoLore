import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from undobench.core import Store, ValidationError
from undobench.evidence import import_evidence
from undobench.reviews import review_evidence

ROOT = Path(__file__).resolve().parents[1]


class LinkedTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "state.sqlite3"
        self.store = Store(self.db)
        self.addCleanup(self.store.db.close)
        self.session = self.store.create_session()
        data = json.loads((ROOT / "data/fixtures/synthetic_manifest.json").read_text())
        self.source = self.root / data["sources"][0]["relative_path"]
        self.source.parent.mkdir()
        self.source.write_bytes(b"Synthetic test bytes only. Not playable media.")
        data["sources"][0]["sha256"] = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.path = self.root / "manifest.json"
        self.path.write_text(json.dumps(data))
        self.bundle = import_evidence(self.store, self.session, self.path, self.root)["bundle_id"]

    def review(self, decision="accept_label", revision=0):
        return review_evidence(self.store, self.session, self.bundle, evidence_id="synthetic-label",
                               decision=decision, actor="sample-reviewer", reason="Synthetic assertion",
                               request_id=f"review-{revision}", expected_review_revision=revision,
                               confirmed=True)

    def confirm(self, **changes):
        args = dict(bundle_id=self.bundle, evidence_id="synthetic-label", expected_review_revision=1,
                    part_id="P01", tray_cell="A1", actor="sample-human", request_id="linked-1",
                    expected_revision=0, confirmed=True)
        args.update(changes)
        return self.store.confirm_reviewed(self.session, **args)

    def test_atomic_record_and_reference_survive_readonly_resume(self):
        review = self.review()
        result = self.confirm()
        self.assertEqual(result["sequence"], 1)
        self.assertEqual(result["evidence_mode"], "manual_report")
        ref = result["evidence_reference"]
        self.assertEqual(ref["review_sequence"], review["sequence"])
        self.assertEqual(ref["bundle_id"], self.bundle)
        self.assertTrue(ref["synthetic"])
        self.assertEqual(ref["support_status"], "historical_manual_review_only")
        with Store(self.db, read_only=True) as reader:
            summary = reader.resume_summary(self.session)
            self.assertEqual(summary["last_step"], result)
            self.assertEqual(summary["revision"], 1)
            self.assertTrue(all(p["current_physical_location"] == "unknown" for p in summary["parts"]))

    def test_unreviewed_unclear_and_rejected_labels_cannot_confirm(self):
        with self.assertRaises(ValidationError):
            self.confirm(expected_review_revision=0)
        self.review("unclear")
        with self.assertRaises(ValidationError):
            self.confirm()
        self.review("reject_label", 1)
        with self.assertRaises(ValidationError):
            self.confirm(expected_review_revision=2)
        self.assertEqual(self.store.history(self.session)["steps"], [])

    def test_stale_session_or_review_and_part_cell_mismatches_reject(self):
        self.review()
        for changes in ({"expected_revision": 1}, {"expected_review_revision": 0},
                        {"expected_review_revision": True}, {"part_id": "P02"},
                        {"tray_cell": "A2"}, {"evidence_id": "missing"},
                        {"bundle_id": "missing"}, {"confirmed": False}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.confirm(**changes)
        self.assertEqual(self.store.history(self.session)["revision"], 0)
        self.review("unclear", 1)
        with self.assertRaisesRegex(ValidationError, "stale review"):
            self.confirm()

    def test_current_missing_or_mismatched_source_blocks_new_confirmation(self):
        self.review()
        self.source.write_bytes(b"Changed synthetic bytes")
        with self.assertRaisesRegex(ValidationError, "source bytes"):
            self.confirm()
        self.source.unlink()
        with self.assertRaisesRegex(ValidationError, "source bytes"):
            self.confirm()
        self.assertEqual(self.store.history(self.session)["steps"], [])

    def test_retries_are_historical_and_cannot_change_reference_or_command(self):
        self.review()
        first = self.confirm()
        self.source.unlink()
        self.assertEqual(self.confirm(), first)
        self.review("unclear", 1)
        self.assertEqual(self.confirm(), first)
        for changes in ({"bundle_id": "other"}, {"evidence_id": "other"},
                        {"expected_review_revision": 2}, {"actor": "other"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.confirm(**changes)
        with self.assertRaises(ValidationError):
            self.store.confirm(self.session, part_id="P01", tray_cell="A1", actor="sample-human",
                               request_id="linked-1", expected_revision=0, confirmed=True)
        self.assertEqual(self.store.history(self.session)["revision"], 1)

    def test_cross_session_review_cannot_be_reused(self):
        self.review()
        other = self.store.create_session()
        import_evidence(self.store, other, self.path, self.root)
        with self.assertRaises(ValidationError):
            self.store.confirm_reviewed(other, bundle_id=self.bundle, evidence_id="synthetic-label",
                                        expected_review_revision=1, part_id="P01", tray_cell="A1",
                                        actor="sample", request_id="linked-1", expected_revision=0,
                                        confirmed=True)
        self.assertEqual(self.store.history(other)["steps"], [])

    def test_link_insert_failure_rolls_back_step_and_session_revision(self):
        self.review()
        first = self.confirm()
        self.store.db.execute("""CREATE TEMP TRIGGER fail_link BEFORE INSERT ON step_evidence
                                 BEGIN SELECT RAISE(ABORT, 'test link failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test link failure"):
            self.confirm(request_id="linked-2", expected_revision=1)
        self.assertEqual(self.store.history(self.session)["steps"], [first])
        self.assertEqual(self.store.history(self.session)["revision"], 1)
        self.store.db.execute("DROP TRIGGER fail_link")
        self.assertEqual(self.confirm(request_id="linked-2", expected_revision=1)["sequence"], 2)

    def test_cli_new_process_confirmation_and_resume_with_historical_provenance(self):
        self.review()
        def cli(*args):
            return subprocess.run([sys.executable, "-m", "undobench", "--db", str(self.db), *args],
                                  cwd=ROOT, capture_output=True, text=True, timeout=15)
        args = ("confirm-reviewed", self.session, self.bundle, "--evidence", "synthetic-label",
                "--review-revision", "1", "--part", "P01", "--cell", "A1", "--actor", "sample",
                "--request", "linked-1", "--revision", "0")
        self.assertEqual(cli(*args).returncode, 2)
        confirmed = cli(*args, "--confirm")
        self.assertEqual(confirmed.returncode, 0, confirmed.stderr)
        before = self.db.read_bytes()
        resumed = cli("resume", self.session)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertIn(f"{self.bundle}/synthetic-label", resumed.stdout)
        self.assertIn("historical manual review", resumed.stdout)
        self.assertIn("synthetic=True", resumed.stdout)
        self.assertIn("current source support not assessed here", resumed.stdout)
        self.assertEqual(before, self.db.read_bytes())
        self.assertEqual(cli(*args, "--confirm").returncode, 0)
        self.assertEqual(self.store.history(self.session)["revision"], 1)


if __name__ == "__main__":
    unittest.main()
