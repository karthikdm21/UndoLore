"""Historical review links on manual reports; never visual verification."""

from .core import ValidationError
from .evidence import evidence_status
from .reviews import rows


def link_for_step(store, session_id, sequence):
    if not store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='step_evidence'"
    ).fetchone():
        return None
    row = store.db.execute(
        "SELECT bundle_id, evidence_id, review_revision, review_sequence, synthetic "
        "FROM step_evidence WHERE session_id=? AND sequence=?", (session_id, sequence)
    ).fetchone()
    if row is None:
        return None
    return {**dict(row), "synthetic": bool(row["synthetic"]),
            "support_status": "historical_manual_review_only"}


def prepare_link(store, session_id, reviewed, part_id, tray_cell):
    bundle_id, evidence_id, review_revision = reviewed
    evidence = evidence_status(store, session_id, bundle_id)
    history = rows(store, session_id, bundle_id)
    if len(history) != review_revision:
        raise ValidationError("stale review revision; read review-status first")
    ref = next((r for r in evidence["evidence"] if r["id"] == evidence_id), None)
    if ref is None:
        raise ValidationError("unknown evidence ID in this bundle")
    latest = next((r for r in reversed(history) if r["evidence_id"] == evidence_id), None)
    if latest is None or latest["decision"] != "accept_label":
        raise ValidationError("evidence label needs an accepted manual review")
    source = next(s for s in evidence["current_sources"] if s["source_id"] == ref["source_id"])
    if source["status"] != "bytes_match":
        raise ValidationError("source bytes unavailable or mismatched; review needed")
    if (ref["part_id"], ref["tray_cell"]) != (part_id, tray_cell):
        raise ValidationError("part and cell must match the reviewed reference")
    return {"bundle_id": bundle_id, "evidence_id": evidence_id,
            "review_revision": review_revision, "review_sequence": latest["sequence"],
            "synthetic": evidence["synthetic"], "support_status": "historical_manual_review_only"}


def insert_link(store, session_id, sequence, link):
    store.db.execute("""CREATE TABLE IF NOT EXISTS step_evidence (
        session_id TEXT NOT NULL,
        sequence INTEGER NOT NULL,
        bundle_id TEXT NOT NULL,
        evidence_id TEXT NOT NULL,
        review_revision INTEGER NOT NULL,
        review_sequence INTEGER NOT NULL,
        synthetic INTEGER NOT NULL CHECK(synthetic IN (0,1)),
        PRIMARY KEY(session_id, sequence),
        FOREIGN KEY(session_id, sequence) REFERENCES steps(session_id, sequence),
        FOREIGN KEY(session_id, bundle_id, review_sequence)
            REFERENCES evidence_reviews(session_id, bundle_id, sequence)
    )""")
    store.db.execute("INSERT INTO step_evidence VALUES (?,?,?,?,?,?,?)",
                     (session_id, sequence, link["bundle_id"], link["evidence_id"],
                      link["review_revision"], link["review_sequence"], int(link["synthetic"])))
