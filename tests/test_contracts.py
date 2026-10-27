import copy
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from undobench.contracts import load_manifest, validate_manifest
from undobench.core import ValidationError

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data/fixtures/synthetic_manifest.json"


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(FIXTURE.read_text())

    def test_pts_and_rational_timebase_survive_roundtrip_without_fps(self):
        result = validate_manifest(self.data)
        ref = result["evidence"][0]
        self.assertEqual((ref["start_pts"], ref["end_pts"]), (3003, 6006))
        self.assertEqual((ref["start_ms"], ref["end_ms"]), (33, 66))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(self.data))
            self.assertEqual(load_manifest(path), result)
        self.assertEqual(result["media_status"], "not_inspected")
        self.assertEqual(result["review_status"], "human_review_declared_only")
        self.assertEqual(result["physical_verification"], "unknown")
        self.assertTrue(result["synthetic"])

    def test_bad_source_contracts(self):
        for key, value in (("duration_pts", 0), ("timebase_num", True),
                           ("timebase_den", 0), ("timebase_den", 1.5),
                           ("sha256", "not-a-hash"), ("relative_path", "../private.mp4"),
                           ("relative_path", "C:/private.mp4"), ("relative_path", "/tmp/video"),
                           ("relative_path", "a\\b.mp4")):
            data = copy.deepcopy(self.data)
            data["sources"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                validate_manifest(data)

    def test_bad_evidence_intervals_and_references(self):
        for key, value in (("start_pts", -1), ("start_pts", True), ("end_pts", 3003),
                           ("end_pts", 90001), ("start_pts", "3003"),
                           ("source_id", "invented"), ("part_id", "P99"),
                           ("tray_cell", "E5")):
            data = copy.deepcopy(self.data)
            data["evidence"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                validate_manifest(data)

    def test_separate_graphs_need_explicit_review_and_valid_inventory_links(self):
        for direction in ("disassembly", "reassembly"):
            for key, value in (("reviewed", False), ("reviewed", 1), ("reviewed_by", "")):
                data = copy.deepcopy(self.data)
                data["graphs"][direction][key] = value
                with self.subTest(direction=direction, key=key), self.assertRaises(ValidationError):
                    validate_manifest(data)
            for key, value in (("parts", ["P99"]), ("evidence_ids", ["invented"]),
                               ("prerequisites", ["missing"]), ("parts", ["P02"])):
                data = copy.deepcopy(self.data)
                data["graphs"][direction]["nodes"][0][key] = value
                with self.subTest(direction=direction, key=key), self.assertRaises(ValidationError):
                    validate_manifest(data)

    def test_cycle_and_self_dependency_rejected_but_acyclic_graph_allowed(self):
        node = self.data["graphs"]["disassembly"]["nodes"][0]
        second = copy.deepcopy(node)
        second["id"] = "second"
        second["prerequisites"] = [node["id"]]
        self.data["graphs"]["disassembly"]["nodes"].append(second)
        validate_manifest(self.data)
        node["prerequisites"] = ["second"]
        with self.assertRaisesRegex(ValidationError, "cycle"):
            validate_manifest(self.data)
        node["prerequisites"] = [node["id"]]
        with self.assertRaisesRegex(ValidationError, "cycle"):
            validate_manifest(self.data)

    def test_unknown_fields_duplicate_ids_and_inventory_size_rejected(self):
        mutations = [lambda d: d.update(confirmed=True),
                     lambda d: d.update(schema_version=True),
                     lambda d: d.update(synthetic="true"),
                     lambda d: d["sources"].append(copy.deepcopy(d["sources"][0])),
                     lambda d: d["evidence"].append(copy.deepcopy(d["evidence"][0])),
                     lambda d: d["parts"].pop(),
                     lambda d: d["parts"].append(d["parts"][0]),
                     lambda d: d["graphs"]["reassembly"]["nodes"].append(
                         copy.deepcopy(d["graphs"]["reassembly"]["nodes"][0]))]
        for mutation in mutations:
            data = copy.deepcopy(self.data)
            mutation(data)
            with self.assertRaises(ValidationError):
                validate_manifest(data)

    def test_json_ingestion_rejects_duplicate_keys_malformed_and_oversize(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            for content in ('{"schema_version":1,"schema_version":2}', '{', ' ' * 262145):
                path.write_text(content)
                with self.assertRaises(ValidationError):
                    load_manifest(path)

    def test_real_metadata_never_promotes_missing_media_to_verified_evidence(self):
        self.data["synthetic"] = False
        result = validate_manifest(self.data)
        self.assertFalse(result["synthetic"])
        self.assertEqual(result["media_status"], "not_inspected")
        self.assertEqual(result["physical_verification"], "unknown")

    def test_cli_reads_fixture_in_new_process_without_creating_session_storage(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "unused" / "db.sqlite3"
            result = subprocess.run(
                [sys.executable, "-m", "undobench", "--db", str(path),
                 "validate-manifest", str(FIXTURE)], cwd=ROOT,
                text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), validate_manifest(self.data))
            self.assertFalse(path.parent.exists())
            bad = Path(directory) / "bad.json"
            bad.write_text('{}')
            result = subprocess.run(
                [sys.executable, "-m", "undobench", "validate-manifest", str(bad)],
                cwd=ROOT, text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 2)
            self.assertIn("Error:", result.stderr)
            self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
