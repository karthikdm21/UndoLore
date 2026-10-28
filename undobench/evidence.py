"""Persistent metadata references and source-byte checks, never visual verification."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .contracts import read_manifest, validate_manifest
from .core import ValidationError, token


def source_statuses(manifest, media_root):
    root = Path(media_root).resolve()
    results = []
    for source in manifest["sources"]:
        status = "missing"
        try:
            path = (root / source["relative_path"]).resolve()
            if root not in path.parents:
                status = "outside_media_root"
            elif path.exists():
                if not path.is_file():
                    status = "not_a_file"
                else:
                    digest = hashlib.sha256()
                    with path.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    status = "bytes_match" if digest.hexdigest() == source["sha256"] else "hash_mismatch"
        except OSError:
            status = "unreadable"
        results.append({"source_id": source["id"], "status": status,
                        "timebase_status": "declared_not_decoded"})
    return results


def import_evidence(store, session_id, manifest_path, media_root):
    manifest = read_manifest(manifest_path)
    root = Path(media_root).resolve()
    if not root.is_dir():
        raise ValidationError("media root must be an existing directory")
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    bundle_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    status = source_statuses(manifest, root)
    # Additive metadata only: do not promote evidence to confirmation or alter steps.
    store.db.execute("BEGIN IMMEDIATE")
    try:
        session = store._session(session_id)
        if session["kit_id"] != manifest["kit_id"]:
            raise ValidationError("manifest kit must match the session kit")
        store.db.execute("""CREATE TABLE IF NOT EXISTS evidence_imports (
            session_id TEXT NOT NULL REFERENCES sessions(id),
            bundle_id TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            media_root TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            initial_status_json TEXT NOT NULL,
            PRIMARY KEY (session_id, bundle_id)
        )""")
        existing = store.db.execute(
            "SELECT media_root FROM evidence_imports WHERE session_id=? AND bundle_id=?",
            (session_id, bundle_id),
        ).fetchone()
        if existing and existing["media_root"] != str(root):
            raise ValidationError("manifest already imported with a different media root")
        if not existing:
            store.db.execute("INSERT INTO evidence_imports VALUES (?, ?, ?, ?, ?, ?)",
                             (session_id, bundle_id, canonical, str(root),
                              datetime.now(timezone.utc).isoformat(), json.dumps(status)))
        store.db.execute("COMMIT")
    except Exception:
        store.db.execute("ROLLBACK")
        raise
    return evidence_status(store, session_id, bundle_id)


def evidence_status(store, session_id, bundle_id):
    token(session_id, "session_id")
    token(bundle_id, "bundle_id")
    store._session(session_id)
    if not store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_imports'"
    ).fetchone():
        raise ValidationError("no evidence imports in this database")
    row = store.db.execute(
        "SELECT * FROM evidence_imports WHERE session_id=? AND bundle_id=?", (session_id, bundle_id)
    ).fetchone()
    if row is None:
        raise ValidationError("unknown evidence bundle in this session")
    manifest = json.loads(row["manifest_json"])
    report = validate_manifest(manifest)
    return {**report, "session_id": session_id, "bundle_id": bundle_id,
            "imported_at": row["imported_at"], "manifest": manifest,
            "initial_sources": json.loads(row["initial_status_json"]),
            "current_sources": source_statuses(manifest, row["media_root"]),
            "confirmation_status": "not_confirmed"}
