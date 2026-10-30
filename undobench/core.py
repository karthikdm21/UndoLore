"""Local manual reports. No physical or visual verification is performed."""

from datetime import datetime, timezone
from pathlib import Path
import re
import sqlite3
import uuid

KIT_ID = "synthetic-block-kit-v1"
PARTS = {f"P{i:02}": f"Synthetic block {i:02}" for i in range(1, 11)}
CELLS = tuple(f"{row}{col}" for row in "ABCD" for col in range(1, 5))
DISCLAIMER = "Manual human report only; no visual evidence or physical verification."


class ValidationError(ValueError):
    """A command was rejected without appending a record."""


def token(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValidationError(f"{field} must be 1-64 ASCII letters, digits, _ or -")
    return value


class Store:
    def __init__(self, path, *, read_only=False):
        path = Path(path)
        if read_only:
            self.db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                                      uri=True, isolation_level=None, timeout=5)
            self.db.row_factory = sqlite3.Row
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                kit_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0)
            );
            CREATE TABLE IF NOT EXISTS steps (
                session_id TEXT NOT NULL REFERENCES sessions(id),
                sequence INTEGER NOT NULL CHECK (sequence > 0),
                request_id TEXT NOT NULL,
                part_id TEXT NOT NULL,
                tray_cell TEXT NOT NULL,
                actor TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                evidence_mode TEXT NOT NULL CHECK (evidence_mode = 'manual_report'),
                PRIMARY KEY (session_id, sequence),
                UNIQUE (session_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS corrections (
                session_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                target_sequence INTEGER NOT NULL,
                reason TEXT NOT NULL,
                PRIMARY KEY (session_id, sequence),
                UNIQUE (session_id, target_sequence),
                FOREIGN KEY (session_id, sequence) REFERENCES steps(session_id, sequence),
                FOREIGN KEY (session_id, target_sequence) REFERENCES steps(session_id, sequence)
            );
        """)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.db.close()

    def create_session(self):
        session_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO sessions (id, kit_id, created_at) VALUES (?, ?, ?)",
            (session_id, KIT_ID, datetime.now(timezone.utc).isoformat()),
        )
        return session_id

    def _session(self, session_id):
        token(session_id, "session_id")
        row = self.db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            raise ValidationError("unknown session; create one with new")
        if row["kit_id"] != KIT_ID:
            raise ValidationError("unsupported kit version")
        return row

    def history(self, session_id):
        # A single read transaction gives a consistent revision and history.
        self.db.execute("BEGIN")
        try:
            session = dict(self._session(session_id))
            session["steps"] = [dict(row) for row in self.db.execute(
                "SELECT * FROM steps WHERE session_id = ? ORDER BY sequence", (session_id,)
            )]
            # Legacy databases remain readable without a write/migration on resume.
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='corrections'").fetchone():
                metadata = {row["sequence"]: dict(row) for row in self.db.execute(
                    "SELECT sequence, target_sequence, reason FROM corrections WHERE session_id = ?",
                    (session_id,),
                )}
                for step in session["steps"]:
                    if step["sequence"] in metadata:
                        step.update(metadata[step["sequence"]])
            from .linked import link_for_step
            for step in session["steps"]:
                link = link_for_step(self, session_id, step["sequence"])
                if link:
                    step["evidence_reference"] = link
            self.db.execute("COMMIT")
            return session
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def resume_summary(self, session_id):
        """Summarize recorded reports, never infer current physical state."""
        history = self.history(session_id)
        # Corrections replace a report in its original historical slot, not at 'now'.
        effective = {}
        roots = {}
        for step in history["steps"]:
            root = roots[step["target_sequence"]] if "target_sequence" in step else step["sequence"]
            roots[step["sequence"]] = root
            effective[root] = step
        latest = {step["part_id"]: step for _, step in sorted(effective.items())}
        return {
            "session_id": session_id,
            "revision": history["revision"],
            "last_step": history["steps"][-1] if history["steps"] else None,
            "parts": [
                {"part_id": part, "latest_report": latest.get(part),
                 "current_physical_location": "unknown"}
                for part in PARTS
            ],
        }

    def confirm(self, session_id, *, part_id, tray_cell, actor,
                request_id, expected_revision, confirmed):
        return self._append(session_id, part_id=part_id, tray_cell=tray_cell, actor=actor,
                            request_id=request_id, expected_revision=expected_revision,
                            confirmed=confirmed)

    def confirm_reviewed(self, session_id, *, bundle_id, evidence_id, expected_review_revision,
                         part_id, tray_cell, actor, request_id, expected_revision, confirmed):
        token(bundle_id, "bundle_id")
        token(evidence_id, "evidence_id")
        if type(expected_review_revision) is not int or expected_review_revision < 0:
            raise ValidationError("expected_review_revision must be a non-negative integer")
        return self._append(session_id, part_id=part_id, tray_cell=tray_cell, actor=actor,
                            request_id=request_id, expected_revision=expected_revision,
                            confirmed=confirmed,
                            reviewed=(bundle_id, evidence_id, expected_review_revision))

    def correct(self, session_id, *, target, reason, part_id, tray_cell, actor,
                request_id, expected_revision, confirmed):
        token(session_id, "session_id")
        match = re.fullmatch(r"([A-Za-z0-9_-]{1,64})/([1-9][0-9]{0,8})", target) if isinstance(target, str) else None
        if match is None or match[1] != session_id:
            raise ValidationError("target must be this session's full session-id/step-number reference")
        if (not isinstance(reason, str) or not reason.strip() or len(reason) > 300
                or any(ord(char) < 32 or ord(char) == 127 for char in reason)):
            raise ValidationError("reason must be 1-300 characters, nonblank, without control characters")
        return self._append(session_id, part_id=part_id, tray_cell=tray_cell, actor=actor,
                            request_id=request_id, expected_revision=expected_revision,
                            confirmed=confirmed, correction=(int(match[2]), reason))

    def _append(self, session_id, *, part_id, tray_cell, actor, request_id,
                expected_revision, confirmed, correction=None, reviewed=None):
        token(session_id, "session_id")
        token(request_id, "request_id")
        token(actor, "actor")
        if not isinstance(part_id, str) or part_id not in PARTS:
            raise ValidationError("part_id must be P01-P10 from the synthetic inventory")
        if not isinstance(tray_cell, str) or tray_cell not in CELLS:
            raise ValidationError("tray_cell must be A1-D4 (uppercase)")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValidationError("expected_revision must be a non-negative integer")
        if confirmed is not True:
            raise ValidationError("explicit human confirmation is required (--confirm)")
        from .linked import link_for_step, prepare_link, insert_link
        self.db.execute("BEGIN IMMEDIATE")
        try:
            session = self._session(session_id)
            previous = self.db.execute(
                "SELECT * FROM steps WHERE session_id = ? AND request_id = ?",
                (session_id, request_id),
            ).fetchone()
            if previous is not None:
                link = link_for_step(self, session_id, previous["sequence"])
                original_reference = (link["bundle_id"], link["evidence_id"], link["review_revision"]) if link else None
                if original_reference != reviewed:
                    raise ValidationError("request_id already used with different evidence reference or command")
                metadata = self.db.execute(
                    "SELECT target_sequence, reason FROM corrections WHERE session_id = ? AND sequence = ?",
                    (session_id, previous["sequence"]),
                ).fetchone()
                if (tuple(metadata) if metadata else None) != correction:
                    raise ValidationError("request_id already used with different command or correction")
                if (previous["part_id"], previous["tray_cell"], previous["actor"],
                    previous["sequence"] - 1) != (part_id, tray_cell, actor, expected_revision):
                    raise ValidationError("request_id already used with different input")
                result = dict(previous)
                if correction:
                    result.update(target_sequence=correction[0], reason=correction[1])
                if link:
                    result["evidence_reference"] = link
            else:
                if session["revision"] != expected_revision:
                    raise ValidationError("stale revision; read history before confirming again")
                link = prepare_link(self, session_id, reviewed, part_id, tray_cell) if reviewed else None
                if correction:
                    target = self.db.execute(
                        "SELECT * FROM steps WHERE session_id = ? AND sequence = ?",
                        (session_id, correction[0]),
                    ).fetchone()
                    if target is None:
                        raise ValidationError("target record does not exist in this session")
                    if self.db.execute(
                        "SELECT 1 FROM corrections WHERE session_id = ? AND target_sequence = ?",
                        (session_id, correction[0]),
                    ).fetchone():
                        raise ValidationError("target already corrected; target its latest correction instead")
                    if (target["part_id"], target["tray_cell"]) == (part_id, tray_cell):
                        raise ValidationError("correction must change the reported part or tray cell")
                sequence = expected_revision + 1
                self.db.execute(
                    "INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?, ?, 'manual_report')",
                    (session_id, sequence, request_id, part_id, tray_cell, actor,
                     datetime.now(timezone.utc).isoformat()),
                )
                if link:
                    insert_link(self, session_id, sequence, link)
                if correction:
                    self.db.execute("INSERT INTO corrections VALUES (?, ?, ?, ?)",
                                    (session_id, sequence, correction[0], correction[1]))
                self.db.execute("UPDATE sessions SET revision = ? WHERE id = ?",
                                (sequence, session_id))
                result = dict(self.db.execute(
                    "SELECT * FROM steps WHERE session_id = ? AND sequence = ?",
                    (session_id, sequence),
                ).fetchone())
                if correction:
                    result.update(target_sequence=correction[0], reason=correction[1])
                if link:
                    result["evidence_reference"] = link
            self.db.execute("COMMIT")
            return result
        except Exception:
            self.db.execute("ROLLBACK")
            raise
