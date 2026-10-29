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
from undobench.reviews import review_evidence, review_status

ROOT = Path(__file__).resolve().parents[1]


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "state.sqlite3"
        self.store = Store(self.db)
        self.addCleanup(self.store.db.close)
        self.session = self.store.create_session()
        self.manifest = json.loads((ROOT / "data/fixtures/synthetic_manifest.json").read_text())
        self.source = self.root / self.manifest["sources"][0]["relative_path"]
        self.source.parent.mkdir()
        self.source.write_bytes(b"Synthetic test bytes. Not a video or physical observation.")
        self.manifest["sources"][0]["sha256"] = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.path = self.root / "manifest.json"
        self.bundle = self.ingest(self.session)

    def ingest(self, session):
        self.path.write_text(json.dumps(self.manifest))
        return import_evidence(self.store, session, self.path, self.root)["bundle_id"]

    def review(self, **changes):
        command = dict(evidence_id="synthetic-label", decision="accept_label", actor="sample-reviewer",
                       reason="Synthetic label assertion only", request_id="review-1",
                       expected_review_revision=0, confirmed=True)
        command.update(changes)
        return review_evidence(self.store, self.session, self.bundle, **command)

    def test_no_reviews_read_only_and_explicit_assertion_preserves_session(self):
        before = self.db.read_bytes()
        with Store(self.db, read_only=True) as reader:
            empty = review_status(reader, self.session, self.bundle)
            self.assertEqual(empty["review_revision"], 0)
            self.assertEqual(empty["labels"][0]["label_state"], "unreviewed")
        self.assertEqual(before, self.db.read_bytes())
        record = self.review()
        status = review_status(self.store, self.session, self.bundle)
        self.assertEqual(status["history"], [record])
        self.assertEqual(status["labels"][0]["label_state"], "accepted_manual_label")
        self.assertEqual(status["labels"][0]["reference"]["start_pts"], 3003)
        self.assertEqual(record["source_status_at_review"], "bytes_match")
        self.assertTrue(status["synthetic"])
        self.assertEqual(status["sources"][0]["timebase_den"], 90000)
        self.assertEqual(status["graph_versions"]["disassembly"], "synthetic-v1")
        self.assertEqual(status["physical_verification"], "unknown")
        self.assertEqual(status["timebase_status"], "declared_not_decoded")
        self.assertEqual(self.store.history(self.session)["revision"], 0)
        self.assertEqual(self.store.history(self.session)["steps"], [])

    def test_unclear_and_reject_append_without_erasing_earlier_reviews(self):
        first = self.review()
        second = self.review(decision="unclear", request_id="review-2", expected_review_revision=1)
        third = self.review(decision="reject_label", request_id="review-3", expected_review_revision=2)
        status = review_status(self.store, self.session, self.bundle)
        self.assertEqual(status["history"], [first, second, third])
        self.assertEqual(status["labels"][0]["latest_review"], third)
        self.assertEqual(status["labels"][0]["label_state"], "reject_label")

    def test_missing_and_changed_media_cannot_accept_and_recheck_invalidates_projection(self):
        record = self.review()
        self.source.write_bytes(b"Changed synthetic bytes")
        with self.assertRaisesRegex(ValidationError, "source bytes"):
            self.review(request_id="review-2", expected_review_revision=1)
        status = review_status(self.store, self.session, self.bundle)
        self.assertEqual(status["labels"][0]["label_state"], "needs_review")
        self.assertEqual(status["history"], [record])
        self.assertEqual(self.review(), record)  # Replay is historical, not a new acceptance.
        self.source.unlink()
        self.assertEqual(review_status(self.store, self.session, self.bundle)
                         ["labels"][0]["source_status"], "missing")
        self.review(decision="unclear", request_id="review-2", expected_review_revision=1)
        self.assertEqual(review_status(self.store, self.session, self.bundle)
                         ["labels"][0]["label_state"], "unclear")

    def test_malformed_unconfirmed_and_unknown_reference_do_not_append(self):
        for changes in ({"confirmed": False}, {"confirmed": 1}, {"decision": "auto-confirm"},
                        {"evidence_id": "missing"}, {"expected_review_revision": True},
                        {"expected_review_revision": -1}, {"actor": ""}, {"reason": " "},
                        {"reason": "a" * 301}, {"reason": "bad\nreason"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.review(**changes)
        self.assertEqual(review_status(self.store, self.session, self.bundle)["history"], [])

    def test_idempotent_retry_and_conflicting_or_stale_commands(self):
        first = self.review()
        self.assertEqual(first, self.review())
        for changed in ({"reason": "different"}, {"actor": "other"}, {"decision": "unclear"},
                        {"evidence_id": "other"}, {"expected_review_revision": 1}):
            with self.subTest(changed=changed), self.assertRaisesRegex(ValidationError, "different"):
                self.review(**changed)
        with Store(self.db) as other:
            with self.assertRaisesRegex(ValidationError, "stale"):
                review_evidence(other, self.session, self.bundle, evidence_id="synthetic-label",
                                decision="unclear", actor="sample", reason="Synthetic ambiguity",
                                request_id="competing", expected_review_revision=0, confirmed=True)
        self.assertEqual(review_status(self.store, self.session, self.bundle)["review_revision"], 1)

    def test_session_and_bundle_reviews_are_isolated(self):
        self.review()
        other = self.store.create_session()
        with self.assertRaises(ValidationError):
            review_status(self.store, other, self.bundle)
        self.ingest(other)
        self.assertEqual(review_status(self.store, other, self.bundle)["review_revision"], 0)
        old = self.bundle
        self.manifest["graphs"]["disassembly"]["version"] = "synthetic-new"
        self.bundle = self.ingest(self.session)
        self.assertEqual(review_status(self.store, self.session, self.bundle)["review_revision"], 0)
        self.review(decision="unclear")  # Same request ID is scoped to the new bundle.
        self.assertEqual(review_status(self.store, self.session, old)["review_revision"], 1)

    def test_failed_append_rolls_back_without_erasing_history(self):
        first = self.review()
        self.store.db.execute("""CREATE TEMP TRIGGER fail_review BEFORE INSERT ON evidence_reviews
                                 BEGIN SELECT RAISE(ABORT, 'test disk failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test disk failure"):
            self.review(decision="unclear", request_id="review-2", expected_review_revision=1)
        self.assertEqual(review_status(self.store, self.session, self.bundle)["history"], [first])
        self.store.db.execute("DROP TRIGGER fail_review")
        self.assertEqual(self.review(decision="unclear", request_id="review-2",
                                     expected_review_revision=1)["sequence"], 2)

    def test_cli_deliberate_review_and_new_process_reload_are_read_only(self):
        def cli(*args):
            return subprocess.run([sys.executable, "-m", "undobench", "--db", str(self.db), *args],
                                  cwd=ROOT, text=True, capture_output=True, timeout=15)
        args = ("review-evidence", self.session, self.bundle, "--evidence", "synthetic-label",
                "--decision", "accept_label", "--actor", "sample", "--reason", "Synthetic label",
                "--request", "review-1", "--review-revision", "0")
        self.assertEqual(cli(*args).returncode, 2)
        accepted = cli(*args, "--confirm")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(json.loads(accepted.stdout)["assertion_type"], "manual_label_only")
        before = self.db.read_bytes()
        result = cli("review-status", self.session, self.bundle)
        self.assertEqual(result.returncode, 0, result.stderr)
        status = json.loads(result.stdout)
        self.assertEqual(status["history"][0]["evidence_id"], "synthetic-label")
        self.assertEqual(status["labels"][0]["reference"]["start_ms"], 33)
        self.assertEqual(self.db.read_bytes(), before)
        self.source.unlink()
        changed = cli("review-status", self.session, self.bundle)
        self.assertEqual(json.loads(changed.stdout)["labels"][0]["label_state"], "needs_review")


if __name__ == "__main__":
    unittest.main()
