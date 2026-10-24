import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from undobench.core import DISCLAIMER, Store, ValidationError

ROOT = Path(__file__).resolve().parents[1]


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "sessions.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(self.store.db.close)
        self.session = self.store.create_session()

    def record(self, session=None, **changes):
        command = dict(part_id="P01", tray_cell="A1", actor="test-human",
                       request_id="request-1", expected_revision=0, confirmed=True)
        command.update(changes)
        return self.store.confirm(session or self.session, **command)

    def test_order_and_manual_evidence_are_persisted(self):
        self.record()
        self.record(part_id="P02", tray_cell="D4", request_id="request-2", expected_revision=1)
        history = self.store.history(self.session)
        self.assertEqual(history["revision"], 2)
        self.assertEqual([s["sequence"] for s in history["steps"]], [1, 2])
        self.assertEqual([s["part_id"] for s in history["steps"]], ["P01", "P02"])
        self.assertEqual([s["tray_cell"] for s in history["steps"]], ["A1", "D4"])
        self.assertTrue(all(s["evidence_mode"] == "manual_report" for s in history["steps"]))
        self.assertTrue(all(s["recorded_at"].endswith("+00:00") for s in history["steps"]))

    def test_malformed_inputs_do_not_append(self):
        invalid = [
            {"part_id": "P11"}, {"part_id": ""}, {"part_id": ["P01"]},
            {"tray_cell": "A0"}, {"tray_cell": "E1"}, {"tray_cell": "a1"},
            {"tray_cell": "A1; DROP TABLE steps"}, {"tray_cell": None},
            {"actor": ""}, {"actor": "a" * 65}, {"actor": " name "},
            {"request_id": ""}, {"request_id": "../private"},
            {"expected_revision": -1}, {"expected_revision": True},
            {"expected_revision": 0.0}, {"expected_revision": "0"},
            {"confirmed": False}, {"confirmed": 1}, {"confirmed": "yes"},
        ]
        for command in invalid:
            with self.subTest(command=command):
                with self.assertRaises(ValidationError):
                    self.record(**command)
                history = self.store.history(self.session)
                self.assertEqual((history["revision"], history["steps"]), (0, []))

    def test_unknown_or_malformed_session_is_rejected(self):
        for session in ("unknown", "../other", "", None):
            with self.subTest(session=session):
                with self.assertRaises(ValidationError):
                    self.store.history(session)
        with self.assertRaisesRegex(ValidationError, "unknown session"):
            self.record(session="unknown")

    def test_replay_is_idempotent_even_after_later_step(self):
        first = self.record()
        self.record(part_id="P02", request_id="request-2", expected_revision=1)
        self.assertEqual(self.record(), first)
        self.assertEqual(self.store.history(self.session)["revision"], 2)

    def test_conflicting_replay_and_stale_revision_do_not_append(self):
        self.record()
        for command in ({"part_id": "P02"}, {"tray_cell": "B2"},
                        {"actor": "other"}, {"expected_revision": 1}):
            with self.subTest(command=command):
                with self.assertRaisesRegex(ValidationError, "different input"):
                    self.record(**command)
        with self.assertRaisesRegex(ValidationError, "stale revision"):
            self.record(request_id="new-request")
        self.assertEqual(self.store.history(self.session)["revision"], 1)

    def test_sessions_are_isolated_including_request_ids(self):
        other = self.store.create_session()
        self.assertNotEqual(other, self.session)
        self.record()
        self.assertEqual(self.store.history(other)["steps"], [])
        self.record(session=other, part_id="P10", tray_cell="C3")
        self.assertEqual(self.store.history(other)["steps"][0]["part_id"], "P10")
        self.assertEqual(self.store.history(self.session)["steps"][0]["part_id"], "P01")

    def test_transaction_rolls_back_step_if_revision_update_fails(self):
        self.store.db.execute("""CREATE TEMP TRIGGER fail_revision BEFORE UPDATE ON sessions
                                 BEGIN SELECT RAISE(ABORT, 'test storage failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test storage failure"):
            self.record()
        self.assertEqual(self.store.history(self.session)["steps"], [])
        self.assertEqual(self.store.history(self.session)["revision"], 0)
        self.store.db.execute("DROP TRIGGER fail_revision")
        self.assertEqual(self.record()["sequence"], 1)

    def test_separate_connections_reject_outdated_writes(self):
        with Store(self.path) as other:
            self.assertEqual(other.history(self.session)["revision"], 0)
            self.record()
            with self.assertRaisesRegex(ValidationError, "stale revision"):
                other.confirm(self.session, part_id="P02", tray_cell="A2", actor="other",
                              request_id="other-request", expected_revision=0, confirmed=True)
        self.assertEqual(self.store.history(self.session)["revision"], 1)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "cli.sqlite3")

    def cli(self, *args, input=None):
        return subprocess.run([sys.executable, "-m", "undobench", "--db", self.db, *args],
                              cwd=ROOT, input=input, text=True, capture_output=True,
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=15)

    def test_new_process_restores_history_and_database_isolation(self):
        created = self.cli("new")
        self.assertEqual(created.returncode, 0, created.stderr)
        session = created.stdout.strip()
        written = self.cli("confirm", session, "--part", "P03", "--cell", "B2",
                           "--actor", "sample-human", "--request", "sample-1",
                           "--revision", "0", "--confirm")
        self.assertEqual(written.returncode, 0, written.stderr)
        restored = self.cli("history", session)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertIn("revision 1", restored.stdout)
        self.assertIn("1. place_in_tray P03 -> B2", restored.stdout)
        self.assertIn(DISCLAIMER, restored.stdout)
        with Store(Path(self.temp.name) / "separate.sqlite3") as other:
            with self.assertRaisesRegex(ValidationError, "unknown session"):
                other.history(session)

    def test_cli_requires_confirmation_and_reports_malformed_values(self):
        session = self.cli("new").stdout.strip()
        command = ("confirm", session, "--part", "P01", "--cell", "A1", "--actor", "human",
                   "--request", "one", "--revision", "0")
        rejected = self.cli(*command)
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("explicit human confirmation", rejected.stderr)
        malformed = self.cli(*command, "--confirm", "--part", "P99")
        self.assertEqual(malformed.returncode, 2)
        self.assertIn("part_id must be", malformed.stderr)
        bad_revision = self.cli(*command, "--revision", "abc", "--confirm")
        self.assertEqual(bad_revision.returncode, 2)
        self.assertIn("invalid int value", bad_revision.stderr)
        self.assertIn("No recorded steps.", self.cli("history", session).stdout)

    def test_demo_needs_human_input_and_never_touches_persistent_db(self):
        for answer, expected in (("yes\n", "1. place_in_tray P01 -> A1"),
                                 ("no\n", "No recorded steps.")):
            with self.subTest(answer=answer):
                result = self.cli("demo", input=answer)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)
                self.assertIn(DISCLAIMER, result.stdout)
                self.assertFalse(Path(self.db).exists())

    def test_storage_error_is_clear_and_nonzero(self):
        Path(self.db).mkdir()
        result = self.cli("new")
        self.assertEqual(result.returncode, 2)
        self.assertIn("Error:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
