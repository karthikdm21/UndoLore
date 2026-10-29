"""Append-only human label assertions; no visual or physical verification."""

from datetime import datetime, timezone

from .core import ValidationError, token
from .evidence import evidence_status

DECISIONS = ("accept_label", "unclear", "reject_label")


def has_reviews(store):
    return store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence_reviews'"
    ).fetchone() is not None


def rows(store, session_id, bundle_id):
    if not has_reviews(store):
        return []
    return [dict(row) for row in store.db.execute(
        "SELECT * FROM evidence_reviews WHERE session_id=? AND bundle_id=? ORDER BY sequence",
        (session_id, bundle_id),
    )]


def review_status(store, session_id, bundle_id):
    # One SQLite snapshot keeps the bundle and review revision consistent.
    store.db.execute("BEGIN")
    try:
        evidence = evidence_status(store, session_id, bundle_id)
        history = rows(store, session_id, bundle_id)
        store.db.execute("COMMIT")
    except Exception:
        store.db.execute("ROLLBACK")
        raise
    latest = {row["evidence_id"]: row for row in history}
    sources = {source["source_id"]: source["status"] for source in evidence["current_sources"]}
    labels = []
    for ref in evidence["evidence"]:
        row = latest.get(ref["id"])
        state = "unreviewed"
        if row:
            state = row["decision"]
            if state == "accept_label":
                state = "accepted_manual_label" if sources[ref["source_id"]] == "bytes_match" else "needs_review"
        labels.append({"reference": ref, "latest_review": row, "label_state": state,
                       "source_status": sources[ref["source_id"]]})
    return {"session_id": session_id, "bundle_id": bundle_id,
            "synthetic": evidence["synthetic"], "review_revision": len(history),
            "history": history, "labels": labels,
            "sources": evidence["manifest"]["sources"],
            "graph_versions": evidence["graph_versions"],
            "physical_verification": "unknown", "timebase_status": "declared_not_decoded",
            "session_step_confirmation": "not_performed"}


def review_evidence(store, session_id, bundle_id, *, evidence_id, decision, actor,
                    reason, request_id, expected_review_revision, confirmed):
    for value, name in ((session_id, "session_id"), (bundle_id, "bundle_id"),
                        (evidence_id, "evidence_id"), (actor, "actor"), (request_id, "request_id")):
        token(value, name)
    if not isinstance(decision, str) or decision not in DECISIONS:
        raise ValidationError("decision must be accept_label, unclear or reject_label")
    if type(expected_review_revision) is not int or expected_review_revision < 0:
        raise ValidationError("expected_review_revision must be a non-negative integer")
    if confirmed is not True:
        raise ValidationError("deliberate review confirmation is required (--confirm)")
    if (not isinstance(reason, str) or not reason.strip() or len(reason) > 300
            or any(ord(c) < 32 or ord(c) == 127 for c in reason)):
        raise ValidationError("reason must be nonblank, at most 300 characters, without controls")
    store.db.execute("BEGIN IMMEDIATE")
    try:
        evidence = evidence_status(store, session_id, bundle_id)
        history = rows(store, session_id, bundle_id)
        previous = next((r for r in history if r["request_id"] == request_id), None)
        if previous:
            if (previous["evidence_id"], previous["decision"], previous["actor"],
                previous["reason"], previous["sequence"] - 1) != (
                    evidence_id, decision, actor, reason, expected_review_revision):
                raise ValidationError("request_id already used with different review input")
            result = previous
        else:
            if len(history) != expected_review_revision:
                raise ValidationError("stale review revision; read review-status first")
            ref = next((r for r in evidence["evidence"] if r["id"] == evidence_id), None)
            if ref is None:
                raise ValidationError("unknown evidence ID in this bundle")
            source = next(s for s in evidence["current_sources"] if s["source_id"] == ref["source_id"])
            if decision == "accept_label" and source["status"] != "bytes_match":
                raise ValidationError("cannot accept label: source bytes unavailable or mismatched")
            store.db.execute("""CREATE TABLE IF NOT EXISTS evidence_reviews (
                session_id TEXT NOT NULL,
                bundle_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK(sequence > 0),
                request_id TEXT NOT NULL,
                evidence_id TEXT NOT NULL,
                decision TEXT NOT NULL CHECK(decision IN ('accept_label','unclear','reject_label')),
                actor TEXT NOT NULL,
                reason TEXT NOT NULL,
                reviewed_at TEXT NOT NULL,
                source_status_at_review TEXT NOT NULL,
                PRIMARY KEY(session_id, bundle_id, sequence),
                UNIQUE(session_id, bundle_id, request_id),
                FOREIGN KEY(session_id, bundle_id) REFERENCES evidence_imports(session_id, bundle_id)
            )""")
            store.db.execute("INSERT INTO evidence_reviews VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (session_id, bundle_id, len(history) + 1, request_id, evidence_id,
                              decision, actor, reason, datetime.now(timezone.utc).isoformat(),
                              source["status"]))
            result = rows(store, session_id, bundle_id)[-1]
        store.db.execute("COMMIT")
        return result
    except Exception:
        store.db.execute("ROLLBACK")
        raise
