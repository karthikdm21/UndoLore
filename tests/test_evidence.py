import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from undobench.core import Store, ValidationError
from undobench.evidence import evidence_status, import_evidence, source_statuses

ROOT = Path(__file__).resolve().parents[1]


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "state.sqlite3"
        self.store = Store(self.db)
        self.addCleanup(self.store.db.close)
        self.session = self.store.create_session()
        self.manifest = json.loads((ROOT / "data/fixtures/synthetic_manifest.json").read_text())
        self.path = self.root / "manifest.json"
        self.media = self.root / "synthetic/not-a-real-video.mp4"
        self.media.parent.mkdir()

    def ingest(self):
        self.path.write_text(json.dumps(self.manifest))
        return import_evidence(self.store, self.session, self.path, self.root)

    def test_missing_source_stays_missing_and_never_confirms_steps(self):
        report = self.ingest()
        self.assertTrue(report["synthetic"])
        self.assertEqual(report["current_sources"][0]["status"], "missing")
        self.assertEqual(report["confirmation_status"], "not_confirmed")
        self.assertEqual(report["physical_verification"], "unknown")
        self.assertEqual(self.store.history(self.session)["steps"], [])
        self.assertEqual(self.store.history(self.session)["revision"], 0)

    def test_matching_bytes_are_not_video_verification_and_changes_are_rechecked(self):
        payload = b"Synthetic bytes only. Not an actual video."
        self.media.write_bytes(payload)
        self.manifest["sources"][0]["sha256"] = hashlib.sha256(payload).hexdigest()
        report = self.ingest()
        self.assertEqual(report["current_sources"][0]["status"], "bytes_match")
        self.assertEqual(report["current_sources"][0]["timebase_status"], "declared_not_decoded")
        self.assertEqual(report["media_status"], "not_inspected")
        self.media.write_bytes(b"Changed synthetic fixture")
        changed = evidence_status(self.store, self.session, report["bundle_id"])
        self.assertEqual(changed["current_sources"][0]["status"], "hash_mismatch")
        self.assertEqual(changed["initial_sources"][0]["status"], "bytes_match")
        self.media.unlink()
        self.assertEqual(evidence_status(self.store, self.session, report["bundle_id"])
                         ["current_sources"][0]["status"], "missing")

    def test_import_is_idempotent_and_snapshot_is_immutable(self):
        first = self.ingest()
        self.assertEqual(self.ingest(), first)
        self.manifest["graphs"]["disassembly"]["version"] = "second-version"
        second = self.ingest()
        self.assertNotEqual(first["bundle_id"], second["bundle_id"])
        self.assertEqual(evidence_status(self.store, self.session, first["bundle_id"])["manifest"],
                         first["manifest"])
        other_root = self.root / "other"
        other_root.mkdir()
        with self.assertRaisesRegex(ValidationError, "different media root"):
            import_evidence(self.store, self.session, self.path, other_root)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM evidence_imports").fetchone()[0], 2)

    def test_session_isolation_invalid_manifest_and_kit_mismatch(self):
        first = self.ingest()
        other = self.store.create_session()
        with self.assertRaisesRegex(ValidationError, "unknown evidence"):
            evidence_status(self.store, other, first["bundle_id"])
        self.manifest["kit_id"] = "unselected-real-kit"
        with self.assertRaisesRegex(ValidationError, "kit must match"):
            self.ingest()
        self.manifest["schema_version"] = 99
        with self.assertRaises(ValidationError):
            self.ingest()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM evidence_imports").fetchone()[0], 1)

    def test_resolved_escape_directory_and_unreadable_source_are_explicit(self):
        with patch("undobench.evidence.Path.resolve", side_effect=[self.root, self.root.parent / "outside"]):
            self.assertEqual(source_statuses(self.manifest, self.root)[0]["status"], "outside_media_root")
        self.media.mkdir()
        self.assertEqual(source_statuses(self.manifest, self.root)[0]["status"], "not_a_file")
        with patch("undobench.evidence.Path.open", side_effect=PermissionError("synthetic denied")), \
                patch("undobench.evidence.Path.is_file", return_value=True):
            self.assertEqual(source_statuses(self.manifest, self.root)[0]["status"], "unreadable")

    def test_failed_insert_rolls_back_and_original_import_survives(self):
        first = self.ingest()
        self.store.db.execute("""CREATE TEMP TRIGGER fail_import BEFORE INSERT ON evidence_imports
                                 BEGIN SELECT RAISE(ABORT, 'test storage failure'); END""")
        self.manifest["graphs"]["reassembly"]["version"] = "new-version"
        with self.assertRaises(sqlite3.IntegrityError):
            self.ingest()
        self.assertEqual(evidence_status(self.store, self.session, first["bundle_id"])["manifest"],
                         first["manifest"])
        self.assertEqual(self.store.history(self.session)["revision"], 0)

    def test_new_process_reloads_exact_pts_and_rechecks_missing_media_read_only(self):
        self.path.write_text(json.dumps(self.manifest))
        def cli(*args):
            return subprocess.run([sys.executable, "-m", "undobench", "--db", str(self.db), *args],
                                  cwd=ROOT, capture_output=True, text=True, timeout=15)
        imported = cli("import-evidence", self.session, str(self.path), "--media-root", str(self.root))
        self.assertEqual(imported.returncode, 0, imported.stderr)
        report = json.loads(imported.stdout)
        before = self.db.read_bytes()
        result = cli("evidence-status", self.session, report["bundle_id"])
        self.assertEqual(result.returncode, 0, result.stderr)
        restored = json.loads(result.stdout)
        self.assertEqual(restored, report)
        self.assertEqual(restored["evidence"][0]["start_pts"], 3003)
        self.assertEqual(restored["manifest"]["sources"][0]["timebase_den"], 90000)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(cli("evidence-status", self.session, "missing").returncode, 2)

    def test_legacy_database_without_import_table_remains_unchanged_on_read(self):
        before = self.db.read_bytes()
        with Store(self.db, read_only=True) as reader:
            with self.assertRaisesRegex(ValidationError, "no evidence imports"):
                evidence_status(reader, self.session, "missing")
        self.assertEqual(self.db.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
